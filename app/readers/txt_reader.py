"""
Чтение TXT-файлов.

Логика полностью повторяет и улучшает `Get-TxtText` из dspscanner.ps1:
    1. Определение BOM (UTF-8 / UTF-16 LE / UTF-16 BE);
    2. Если BOM отсутствует — сначала пробуем UTF-8, при обнаружении
       управляющих байт (признак не-UTF8 кириллицы) используем
       charset-normalizer для интеллектуального определения кодировки
       (в оригинале был жёстко прописан cp1251 — улучшение точности).
"""
from __future__ import annotations

from pathlib import Path

from app.readers.base import BaseReader, ReaderOutcome

try:
    from charset_normalizer import from_bytes as _cn_from_bytes
except ImportError:  # pragma: no cover
    _cn_from_bytes = None

_CONTROL_BYTES = set(range(0, 9)) | {11, 12} | set(range(14, 32))


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

            has_control = any(b in _CONTROL_BYTES for b in data[:4096])
            if not has_control:
                try:
                    return ReaderOutcome(text=data.decode("utf-8"), method_used="utf-8")
                except UnicodeDecodeError:
                    pass

            if _cn_from_bytes is not None:
                best = _cn_from_bytes(data).best()
                if best is not None:
                    return ReaderOutcome(text=str(best), method_used=f"detected:{best.encoding}")

            return ReaderOutcome(text=data.decode("cp1251", errors="replace"), method_used="cp1251-fallback")
        except Exception as exc:  # noqa: BLE001
            return ReaderOutcome(error=f"Ошибка чтения TXT: {exc}")
