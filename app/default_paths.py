"""Системные пути поиска, используемые при первом запуске DSP Scanner.

На Windows набор состоит из профиля текущего пользователя и корней всех
подключённых логических дисков, кроме ``C:``.  Профиль добавляется отдельно,
поскольку сканировать весь системный диск C: по умолчанию слишком медленно и
может затронуть большое количество служебных каталогов.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def _windows_logical_drive_mask() -> int:
    """Возвращает битовую маску логических дисков Windows.

    Функция изолирована, чтобы её можно было безопасно тестировать и чтобы
    импорт модуля на Linux/macOS не обращался к ``ctypes.windll``.
    """
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetLogicalDrives())
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


def drive_roots_from_mask(mask: int, *, excluded_letters: set[str] | None = None) -> list[str]:
    """Преобразует маску ``GetLogicalDrives`` в корни вида ``D:\\``.

    По умолчанию исключается только системный диск ``C:``. Наличие диска уже
    подтверждено самой битовой маской, поэтому дополнительные обращения к
    каждому корню не выполняются — это предотвращает задержки на сетевых или
    съёмных накопителях.
    """
    excluded = {letter.upper() for letter in (excluded_letters or {"C"})}
    roots: list[str] = []
    safe_mask = max(0, int(mask))
    for index in range(26):
        if not safe_mask & (1 << index):
            continue
        letter = chr(ord("A") + index)
        if letter in excluded:
            continue
        roots.append(f"{letter}:\\")
    return roots


def default_search_paths(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    drive_mask: int | None = None,
    home: str | Path | None = None,
) -> list[str]:
    """Формирует переносимый набор путей поиска по умолчанию.

    На Windows первым путём всегда является ``%USERPROFILE%`` (с безопасным
    fallback на ``Path.home()``), затем добавляются все имеющиеся диски кроме
    ``C:``. На других ОС используется домашняя папка пользователя.
    """
    platform_value = (platform_name or os.name).lower()
    environment = os.environ if environ is None else environ

    if home is not None:
        profile = str(Path(home).expanduser())
    elif platform_value == "nt":
        profile = str(environment.get("USERPROFILE", "")).strip()
        if not profile:
            profile = str(Path.home())
    else:
        profile = str(Path.home())

    candidates = [profile] if profile else []
    if platform_value == "nt":
        mask = _windows_logical_drive_mask() if drive_mask is None else int(drive_mask)
        candidates.extend(drive_roots_from_mask(mask))

    # Устраняем дубли без изменения порядка. Для Windows сравнение делаем
    # регистронезависимым и одинаково обрабатываем завершающий слэш.
    result: list[str] = []
    seen: set[str] = set()
    for raw_path in candidates:
        value = str(raw_path).strip()
        if not value:
            continue
        key = value.replace("/", "\\").rstrip("\\").casefold() if platform_value == "nt" else value
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
