r"""Чтение PDF через PyMuPDF с опциональным Tesseract OCR."""
from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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


def configure_ocr_limit(limit: int) -> None:
    """Ограничивает параллелизм OCR в текущем процессе.

    Используется из изолированного процесса обработки PDF, чтобы несколько
    PDF одновременно не создавали ``N файлов × N ядер`` процессов Tesseract.
    """
    global _OCR_LIMIT, _OCR_SEMAPHORE, _OCR_CHUNK
    _OCR_LIMIT = max(1, int(limit))
    _OCR_SEMAPHORE = threading.BoundedSemaphore(_OCR_LIMIT)
    _OCR_CHUNK = min(4, _OCR_LIMIT)


# Градация моделей OCR: имя каталога с traineddata.
# fast — int8-модели (самые быстрые, точность чуть ниже);
# medium — стандартный tessdata: integer LSTM + legacy; используем только LSTM;
# best — float LSTM, ориентированные на точность (обычно медленнее).
OCR_MODEL_TIERS: dict[str, str] = {
    "fast": "tessdata-fast",
    "medium": "tessdata-medium",
    "best": "tessdata-best",
}
_DEFAULT_OCR_TIER = "fast"


def _ocr_tessdata_dir(tesseract_bin: str, tier: str) -> Optional[Path]:
    """Выбирает переносимые модели, не ломая системный путь Tesseract.

    None означает: сохранить TESSDATA_PREFIX пользователя либо использовать
    встроенный путь бинаря (например, /usr/share/tesseract-ocr/5/tessdata).
    Нельзя подставлять несуществующий /usr/bin/tessdata.
    """
    executable = Path(tesseract_bin).resolve()
    tier_dir_name = OCR_MODEL_TIERS.get(tier, OCR_MODEL_TIERS[_DEFAULT_OCR_TIER])
    directories = [
        base / name
        for name in (tier_dir_name, "tessdata")
        for base in (executable.parent, executable.parent.parent)
    ]
    # Оба поддерживаемых режима запрашивают русский (rus либо rus+eng).
    # English-only fast не должен затенять рабочий русский fallback.
    # При отсутствии rus всё же допускаем установку только с eng.
    for language in ("rus", "eng"):
        for directory in directories:
            if (directory / f"{language}.traineddata").is_file():
                return directory
    return None


def _tesseract_environment(tesseract_bin: str, tier: str = _DEFAULT_OCR_TIER) -> dict[str, str]:
    """Окружение только дочернего OCR: без изменения глобального os.environ."""
    env = os.environ.copy()
    directory = _ocr_tessdata_dir(tesseract_bin, tier)
    if directory is not None:
        env["TESSDATA_PREFIX"] = str(directory)
    # Параллелизм уже организован на уровне страниц/файлов. В том числе при
    # одном OCR-слоте внутри каждого из нескольких изолированных PDF-ридеров.
    env.setdefault("OMP_THREAD_LIMIT", "1")
    return env


def _run_tesseract(
    tesseract_bin: str,
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: int,
    tier: str = _DEFAULT_OCR_TIER,
) -> subprocess.CompletedProcess[bytes]:
    """Безопасно запускает Tesseract напрямую, без pytesseract/Pillow."""
    kwargs: dict = {
        "input": input_bytes,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
        "env": _tesseract_environment(tesseract_bin, tier),
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run([tesseract_bin, *arguments], **kwargs)


def _resolve_ocr_lang(
    tesseract_bin: str,
    russian_only: bool = True,
    tier: str = _DEFAULT_OCR_TIER,
) -> Optional[str]:
    """Определяет доступные языки OCR с учётом пользовательской настройки."""
    global _ocr_lang_resolved, _ocr_lang_done, _ocr_lang_command
    command = (
        str(Path(tesseract_bin).resolve()) + (":rus" if russian_only else ":full")
        + f":{tier}:" + _tesseract_environment(tesseract_bin, tier).get("TESSDATA_PREFIX", "")
    )
    with _ocr_lang_lock:
        if _ocr_lang_done and _ocr_lang_command == command:
            return _ocr_lang_resolved
        _ocr_lang_resolved = None
        _ocr_lang_done = False
        _ocr_lang_command = command
        try:
            completed = _run_tesseract(
                str(tesseract_bin),
                ["--list-langs"],
                timeout=15,
                tier=tier,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.decode("utf-8", errors="replace").strip())
            output = completed.stdout.decode("utf-8", errors="replace")
            available = {
                line.strip()
                for line in output.splitlines()
                if line.strip() and not line.lower().startswith("list of available languages")
            }
            # Если включён режим "только русский" - используем rus, иначе rus+eng
            if russian_only:
                # Строгий выбор: отсутствие rus не разрешает подменять язык на eng.
                _ocr_lang_resolved = "rus" if "rus" in available else None
            elif {"rus", "eng"}.issubset(available):
                _ocr_lang_resolved = "rus+eng"
            elif "rus" in available:
                _ocr_lang_resolved = "rus"
            elif "eng" in available:
                _ocr_lang_resolved = "eng"
            logger.info(
                "Tesseract: определён язык OCR: %s (доступно: %s, russian_only=%s)",
                _ocr_lang_resolved,
                ", ".join(sorted(available)) or "ничего",
                russian_only,
            )
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
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


def _render_ocr_page(document, page_index: int, target_dpi: int) -> tuple[bytes, int]:
    """Вызывается только в потоке-владельце document, не в OCR executor.

    PGM (серый PNM) не требует PNG-сжатия и поддерживается Leptonica.
    DPI передаём отдельно: в отличие от PNG, PGM не хранит разрешение.
    """
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz

    page = document[page_index]
    dpi = min(target_dpi, _safe_ocr_dpi(page))
    pixmap = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    return pixmap.tobytes("ppm"), dpi


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
            try:
                import pymupdf as fitz
            except ImportError:
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
        if not tesseract_bin or not Path(tesseract_bin).is_file():
            return {}, False, "Исполняемый файл Tesseract не найден в системе"

        started = time.perf_counter()
        tier = getattr(settings, "ocr_model_tier", _DEFAULT_OCR_TIER) or _DEFAULT_OCR_TIER
        directory = _ocr_tessdata_dir(tesseract_bin, tier)
        model_source = str(directory or os.environ.get("TESSDATA_PREFIX") or "системный путь Tesseract")
        warnings: list[str] = []
        if directory is None or directory.name != OCR_MODEL_TIERS.get(tier):
            warnings.append(
                f"Не выбран каталог {OCR_MODEL_TIERS.get(tier, tier)} с подходящими моделями; "
                f"используются модели: {model_source}. Градация {tier} не подтверждена."
            )
        language = _resolve_ocr_lang(
            tesseract_bin, russian_only=settings.russian_only, tier=tier,
        )
        if language is None:
            # Повтор на другом DPI не исправит отсутствие моделей/бинаря.
            warnings.append(
                "Принудительный OCR только на русском: модель rus недоступна; "
                "установите rus.traineddata. Переход на eng запрещён."
                if settings.russian_only else
                "Tesseract: не удалось найти доступные модели rus/eng; проверьте tessdata"
            )
            return {}, False, "; ".join(warnings)
        requested_language = "rus" if settings.russian_only else "rus+eng"
        if language != requested_language:
            warnings.append(f"Запрошен язык OCR {requested_language}, доступен только {language}")
        page_timeout = max(5, min(settings.per_file_timeout, 120))

        def ocr_image(image_bytes: bytes, dpi: int) -> tuple[str, float]:
            with _OCR_SEMAPHORE:
                ocr_started = time.perf_counter()
                try:
                    completed = _run_tesseract(
                        tesseract_bin,
                        ["stdin", "stdout", "-l", language, "--oem", "1", "--psm", "3", "--dpi", str(dpi)],
                        input_bytes=image_bytes,
                        timeout=page_timeout,
                        tier=tier,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(f"превышен таймаут OCR {page_timeout} с") from exc
                if completed.returncode != 0:
                    details = completed.stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(details or f"код завершения {completed.returncode}")
                return completed.stdout.decode("utf-8", errors="replace"), time.perf_counter() - ocr_started

        page_texts: dict[int, str] = {}
        failures: dict[int, str] = {}
        workers = min(_OCR_CHUNK, _OCR_LIMIT, max(1, len(page_indices)))
        render_seconds = ocr_seconds = 0.0
        attempts = 0

        def run_pass(pool, indices: list[int], dpi: int) -> None:
            nonlocal render_seconds, ocr_seconds, attempts
            pending = {}
            remaining = iter(indices)
            exhausted = False
            while pending or not exhausted:
                # Скользящее окно: не более workers изображений в полёте.
                # Освободился любой слот — сразу рендерим следующую страницу,
                # не ждём самую медленную страницу старой пачки.
                while len(pending) < workers and not exhausted:
                    try:
                        page_index = next(remaining)
                    except StopIteration:
                        exhausted = True
                        break
                    render_started = time.perf_counter()
                    try:
                        image_bytes, actual_dpi = _render_ocr_page(document, page_index, dpi)
                    except Exception as exc:
                        failures[page_index] = f"рендер: {exc}"
                        continue
                    finally:
                        render_seconds += time.perf_counter() - render_started
                    attempts += 1
                    pending[pool.submit(ocr_image, image_bytes, actual_dpi)] = page_index
                    del image_bytes
                if not pending:
                    continue
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    page_index = pending.pop(future)
                    try:
                        text, elapsed = future.result()
                        ocr_seconds += elapsed
                        if len(text.strip()) > len(page_texts.get(page_index, "").strip()):
                            page_texts[page_index] = text
                    except Exception as exc:
                        failures[page_index] = str(exc)

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dsp-ocr") as pool:
            run_pass(pool, page_indices, 150)
            # Короткий успешно распознанный текст — повод повысить DPI.
            # Ошибка процесса/таймаут — не повод повторять дорогой вызов.
            retry_pages = [
                idx for idx in page_indices
                if idx not in failures and len(page_texts.get(idx, "").strip()) < 30
            ]
            run_pass(pool, retry_pages, 250)

        if failures:
            shown = "; ".join(f"стр. {idx + 1}: {error}" for idx, error in sorted(failures.items())[:3])
            suffix = f"; ещё ошибок: {len(failures) - 3}" if len(failures) > 3 else ""
            warnings.append(f"OCR обработал не все страницы: {shown}{suffix}")
        logger.info(
            "OCR %s: tier=%s models=%s lang=%s workers=%d pages=%d retries=%d "
            "attempts=%d failures=%d render=%.3fs ocr_success_sum=%.3fs wall=%.3fs",
            doc_name, tier, model_source, language, workers, len(page_indices), len(retry_pages),
            attempts, len(failures), render_seconds, ocr_seconds, time.perf_counter() - started,
        )
        return page_texts, bool(page_texts), "; ".join(warnings) or None
