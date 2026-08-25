"""Хранилище параметров поиска: имя папки и совместимость со старым.

Папка переименована с ``DSPScanner_Config`` на ``DSPScanner-Config``.
Сохранённые параметры при этом должны пережить обновление, поэтому старое
имя продолжает читаться, но новые данные пишутся только в новую папку.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import scan_config_store as store


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """Переменная окружения перекрывает пути и мешает проверять их выбор."""
    monkeypatch.delenv("DSP_SCANNER_CONFIG_DIR", raising=False)


def _write_config(directory: Path, marker: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "search_settings.json"
    target.write_text(json.dumps({"marker": marker}), encoding="utf-8")
    return target


def test_config_dir_uses_the_new_name(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "application_root", lambda: tmp_path)

    assert store.portable_config_dir().name == "DSPScanner-Config"
    assert store.fallback_config_dir().name == "DSPScanner-Config"


def test_legacy_config_is_still_readable(tmp_path, monkeypatch):
    """Настройки из папки со старым именем не теряются после обновления."""
    monkeypatch.setattr(store, "application_root", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    _write_config(tmp_path / "DSPScanner_Config", "legacy")

    found = store.existing_auto_config_path()

    assert found is not None
    assert store.load_scan_config() == {"marker": "legacy"}


def test_new_config_wins_over_legacy(tmp_path, monkeypatch):
    """При наличии обеих папок берётся более свежая, а не старая."""
    monkeypatch.setattr(store, "application_root", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    legacy = _write_config(tmp_path / "DSPScanner_Config", "legacy")
    current = _write_config(tmp_path / "DSPScanner-Config", "current")
    # Явно делаем новый файл свежее: выбор идёт по времени изменения.
    legacy_mtime = legacy.stat().st_mtime_ns
    import os

    os.utime(current, ns=(legacy_mtime + 1_000_000_000, legacy_mtime + 1_000_000_000))

    assert store.load_scan_config() == {"marker": "current"}


def test_saving_never_writes_into_the_legacy_dir(tmp_path, monkeypatch):
    """Старая папка только читается: запись идёт в папку с новым именем."""
    monkeypatch.setattr(store, "application_root", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    legacy_dir = tmp_path / "DSPScanner_Config"
    _write_config(legacy_dir, "legacy")

    saved = store.save_scan_config({"marker": "fresh"})

    assert saved.parent.name == "DSPScanner-Config"
    # Старый файл остаётся нетронутым — на случай отката на прежнюю версию.
    assert json.loads((legacy_dir / "search_settings.json").read_text()) == {
        "marker": "legacy"
    }


def test_env_override_ignores_legacy_dirs(tmp_path, monkeypatch):
    """Явно заданный каталог не должен подменяться старыми путями."""
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("DSP_SCANNER_CONFIG_DIR", str(explicit))
    monkeypatch.setattr(store, "application_root", lambda: tmp_path)
    _write_config(tmp_path / "DSPScanner_Config", "legacy")

    assert store.legacy_config_dirs() == ()
    assert store.portable_config_dir() == explicit
    assert store.existing_auto_config_path() is None
