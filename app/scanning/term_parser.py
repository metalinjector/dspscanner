"""Разбор пользовательских списков поисковых терминов."""
from __future__ import annotations

import re

_SEPARATOR_RE = re.compile(r"[,;\n\r]+")


def parse_terms(raw_text: str) -> list[str]:
    """Разбирает термины, разделённые строками, запятыми или точкой с запятой.

    Обычный пробел не является разделителем: благодаря этому словосочетания
    вроде ``служебная записка`` сохраняются как один поисковый термин.
    Порядок сохраняется, точные дубликаты удаляются без учёта регистра и ``ё``.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for chunk in _SEPARATOR_RE.split(raw_text or ""):
        term = " ".join(chunk.strip().split())
        if not term:
            continue
        key = term.casefold().replace("ё", "е")
        if key in seen:
            continue
        seen.add(key)
        unique.append(term)
    return unique
