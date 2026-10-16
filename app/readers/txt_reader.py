"""
Чтение TXT-файлов.

Логика полностью повторяет и улучшает `Get-TxtText` из dspscanner.ps1:
    1. Определение BOM (UTF-8 / UTF-16 LE / UTF-16 BE);
    2. Без BOM проверяем структуру UTF-16/32, затем строгий UTF-8.
    3. Legacy-кодировки определяем эвристически с явным предупреждением.
       Для короткой неоднозначной строки кириллица предпочтительнее CJK;
       произвольную кодировку без метаданных гарантировать нельзя.
"""
from __future__ import annotations

from pathlib import Path
import re

from app.readers.base import BaseReader, ReaderOutcome

try:
    from charset_normalizer import from_bytes as _cn_from_bytes
except ImportError:  # pragma: no cover
    _cn_from_bytes = None

_CONTROL_RE = re.compile(rb"[\x00-\x08\x0e-\x1f]")
_CYRILLIC_ENCODINGS = ("cp1251", "koi8_r", "cp866")


def _bomless_unicode(data: bytes):
    """Conservative UTF-16/32 signature: repeated high bytes, strict decoding."""
    sample = data[:4096]
    if b"\x00" not in sample and b"\x03" not in sample and b"\x04" not in sample:
        return None
    for encoding, width, high in (
        ("utf-32-le", 4, (2, 3)), ("utf-32-be", 4, (0, 1)),
        ("utf-16-le", 2, (1,)), ("utf-16-be", 2, (0,)),
    ):
        if len(data) < width * 2 or len(data) % width:
            continue
        high_bytes = [sample[i] for i in range(len(sample)) if i % width in high]
        allowed = {0} if width == 4 else {0, 3, 4}  # Latin/Greek/Cyrillic BMP text
        if sum(b in allowed for b in high_bytes) / len(high_bytes) < 0.85:
            continue
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if all(c.isprintable() or c in "\r\n\t\f" for c in text):
            return text, encoding
    return None


class TxtReader(BaseReader):
    name = "txt"

    def extract_text(self, path: Path) -> ReaderOutcome:
        import errno

        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                return ReaderOutcome(locked=True, error="Файл заблокирован")
            return ReaderOutcome(error=f"Не удалось прочитать файл: {exc}")
        if not data:
            return ReaderOutcome(text=None, warning="Пустой файл")

        try:
            if data[:3] == b"\xef\xbb\xbf":
                return ReaderOutcome(text=data[3:].decode("utf-8", errors="replace"), method_used="utf-8-bom")
            # UTF-32 BOM проверяется раньше UTF-16: FF FE 00 00 иначе совпадёт
            # с более коротким UTF-16LE BOM (FF FE) и файл будет декодирован
            # неверно (лишний \x00 через символ).
            if data[:4] == b"\xff\xfe\x00\x00":
                return ReaderOutcome(text=data[4:].decode("utf-32-le", errors="replace"), method_used="utf-32-le")
            if data[:4] == b"\x00\x00\xfe\xff":
                return ReaderOutcome(text=data[4:].decode("utf-32-be", errors="replace"), method_used="utf-32-be")
            if data[:2] == b"\xff\xfe":
                return ReaderOutcome(text=data[2:].decode("utf-16-le", errors="replace"), method_used="utf-16-le")
            if data[:2] == b"\xfe\xff":
                return ReaderOutcome(text=data[2:].decode("utf-16-be", errors="replace"), method_used="utf-16-be")

            unicode_guess = _bomless_unicode(data)
            if unicode_guess:
                text, encoding = unicode_guess
                return ReaderOutcome(text=text, method_used=f"detected:{encoding}",
                                     warning=f"UTF без BOM: предполагается {encoding}")

            # Form-feed / tab in valid UTF-8 must not trigger an expensive guess.
            if b"\x00" not in data and not _CONTROL_RE.search(data[:4096]):
                try:
                    return ReaderOutcome(text=data.decode("utf-8"), method_used="utf-8")
                except UnicodeDecodeError:
                    pass

            if _cn_from_bytes is not None:
                best = _cn_from_bytes(data).best()
                if best is not None:
                    # On short Russian ANSI strings the universal detector can
                    # prefer Shift-JIS (e.g. CP1251 «Договор»). A Cyrillic prior
                    # resolves short, low-confidence guesses only. Do not replace
                    # confident Western/Unicode results globally.
                    ambiguous = best.encoding not in _CYRILLIC_ENCODINGS and best.coherence < 0.2
                    if len(data) <= 4096 and ambiguous:
                        russian = _cn_from_bytes(data, cp_isolation=list(_CYRILLIC_ENCODINGS)).best()
                        if russian is not None and sum("а" <= c.lower() <= "я" for c in str(russian)) >= 6:
                            best = russian
                    return ReaderOutcome(text=str(best), method_used=f"detected:{best.encoding}",
                                         warning=f"Кодировка определена эвристически: {best.encoding}; "
                                         "при искажениях пересохраните TXT в UTF-8")

            return ReaderOutcome(text=data.decode("cp1251", errors="replace"), method_used="cp1251-fallback",
                                 warning="Кодировка не определена: предполагается Windows-1251")
        except Exception as exc:  # noqa: BLE001
            return ReaderOutcome(error=f"Ошибка чтения TXT: {exc}")
