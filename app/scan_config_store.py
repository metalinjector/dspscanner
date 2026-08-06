"""Переносимое автоматическое хранение параметров поиска в JSON.

Основной файл располагается в папке ``DSPScanner_Config`` рядом с программой.
Эту папку можно перенести на другой компьютер вместе с приложением: при
следующем запуске поля поиска будут восстановлены из ``search_settings.json``.
Если каталог программы недоступен для записи (например, Program Files),
используется резервный каталог в профиле пользователя.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

_CONFIG_DIR_NAME = "DSPScanner_Config"
_CONFIG_FILE_NAME = "search_settings.json"
_MAX_CONFIG_BYTES = 5_000_000


def application_root() -> Path:
    """Возвращает папку исполняемого файла либо корень исходного проекта."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def portable_config_dir() -> Path:
    override = os.environ.get("DSP_SCANNER_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return application_root() / _CONFIG_DIR_NAME


def fallback_config_dir() -> Path:
    return Path.home() / "DSPScanner" / _CONFIG_DIR_NAME


def ensure_config_dir() -> Path:
    """Создаёт специальную папку настроек и возвращает доступный каталог.

    Сначала используется переносимая папка рядом с программой. Если она
    недоступна для создания/записи, создаётся папка в профиле пользователя.
    """
    for directory in (portable_config_dir(), fallback_config_dir()):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Проверка реальной возможности атомарной записи, а не только os.access.
            fd, probe_name = tempfile.mkstemp(prefix=".write_probe_", dir=directory)
            os.close(fd)
            Path(probe_name).unlink(missing_ok=True)
            return directory
        except OSError:
            continue
    # Последняя попытка даст вызывающему коду понятную ошибку при сохранении.
    directory = fallback_config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def auto_config_path() -> Path:
    """Путь для автоматического сохранения текущей конфигурации."""
    return ensure_config_dir() / _CONFIG_FILE_NAME


def existing_auto_config_path() -> Path | None:
    """Возвращает наиболее свежий доступный автоматический JSON.

    Обычно существует только переносимая копия рядом с программой. Если эта
    папка стала read-only, новые изменения сохраняются в профиль пользователя;
    выбор по времени изменения предотвращает загрузку устаревшей копии.
    """
    candidates = [
        path for path in (
            portable_config_dir() / _CONFIG_FILE_NAME,
            fallback_config_dir() / _CONFIG_FILE_NAME,
        )
        if path.is_file()
    ]
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda item: item.stat().st_mtime_ns)
    except OSError:
        return candidates[0]


def load_scan_config(path: Path | str | None = None) -> dict[str, Any] | None:
    target = Path(path) if path is not None else existing_auto_config_path()
    if target is None or not target.is_file():
        return None
    if target.stat().st_size > _MAX_CONFIG_BYTES:
        raise ValueError("Файл конфигурации слишком велик")
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Корень конфигурации должен быть JSON-объектом")
    return data


def save_scan_config(config: Mapping[str, Any], path: Path | str | None = None) -> Path:
    """Атомарно сохраняет JSON и возвращает фактический путь."""
    target = Path(path) if path is not None else auto_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file_handle:
            json.dump(dict(config), file_handle, ensure_ascii=False, indent=2)
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temp_path, target)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target
