"""Bounded, process-local text cache. Recognized content is never persisted to disk."""
from __future__ import annotations

import hashlib
import os
import sys
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock

from app.readers.pdf_reader import _ocr_tessdata_dir

_LIMIT = 64 * 1024 * 1024
_lock = Lock()
_entries = OrderedDict()
_size = 0

# Single-flight (дедупликация байтовых копий, как md5-дедуп в ocr_scanner_final):
# ключ → хендл в полёте. Второй файл с тем же содержимым ждёт первого и берёт
# его результат напрямую из хендла (даже с warnings, которые постоянный кэш
# не принимает), вместо параллельного повторного OCR (cache stampede).
class _Inflight:
    __slots__ = ("event", "outcome")

    def __init__(self):
        from threading import Event
        self.event = Event()
        self.outcome = None


_inflight: dict = {}
_inflight_lock = Lock()


def begin(key):
    """Занять ключ. Возвращает (handle, is_owner): владелец обязан вызвать
    end(key, handle, outcome); не-владелец ждёт handle.event и читает
    handle.outcome после срабатывания."""
    if key is None:
        return None, False
    with _inflight_lock:
        flight = _inflight.get(key)
        if flight is None:
            flight = _Inflight()
            _inflight[key] = flight
            return flight, True
        return flight, False


def end(key, flight, outcome=None) -> None:
    """Освободить ключ, опубликовать результат и разбудить ожидающих."""
    if key is None or flight is None:
        return
    if outcome is not None:
        flight.outcome = outcome
    with _inflight_lock:
        if _inflight.get(key) is flight:
            del _inflight[key]
    flight.event.set()


def cache_key(path, scan, app):
    try:
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        after = path.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino) != (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino):
            return None
        models = ()
        if scan.use_ocr_for_pdf:
            if not app.tesseract_path:
                return None
            binary = Path(app.tesseract_path).resolve()
            directory = _ocr_tessdata_dir(str(binary), app.ocr_model_tier)
            if directory is None:
                return None
            models = tuple((str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns)
                           for p in [binary, *sorted(directory.glob('*.traineddata'))])
        # Ключ НЕ включает путь файла: байтовые копии одного PDF (разные имена,
        # разные каталоги) делят одну запись кэша, и их OCR не выполняется дважды.
        # Идентичность гарантирует digest всего содержимого + неизменность stat
        # во время чтения; путь в ключе лишь дублировал одинаковые результаты.
        return ('ocr-v2', digest.hexdigest(), stat.st_size,
                scan.use_ocr_for_pdf, scan.limit_pdf_pages, scan.pdf_page_limit,
                app.russian_only, app.ocr_model_tier, app.ocr_quality, app.ocr_force,
                os.environ.get('TESSDATA_PREFIX'), models)
    except OSError:
        return None


def get(key):
    if key is None:
        return None
    with _lock:
        if key not in _entries:
            return None
        outcome, _ = _entries[key]
        _entries.move_to_end(key)
        return replace(outcome)


def put(key, outcome):
    global _size
    if key is None or not outcome.text or outcome.warning or outcome.error or outcome.locked:
        return
    size = sys.getsizeof(outcome.text) + sys.getsizeof(key)
    if size > _LIMIT:
        return
    with _lock:
        if key in _entries:
            _, old_size = _entries.pop(key)
            _size -= old_size
        while _entries and (_size + size > _LIMIT or len(_entries) >= 128):
            _, (_, old_size) = _entries.popitem(last=False)
            _size -= old_size
        _entries[key] = (replace(outcome), size)
        _size += size
