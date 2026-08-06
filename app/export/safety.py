"""Санитизация данных, извлечённых из недоверенных документов."""
from __future__ import annotations

import html
import re

_ILLEGAL_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def spreadsheet_text(value, max_length: int = 32_767) -> str:
    text = _ILLEGAL_XML_CHARS.sub("", str(value))[:max_length]
    if text.lstrip(" \t\r\n").startswith(_FORMULA_PREFIXES):
        # Защита от CSV/Excel formula injection. В Excel ведущий апостроф
        # помечает значение как текст и не отображается в ячейке.
        text = "'" + text
    return text


def markdown_text(value) -> str:
    text = html.escape(str(value), quote=False)
    return text.replace("\\", "\\\\").replace("`", "\\`")


def markdown_inline_code(value) -> str:
    text = html.escape(str(value), quote=False).replace("`", "ˋ")
    return f"`{text}`"
