"""PDF: single-owner MuPDF rendering and bounded parallel Tesseract processes."""
from __future__ import annotations

import csv
import errno
import io
import logging
import math
import os
import re
import subprocess
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.readers.base import BaseReader, ReaderOutcome
from app.settings_store import autodetect_external_programs, load_settings

logger = logging.getLogger("dspscanner")
_OCR_LIMIT = min(8, max(1, os.cpu_count() or 1))
_OCR_MAX_PIXELS = 40_000_000
_OCR_TARGET_DPI = 300
_OCR_RASTER_BUDGET = 128 * 1024 * 1024
_CID_RE = re.compile(r"\(cid:\d+\)")
_PROGRESS_CALLBACK = None
_CAPACITY_CALLBACK = None
OCR_MODEL_TIERS = {"fast": "tessdata-fast", "medium": "tessdata-medium", "best": "tessdata-best"}
_DEFAULT_OCR_TIER = "fast"


def configure_ocr_limit(limit: int, progress_callback=None, capacity_callback=None) -> None:
    """Configure an isolated process. Callbacks are invoked only by the owner thread."""
    global _OCR_LIMIT, _PROGRESS_CALLBACK, _CAPACITY_CALLBACK
    _OCR_LIMIT = max(1, min(32, int(limit)))
    _PROGRESS_CALLBACK = progress_callback
    _CAPACITY_CALLBACK = capacity_callback


def _progress(index: int, text: str) -> None:
    if _PROGRESS_CALLBACK is not None:
        _PROGRESS_CALLBACK(index, text)


def _has_models(directory: Path) -> bool:
    return directory.is_dir() and any(directory.glob("*.traineddata"))


def _ocr_tessdata_dir(tesseract_bin: str, tier: str) -> Optional[Path]:
    executable = Path(tesseract_bin).resolve()
    for base in (executable.parent, executable.parent.parent):
        candidate = base / OCR_MODEL_TIERS.get(tier, OCR_MODEL_TIERS[_DEFAULT_OCR_TIER])
        if _has_models(candidate):
            return candidate
    prefix = os.environ.get("TESSDATA_PREFIX")
    if prefix:
        return Path(prefix).expanduser()
    for base in (executable.parent, executable.parent.parent):
        candidate = base / "tessdata"
        if _has_models(candidate):
            return candidate
    # Let a system installation use its compiled-in model directory.
    return None


def _tesseract_environment(tesseract_bin: str, tier: str = _DEFAULT_OCR_TIER) -> dict[str, str]:
    env = os.environ.copy()
    directory = _ocr_tessdata_dir(tesseract_bin, tier)
    if directory is not None:
        env["TESSDATA_PREFIX"] = str(directory)
    env["OMP_THREAD_LIMIT"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    return env


def _run_tesseract(tesseract_bin: str, arguments: list[str], *, input_bytes: bytes | None = None,
                   timeout: int, tier: str = _DEFAULT_OCR_TIER) -> subprocess.CompletedProcess[bytes]:
    kwargs = dict(input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  timeout=timeout, check=False, env=_tesseract_environment(tesseract_bin, tier))
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run([str(tesseract_bin), *arguments], **kwargs)


def _resolve_ocr_lang(tesseract_bin: str, russian_only: bool = True,
                      tier: str = _DEFAULT_OCR_TIER) -> str:
    # Once per PDF; do not retain stale language lists or failed configuration probes.
    result = _run_tesseract(tesseract_bin, ["--list-langs"], timeout=15, tier=tier)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or "Не удалось прочитать языки")
    available = set(result.stdout.decode("utf-8", errors="replace").splitlines())
    wanted = ("rus",) if russian_only else ("rus", "eng")
    missing = [lang for lang in wanted if lang not in available]
    if missing:
        raise RuntimeError("Отсутствуют модели OCR: " + ", ".join(missing))
    return "+".join(wanted)


def _safe_ocr_dpi(page, target_dpi: int = _OCR_TARGET_DPI) -> int:
    width, height = float(page.rect.width), float(page.rect.height)
    if not math.isfinite(width * height) or width <= 0 or height <= 0:
        raise ValueError("Недопустимый размер страницы PDF")
    dpi = min(target_dpi, int(72 * math.sqrt(_OCR_MAX_PIXELS / (width * height))))
    while dpi > 0 and math.ceil(width * dpi / 72) * math.ceil(height * dpi / 72) > _OCR_MAX_PIXELS:
        dpi -= 1
    if dpi < 1:
        raise ValueError("Страница слишком велика для безопасного OCR")
    return dpi


def _looks_garbled(text: str) -> bool:
    text = text[:4000].strip()
    if not text:
        return False
    if _CID_RE.search(text) or text.count("\ufffd") > len(text) * .1:
        return True
    return len(text) >= 40 and sum(c.isalnum() for c in text) / len(text) < .15


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float = 0.0
    low_fraction: float = 1.0
    word_count: int = 0
    median_height: float = 0.0

    @property
    def needs_retry(self) -> bool:
        # Уверенность < 85 сама по себе — НЕ повод для повторного прохода:
        # у чистых сканов газет 73-83% является нормой (замер на реальном
        # корпусе: adaptive 200->300 vs один проход 150 DPI давали идентичные
        # совпадения при +40% времени). Ретрай остаётся для действительно
        # подозрительных страниц: пустой текст, галлюцинации cid/мусор,
        # >15% слов с conf<60, мелкие глифы (upscale может помочь).
        return (not self.text.strip() or self.low_fraction > .15
                or self.median_height < 12 or _looks_garbled(self.text))

    @property
    def score(self) -> tuple:
        return (bool(self.text.strip()), not _looks_garbled(self.text),
                self.confidence * (1 - self.low_fraction), self.word_count)


def _parse_tsv(data: bytes) -> OcrResult:
    rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")),
                          delimiter="\t", quoting=csv.QUOTE_NONE)
    required = {"level", "conf", "text", "height", "block_num", "par_num", "line_num"}
    if not rows.fieldnames or not required.issubset(rows.fieldnames):
        raise ValueError("Tesseract вернул некорректный TSV")
    lines, confidences, heights = [], [], []
    previous = None
    for row in rows:
        if row["level"] != "5" or not (row.get("text") or "").strip():
            continue
        conf, height = float(row["conf"]), float(row["height"])
        if not math.isfinite(conf) or not 0 <= conf <= 100 or not math.isfinite(height) or height < 0:
            raise ValueError("Некорректные метрики TSV")
        key = (row["block_num"], row["par_num"], row["line_num"])
        if previous is not None:
            lines.append(" " if key == previous else ("\n" if key[:2] == previous[:2] else "\n\n"))
        lines.append(row["text"].strip())
        previous = key
        confidences.append(conf)
        heights.append(height)
    if not confidences:
        return OcrResult("")
    return OcrResult("".join(lines), sum(confidences) / len(confidences),
                     sum(c < 60 for c in confidences) / len(confidences),
                     len(confidences), sorted(heights)[len(heights) // 2])


def _ocr_image(binary: str, png: bytes, language: str, tier: str, timeout: int, psm: int = 3) -> OcrResult:
    # A variable avoids requiring tessdata/configs/tsv in minimal portable bundles.
    result = _run_tesseract(binary, ["stdin", "stdout", "-l", language, "--oem", "1", "--psm", str(psm),
                                    "-c", "tessedit_create_tsv=1"],
                            input_bytes=png, timeout=timeout, tier=tier)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"Tesseract завершился с кодом {result.returncode}")
    return _parse_tsv(result.stdout)


def _warning(messages: list[str]) -> Optional[str]:
    if not messages:
        return None
    return "; ".join(messages[:8]) + (f"; ещё замечаний: {len(messages) - 8}" if len(messages) > 8 else "")


def _merge_native_ocr(native: str, ocr: str, duplicate_source: str | None = None) -> str:
    if not native.strip() or _looks_garbled(native):
        return ocr or native
    if not ocr.strip():
        return native
    normalized = " ".join((native if duplicate_source is None else duplicate_source).casefold().split())
    extra = [line for line in ocr.splitlines() if line.strip()
             and " ".join(line.casefold().split()) not in normalized]
    return native.rstrip() + ("\n\n" + "\n".join(extra) if extra else "")


class PdfReader(BaseReader):
    name = "pdf"

    def extract_text(self, path: Path, use_ocr: bool = True, max_pages: int = 0) -> ReaderOutcome:
        try:
            import fitz
        except ImportError:
            return ReaderOutcome(error="Библиотека PyMuPDF (fitz) не установлена")
        texts, warnings = {}, []
        ocr_used = False
        try:
            with fitz.open(str(path)) as document:
                if document.is_encrypted and not document.authenticate(""):
                    return ReaderOutcome(error="PDF защищён паролем")
                count = min(len(document), max_pages) if max_pages > 0 else len(document)
                pending, regions = [], {}
                settings = load_settings()
                for index in range(count):
                    try:
                        page = document[index]
                        text = page.get_text("text") or ""
                        texts[index] = text
                        _progress(index, text)
                        if not use_ocr:
                            continue
                        if settings.ocr_force:
                            pending.append(index)
                        elif settings.ocr_quality == "a2fast":
                            # A2fast (алгоритм DSP Scanner 2.9.7): OCR нужен только
                            # страницам с пустым или битым текстовым слоем; страница
                            # с нормальным текстом не распознаётся вообще.
                            if not text.strip() or _looks_garbled(text):
                                pending.append(index)
                        elif not text.strip() or _looks_garbled(text):
                            pending.append(index)
                        else:
                            image_info = page.get_image_info()
                            if image_info and page.rotation:
                                pending.append(index)
                                continue
                            boxes = []
                            for info in image_info:
                                box = fitz.Rect(info["bbox"]) & page.rect
                                if not box.is_empty and not box.is_infinite:
                                    boxes.append(box)
                            if boxes:
                                merged = []
                                for box in boxes:
                                    # Restart after each union to merge transitively overlapping masks/images.
                                    while True:
                                        overlap = next((r for r in merged if box.intersects(r)), None)
                                        if overlap is None:
                                            break
                                        box |= overlap
                                        merged.remove(overlap)
                                    merged.append(box)
                                pending.append(index)
                                if not page.rotation:
                                    regions[index] = merged
                    except Exception as exc:
                        warnings.append(f"стр. {index + 1}: чтение: {exc}")
                        if use_ocr and index not in pending:
                            pending.append(index)
                if pending and use_ocr:
                    recognized, ocr_used, warning = self._ocr_pages(document, pending, path.name,
                                                                   regions=regions, native_texts=texts)
                    texts.update(recognized)
                    if warning:
                        warnings.append(warning)
                text = "\n\n".join(texts[i].strip() for i in sorted(texts) if texts[i].strip())
                if not text:
                    warnings.append("PDF не содержит извлечённого текста" +
                                    ("; OCR выключен" if not use_ocr else "; проверьте страницы вручную"))
                method = "pymupdf+ocr" if ocr_used else "pymupdf"
                if count < len(document):
                    method += f"-first-{count}-pages"
                return ReaderOutcome(text=text or None, warning=_warning(warnings), method_used=method)
        except Exception as exc:
            if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM):
                return ReaderOutcome(locked=True, error="Файл заблокирован")
            return ReaderOutcome(error=f"Ошибка чтения PDF: {exc}")

    @staticmethod
    def _ocr_pages(document, page_indices: list[int], doc_name: str, *, regions=None,
                   native_texts=None) -> tuple[dict[int, str], bool, Optional[str]]:
        import fitz

        settings = load_settings()
        binary = settings.tesseract_path
        if not binary or not Path(binary).is_file():
            _, binary = autodetect_external_programs()
        if not binary or not Path(binary).is_file():
            return {}, False, "Исполняемый файл Tesseract не найден"
        tier, warnings = settings.ocr_model_tier, []
        directory = _ocr_tessdata_dir(binary, tier)
        if directory is None or directory.name != OCR_MODEL_TIERS[tier]:
            warnings.append(f"Модель {tier} не подтверждена: используется {directory or 'системный tessdata'}")
        try:
            language = _resolve_ocr_lang(binary, settings.russian_only, tier)
            # Validate engine/model compatibility once, before rendering any PDF pages.
            probe = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 32, 32), False)
            probe.clear_with(255)
            _ocr_image(binary, probe.tobytes("png"), language, tier, 15)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            warnings.append(f"OCR недоступен: {exc}")
            return {}, False, _warning(warnings)
        logger.info("OCR %s: язык=%s, модель=%s, каталог=%s, режим=%s", doc_name, language,
                    tier, directory or "системный", settings.ocr_quality)
        regions, native_texts = regions or {}, native_texts or {}
        # Only native text overlapping an image can duplicate that image's OCR.
        # Identical words elsewhere on the page are distinct occurrences.
        duplicate_sources = {}
        for index, boxes in regions.items():
            try:
                duplicate_sources[index] = "\n".join(document[index].get_text("text", clip=box) for box in boxes)
            except Exception as exc:
                warnings.append(f"стр. {index + 1}: сопоставление текстового слоя: {exc}")
                duplicate_sources[index] = ""
        first_dpi = {"thorough": 300, "fast150": 150, "a2fast": 250}.get(settings.ocr_quality, 200)
        jobs = deque((index, part, box, first_dpi, None) for index in page_indices
                     for part, box in enumerate(regions.get(index, [None])))
        remaining = {index: len(regions.get(index, [None])) for index in page_indices}
        pieces = {index: {} for index in page_indices}
        output = {}
        workers = min(_OCR_LIMIT, settings.ocr_workers or 8, max(1, len(jobs)))
        if _CAPACITY_CALLBACK is not None:
            _CAPACITY_CALLBACK(workers)
        futures, retained_bytes = {}, 0

        def finish(index, part, result):
            pieces[index][part] = result.text
            remaining[index] -= 1
            if remaining[index] == 0:
                ocr = "\n\n".join(pieces[index][i] for i in sorted(pieces[index]))
                combined = _merge_native_ocr(native_texts.get(index, ""), ocr, duplicate_sources.get(index))
                output[index] = combined
                _progress(index, combined)

        # Only this caller thread accesses MuPDF; workers receive immutable bytes.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dsp-ocr") as pool:
            while jobs or futures:
                while jobs and len(futures) < workers:
                    index, part, box, target, previous = jobs[0]
                    popped = False
                    try:
                        page = document[index]
                        rect = box if box is not None else page.rect
                        dpi = _safe_ocr_dpi(page, target)
                        estimate = math.ceil(rect.width * dpi / 72) * math.ceil(rect.height * dpi / 72)
                        if futures and retained_bytes + estimate > _OCR_RASTER_BUDGET:
                            break
                        jobs.popleft()
                        popped = True
                        pixmap = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False, clip=box)
                        png = pixmap.tobytes("png")
                        del pixmap
                        if dpi < target:
                            warnings.append(f"стр. {index + 1}: DPI снижен до {dpi} из-за размера страницы")
                        psm = 11 if previous is not None and not previous.text.strip() else 3
                        future = pool.submit(_ocr_image, binary, png, language, tier, settings.ocr_page_timeout, psm)
                        futures[future] = (index, part, box, dpi, previous, estimate)
                        retained_bytes += estimate
                        del png
                    except Exception as exc:
                        if not popped:
                            jobs.popleft()
                        warnings.append(f"стр. {index + 1}: рендеринг: {exc}")
                        finish(index, part, previous or OcrResult(""))
                if not futures:
                    continue
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    index, part, box, dpi, previous, estimate = futures.pop(future)
                    retained_bytes -= estimate
                    try:
                        result = future.result()
                    except Exception as exc:
                        warnings.append(f"стр. {index + 1}: OCR: {exc}")
                        finish(index, part, previous or OcrResult(""))
                        continue
                    if (settings.ocr_quality not in ("fast150", "a2fast")
                            and previous is None and result.needs_retry and (dpi < 300 or not result.text.strip())):
                        jobs.appendleft((index, part, box, 300, result))
                        continue
                    if previous is not None and previous.score > result.score:
                        result = previous
                    if not result.text.strip():
                        warnings.append(f"стр. {index + 1}: OCR не обнаружил текста; полнота не подтверждена")
                    elif result.needs_retry:
                        warnings.append(f"стр. {index + 1}: низкая уверенность OCR ({result.confidence:.0f}%)")
                    finish(index, part, result)
        return output, any(pieces[i][p].strip() for i in pieces for p in pieces[i]), _warning(warnings)
