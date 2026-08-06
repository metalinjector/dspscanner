"""Сохранение и загрузка глобальных настроек приложения."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional

from app.config import SETTINGS_FILE
from app.scan_config_store import application_root

_settings_lock = threading.Lock()
_settings_cache: Optional["AppSettings"] = None
_PORTABLE_TESSERACT_DIR_NAME = "Tesseract-OCR"


def portable_tesseract_dir() -> Path:
    """Папка переносимой копии Tesseract рядом с приложением."""
    return application_root() / _PORTABLE_TESSERACT_DIR_NAME


def ensure_portable_tesseract_dir() -> Optional[Path]:
    """Создаёт переносимую папку Tesseract, не прерывая запуск при read-only.

    В обычной portable-распаковке каталог приложения доступен для записи и
    папка создаётся при каждом запуске. Если приложение установлено в
    защищённый системный каталог, функция возвращает ``None``: автоопределение
    продолжит проверять PATH и стандартные каталоги установки.
    """
    directory = portable_tesseract_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return directory


def _portable_tesseract_candidates(directory: Path) -> tuple[Path, ...]:
    """Ожидаемые расположения исполняемого файла в переносимой папке."""
    return (
        directory / "tesseract.exe",
        directory / "tesseract",
        directory / "bin" / "tesseract.exe",
        directory / "bin" / "tesseract",
    )


@dataclass
class AppSettings:
    libreoffice_path: Optional[str] = None
    tesseract_path: Optional[str] = None
    max_workers: int = min(32, (os.cpu_count() or 4) * 4)
    per_file_timeout: int = 60
    max_matches_per_word: int = 50
    secure_passes: int = 3
    reset_stats_on_start: bool = True

    def sanitized(self) -> "AppSettings":
        def _int(value, default: int, low: int, high: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return default
            return min(high, max(low, parsed))

        def _path(value) -> Optional[str]:
            if value is None:
                return None
            try:
                text = str(value).strip()
            except Exception:
                return None
            return text or None

        return AppSettings(
            libreoffice_path=_path(self.libreoffice_path),
            tesseract_path=_path(self.tesseract_path),
            max_workers=_int(self.max_workers, min(32, (os.cpu_count() or 4) * 4), 1, 128),
            per_file_timeout=_int(self.per_file_timeout, 60, 10, 600),
            max_matches_per_word=_int(self.max_matches_per_word, 50, 1, 500),
            secure_passes=_int(self.secure_passes, 3, 1, 7),
            reset_stats_on_start=bool(self.reset_stats_on_start),
        )

    def to_mapping(self) -> dict:
        return asdict(self.sanitized())

    @classmethod
    def from_mapping(cls, value) -> "AppSettings":
        if not isinstance(value, dict):
            return cls().sanitized()
        allowed = cls.__dataclass_fields__
        return cls(**{key: item for key, item in value.items() if key in allowed}).sanitized()


def _apply_environment_overrides(settings: AppSettings) -> AppSettings:
    """Переопределения для CLI/серверного запуска, наследуемые spawn-процессами."""
    mapping = {
        "DSP_SCANNER_LIBREOFFICE_PATH": "libreoffice_path",
        "DSP_SCANNER_TESSERACT_PATH": "tesseract_path",
        "DSP_SCANNER_MAX_WORKERS": "max_workers",
        "DSP_SCANNER_PER_FILE_TIMEOUT": "per_file_timeout",
        "DSP_SCANNER_MAX_MATCHES": "max_matches_per_word",
        "DSP_SCANNER_SECURE_PASSES": "secure_passes",
    }
    values = asdict(settings)
    for env_name, field_name in mapping.items():
        raw = os.environ.get(env_name)
        if raw is not None and str(raw).strip():
            values[field_name] = raw
    return AppSettings(**values).sanitized()


def load_settings() -> AppSettings:
    global _settings_cache
    with _settings_lock:
        if _settings_cache is None:
            _settings_cache = _load_settings_uncached()
        # Возвращаем копию с runtime-переопределениями: CLI может задавать
        # пути и лимиты без изменения пользовательского JSON.
        return _apply_environment_overrides(replace(_settings_cache))


def invalidate_settings_cache() -> None:
    global _settings_cache
    with _settings_lock:
        _settings_cache = None


def _load_settings_uncached() -> AppSettings:
    if not SETTINGS_FILE.exists():
        return _autodetect(AppSettings()).sanitized()
    try:
        if SETTINGS_FILE.stat().st_size > 1_000_000:
            raise ValueError("Файл настроек слишком велик")
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("Настройки должны быть JSON-объектом")
        allowed = AppSettings.__dataclass_fields__
        settings = AppSettings(**{k: v for k, v in data.items() if k in allowed}).sanitized()
        return _autodetect(settings).sanitized()
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return _autodetect(AppSettings()).sanitized()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def save_settings(settings: AppSettings) -> bool:
    sanitized = settings.sanitized()
    try:
        _atomic_write_text(
            SETTINGS_FILE,
            json.dumps(asdict(sanitized), ensure_ascii=False, indent=2),
        )
    except OSError:
        return False
    finally:
        invalidate_settings_cache()
    return True


def autodetect_external_programs() -> tuple[Optional[str], Optional[str]]:
    lo: Optional[str] = None
    for candidate in ("soffice", "soffice.exe", "libreoffice"):
        found = shutil.which(candidate)
        if found:
            lo = found
            break
    if not lo and os.name == "nt":
        for candidate in (
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ):
            if Path(candidate).is_file():
                lo = candidate
                break

    # Переносимая папка рядом с программой имеет приоритет над системной
    # установкой: пользователь может положить туда готовый комплект Tesseract
    # вместе с tessdata и переносить приложение между компьютерами.
    ts: Optional[str] = None
    portable_dir = ensure_portable_tesseract_dir()
    if portable_dir is not None:
        for candidate in _portable_tesseract_candidates(portable_dir):
            if candidate.is_file():
                ts = str(candidate.resolve())
                break

    if not ts:
        ts = shutil.which("tesseract") or shutil.which("tesseract.exe")
    if not ts and os.name == "nt":
        for candidate in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            r"C:\Tesseract-OCR\tesseract.exe",
            r"D:\Program Files\Tesseract-OCR\tesseract.exe",
        ):
            if Path(candidate).is_file():
                ts = candidate
                break

    return lo, ts


def _autodetect(settings: AppSettings) -> AppSettings:
    # Переносимый JSON может содержать путь с другого компьютера. Несуществующий
    # путь не должен блокировать автоматический поиск на новой машине.
    needs_lo = not settings.libreoffice_path or not Path(settings.libreoffice_path).is_file()
    needs_ts = not settings.tesseract_path or not Path(settings.tesseract_path).is_file()
    if needs_lo or needs_ts:
        lo, ts = autodetect_external_programs()
        if needs_lo:
            settings.libreoffice_path = lo
        if needs_ts:
            settings.tesseract_path = ts
    return settings
