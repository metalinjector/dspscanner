r"""Чтение PDF через PyMuPDF с опциональным Tesseract OCR."""
from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from app.readers.base import BaseReader, ReaderOutcome
from app.settings_store import autodetect_external_programs, load_settings

logger = logging.getLogger("dspscanner")

_OCR_LIMIT = max(1, os.cpu_count() or 2)
_OCR_SEMAPHORE = threading.BoundedSemaphore(_OCR_LIMIT)
_OCR_CHUNK = 4
_OCR_MAX_PIXELS = 40_000_000
_OCR_TARGET_DPI = 250
_ocr_lang_lock = threading.Lock()
_ocr_lang_resolved: Optional[str] = None
_ocr_lang_done = False
_ocr_lang_command: Optional[str] = None
_CID_RE = re.compile(r"\(cid:\d+\)")

if _OCR_LIMIT > 1:
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")


def configure_ocr_limit(limit: int) -> None:
    """Ограничивает параллелизм OCR в текущем процессе.

    Используется из изолированного процесса обработки PDF, чтобы несколько
    PDF одновременно не создавали ``N файлов × N ядер`` процессов Tesseract.
    """
    global _OCR_LIMIT, _OCR_SEMAPHORE, _OCR_CHUNK
    _OCR_LIMIT = max(1, int(limit))
    _OCR_SEMAPHORE = threading.BoundedSemaphore(_OCR_LIMIT)
    _OCR_CHUNK = min(4, _OCR_LIMIT)
    if _OCR_LIMIT > 1:
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")


def _tesseract_environment(tesseract_bin: str) -> dict[str, str]:
    """Формирует окружение для системной и переносимой копии Tesseract."""
    env = os.environ.copy()
    executable = Path(tesseract_bin).resolve()
    candidates = (
        executable.parent / "tessdata",
        executable.parent.parent / "tessdata",
    )
    for candidate in candidates:
        if candidate.is_dir():
            # Tesseract 5 принимает путь к каталогу tessdata. Не переопределяем
            # явную пользовательскую настройку окружения.
            env.setdefault("TESSDATA_PREFIX", str(candidate))
            break
    return env


def _run_tesseract(
    tesseract_bin: str,
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    """Безопасно запускает Tesseract напрямую, без pytesseract/Pillow."""
    kwargs: dict = {
        "input": input_bytes,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
        "env": _tesseract_environment(tesseract_bin),
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run([tesseract_bin, *arguments], **kwargs)


def _resolve_ocr_lang(tesseract_bin: str) -> Optional[str]:
    global _ocr_lang_resolved, _ocr_lang_done, _ocr_lang_command
    command = str(Path(tesseract_bin))
    with _ocr_lang_lock:
        if _ocr_lang_done and _ocr_lang_command == command:
            return _ocr_lang_resolved
        _ocr_lang_resolved = None
        _ocr_lang_done = False
        _ocr_lang_command = command
        try:
            completed = _run_tesseract(
                command,
                ["--list-langs"],
                timeout=15,
            )
            output = completed.stdout.decode("utf-8", errors="replace")
            available = {
                line.strip()
                for line in output.splitlines()
                if line.strip() and not line.lower().startswith("list of available languages")
            }
            if {"rus", "eng"}.issubset(available):
                _ocr_lang_resolved = "rus+eng"
            elif "rus" in available:
                _ocr_lang_resolved = "rus"
            elif "eng" in available:
                _ocr_lang_resolved = "eng"
            logger.info(
                "Tesseract: определён язык OCR: %s (доступно: %s)",
                _ocr_lang_resolved,
                ", ".join(sorted(available)) or "ничего",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Не удалось получить список языков Tesseract: %s", exc)
        _ocr_lang_done = True
        return _ocr_lang_resolved



def _safe_ocr_dpi(page) -> int:
    """Подбирает DPI так, чтобы растр страницы не раздувал память процесса."""
    try:
        width_points = max(1.0, float(page.rect.width))
        height_points = max(1.0, float(page.rect.height))
        max_dpi = int(72.0 * math.sqrt(_OCR_MAX_PIXELS / (width_points * height_points)))
        return max(1, min(_OCR_TARGET_DPI, max_dpi))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return _OCR_TARGET_DPI

def _looks_garbled(page_text: str) -> bool:
    sample = page_text[:4000]
    stripped = sample.strip()
    if len(stripped) < 40:
        return False
    if _CID_RE.search(sample):
        return True
    if sample.count("\ufffd") > len(stripped) * 0.10:
        return True
    alnum = sum(1 for char in stripped if char.isalnum())
    return alnum / len(stripped) < 0.15


class PdfReader(BaseReader):
    name = "pdf"

    def extract_text(self, path: Path, use_ocr: bool = True, max_pages: int = 0) -> ReaderOutcome:
        import errno

        try:
            import fitz
        except ImportError:
            return ReaderOutcome(error="Библиотека PyMuPDF (fitz) не установлена")

        try:
            with fitz.open(str(path)) as document:
                if document.is_encrypted and not document.authenticate(""):
                    return ReaderOutcome(error="PDF защищён паролем")

                page_texts: dict[int, str] = {}
                ocr_pages: list[int] = []
                garbled_pages: set[int] = set()

                page_count = len(document)
                effective_count = page_count
                if max_pages > 0:
                    effective_count = min(page_count, max(1, int(max_pages)))

                for page_index in range(effective_count):
                    page = document[page_index]
                    page_text = page.get_text("text") or ""
                    page_texts[page_index] = page_text
                    if not page_text.strip():
                        ocr_pages.append(page_index)
                    elif _looks_garbled(page_text):
                        garbled_pages.add(page_index)
                        ocr_pages.append(page_index)

                ocr_used = False
                ocr_warning: Optional[str] = None
                if ocr_pages and use_ocr:
                    ocr_texts, ocr_used, ocr_warning = self._ocr_pages(document, ocr_pages, path.name)
                    for page_index, ocr_text in ocr_texts.items():
                        # Для пустого или явно битого текстового слоя OCR является
                        # более полезным представлением. Вставляем его на исходную
                        # позицию страницы, а не в конец документа.
                        if page_index in garbled_pages or not page_texts.get(page_index, "").strip():
                            page_texts[page_index] = ocr_text

                ordered = [page_texts[index].strip() for index in sorted(page_texts) if page_texts[index].strip()]
                full_text = "\n\n".join(ordered).strip()

            if not full_text:
                if not use_ocr:
                    return ReaderOutcome(
                        text=None,
                        warning="PDF не содержит текстового слоя (скан). Включите OCR в настройках.",
                    )
                warning = "PDF не содержит текста и OCR не дал результатов."
                if ocr_warning:
                    warning += f" Ошибка OCR: {ocr_warning}"
                return ReaderOutcome(text=None, warning=warning)

            if max_pages > 0 and page_count > effective_count:
                logger.debug(
                    "PDF %s: обработаны первые %d из %d страниц",
                    path.name,
                    effective_count,
                    page_count,
                )
            method = "pymupdf+ocr" if ocr_used else "pymupdf"
            if max_pages > 0 and page_count > effective_count:
                method += f"-first-{effective_count}-pages"
            return ReaderOutcome(
                text=full_text,
                warning=ocr_warning,
                method_used=method,
            )
        except Exception as exc:
            if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM):
                return ReaderOutcome(locked=True, error="Файл заблокирован")
            return ReaderOutcome(error=f"Ошибка чтения PDF: {exc}")

    @staticmethod
    def _ocr_pages(
        document,
        page_indices: list[int],
        doc_name: str,
    ) -> tuple[dict[int, str], bool, Optional[str]]:
        settings = load_settings()
        tesseract_bin = settings.tesseract_path
        if not tesseract_bin or not Path(tesseract_bin).is_file():
            _, detected = autodetect_external_programs()
            if detected:
                tesseract_bin = detected
        if not tesseract_bin:
            return {}, False, "Исполняемый файл Tesseract не найден в системе"

        # Tesseract запускается напрямую через CLI. Поэтому переносимой папки
        # Tesseract-OCR достаточно: отдельные Python-пакеты pytesseract и Pillow
        # больше не являются обязательными и не могут блокировать OCR.
        language = _resolve_ocr_lang(tesseract_bin)
        page_timeout = max(5, min(settings.per_file_timeout, 120))

        def ocr_png(png_bytes: bytes) -> str:
            with _OCR_SEMAPHORE:
                cascade = (language,) if language else ("rus+eng", "rus", "eng")
                errors: list[str] = []
                for lang in cascade:
                    try:
                        completed = _run_tesseract(
                            tesseract_bin,
                            ["stdin", "stdout", "-l", lang, "--psm", "3"],
                            input_bytes=png_bytes,
                            timeout=page_timeout,
                        )
                    except subprocess.TimeoutExpired:
                        errors.append(f"{lang}: превышен таймаут {page_timeout} с")
                        continue
                    except OSError as exc:
                        errors.append(f"{lang}: не удалось запустить Tesseract: {exc}")
                        continue

                    stdout = completed.stdout.decode("utf-8", errors="replace")
                    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
                    if completed.returncode == 0:
                        return stdout
                    details = stderr or f"код завершения {completed.returncode}"
                    errors.append(f"{lang}: {details}")
                raise RuntimeError("Ошибка OCR: " + "; ".join(errors))

        page_texts: dict[int, str] = {}
        failures: list[str] = []
        workers = min(_OCR_CHUNK, _OCR_LIMIT, max(1, len(page_indices)))

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dsp-ocr") as pool:
            for chunk_start in range(0, len(page_indices), _OCR_CHUNK):
                chunk = page_indices[chunk_start:chunk_start + _OCR_CHUNK]
                futures = {}
                for page_index in chunk:
                    try:
                        page = document[page_index]
                        dpi = _safe_ocr_dpi(page)
                        if dpi < _OCR_TARGET_DPI:
                            logger.debug(
                                "OCR страницы %d файла %s: DPI снижен с %d до %d для ограничения памяти",
                                page_index + 1,
                                doc_name,
                                _OCR_TARGET_DPI,
                                dpi,
                            )
                        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                        png_bytes = pixmap.tobytes("png")
                        # Запускаем OCR сразу после рендеринга каждой страницы.
                        # Пока PyMuPDF готовит следующую страницу, Tesseract уже
                        # обрабатывает предыдущую. Размер чанка (не более 4)
                        # по-прежнему жёстко ограничивает число PNG в памяти.
                        futures[pool.submit(ocr_png, png_bytes)] = page_index
                    except Exception as exc:
                        failures.append(f"стр. {page_index + 1}: рендеринг: {exc}")
                        logger.debug(
                            "Сбой рендеринга страницы %d файла %s: %s",
                            page_index + 1,
                            doc_name,
                            exc,
                        )

                for future in as_completed(futures):
                    page_index = futures[future]
                    try:
                        page_text = future.result()
                        if page_text.strip():
                            page_texts[page_index] = page_text
                    except Exception as exc:
                        failures.append(f"стр. {page_index + 1}: {exc}")
                        logger.debug(
                            "Сбой OCR на странице %d файла %s: %s",
                            page_index + 1,
                            doc_name,
                            exc,
                        )

        warning = None
        if failures:
            shown = "; ".join(failures[:3])
            suffix = f"; ещё ошибок: {len(failures) - 3}" if len(failures) > 3 else ""
            warning = f"OCR обработал не все страницы: {shown}{suffix}"
        return page_texts, bool(page_texts), warning

