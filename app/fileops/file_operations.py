"""Копирование и осторожные разрушительные операции с файлами."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat as stat_module
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence


@dataclass
class CopyMapping:
    source_path: str
    target_name: str
    target_path: str


@dataclass
class OperationResult:
    succeeded: int = 0
    failures: list[str] = field(default_factory=list)


def _unique_target(dest_dir: Path, source: Path) -> Path:
    candidate = dest_dir / source.name
    if not candidate.exists():
        return candidate
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{source.stem}_{stamp}"
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"_{counter}"
        candidate = dest_dir / f"{base}{suffix}{source.suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _copy_exclusive(source: Path, target: Path) -> None:
    """Копирует без перезаписи существующего файла и удаляет недокопанный файл."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(source, "rb") as src, open(target, "xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, target, follow_symlinks=False)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def copy_found_files(unique_paths: Sequence[str], destination: str) -> List[CopyMapping]:
    dest_dir = Path(destination).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    mappings: List[CopyMapping] = []

    for source_text in unique_paths:
        source = Path(source_text)
        try:
            if source.is_symlink() or not source.is_file():
                continue
            target = _unique_target(dest_dir, source)
            # Защита от гонки: если имя заняли между проверкой и созданием,
            # выбираем следующее, но никогда не перезаписываем чужой файл.
            while True:
                try:
                    _copy_exclusive(source, target)
                    break
                except FileExistsError:
                    target = _unique_target(dest_dir, source)
            mappings.append(CopyMapping(str(source), target.name, str(target)))
        except OSError:
            continue

    _write_mapping_files(dest_dir, mappings)
    return mappings


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file_handle:
            file_handle.write(text)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _free_metadata_path(dest_dir: Path, name: str) -> Path:
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    counter = 0
    while True:
        extra = "" if counter == 0 else f"_{counter}"
        candidate = dest_dir / f"{stem}_{stamp}{extra}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _write_mapping_files(dest_dir: Path, mappings: List[CopyMapping]) -> None:
    try:
        text = "".join(f"{mapping.source_path} -> {mapping.target_path}\n" for mapping in mappings)
        _atomic_write(_free_metadata_path(dest_dir, "allEX.txt"), text)
        _atomic_write(
            _free_metadata_path(dest_dir, "allEX.json"),
            json.dumps([asdict(mapping) for mapping in mappings], ensure_ascii=False, indent=2),
        )
    except OSError:
        return


def detect_removed_copies(destination: str, mappings: Iterable[CopyMapping]) -> List[str]:
    """Возвращает оригиналы, чьи *конкретные* скопированные файлы исчезли.

    Параметр ``destination`` сохранён для обратной совместимости, но проверка
    идёт по ``target_path`` из маппинга. Поэтому изменение поля назначения в
    интерфейсе больше не может ошибочно пометить все оригиналы на удаление.
    """
    del destination
    removed: List[str] = []
    for mapping in mappings:
        try:
            if not Path(mapping.target_path).is_file():
                removed.append(mapping.source_path)
        except OSError:
            removed.append(mapping.source_path)
    return removed


def _file_identity(file_stat) -> tuple[int, int]:
    return int(getattr(file_stat, "st_dev", 0)), int(getattr(file_stat, "st_ino", 0))


def _validate_destructive_path(path: Path):
    link_stat = path.lstat()
    if stat_module.S_ISLNK(link_stat.st_mode):
        raise OSError("символьные ссылки не перезаписываются")
    if not stat_module.S_ISREG(link_stat.st_mode):
        raise OSError("путь не является обычным файлом")
    if getattr(link_stat, "st_nlink", 1) > 1:
        raise OSError("файл имеет жёсткие ссылки; перезапись затронула бы другие имена")
    return link_stat


def _clamp_passes(passes: int) -> int:
    try:
        return min(7, max(1, int(passes)))
    except (TypeError, ValueError, OverflowError):
        return 3


def _unlink_verified(path: Path, expected_stat) -> None:
    current = path.lstat()
    if stat_module.S_ISLNK(current.st_mode) or not stat_module.S_ISREG(current.st_mode):
        raise OSError("файл был заменён во время операции")
    expected_identity = _file_identity(expected_stat)
    current_identity = _file_identity(current)
    if all(expected_identity) and current_identity != expected_identity:
        raise OSError("файл был заменён во время операции")
    path.unlink()


def secure_delete_files(paths: Sequence[str], passes: int = 3) -> OperationResult:
    result = OperationResult()
    passes = _clamp_passes(passes)
    for path_text in paths:
        path = Path(path_text).expanduser()
        try:
            expected_stat = _validate_destructive_path(path)
            overwritten_stat = _overwrite_file(path, passes, expected_stat)
            _unlink_verified(path, overwritten_stat)
            result.succeeded += 1
        except OSError as exc:
            result.failures.append(f"{path}: {exc}")
    return result




def _sha256_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with open(path, "rb") as file_handle:
        while chunk := file_handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


def _verify_secure_move_copy(source: Path, target: Path, expected_stat) -> None:
    """Проверяет новую копию до начала необратимой перезаписи оригинала."""
    current = _validate_destructive_path(source)
    expected_identity = _file_identity(expected_stat)
    current_identity = _file_identity(current)
    if all(expected_identity) and current_identity != expected_identity:
        raise OSError("оригинал был заменён во время копирования")
    if current.st_size != expected_stat.st_size:
        raise OSError("размер оригинала изменился во время копирования")
    target_stat = target.stat()
    if target_stat.st_size != current.st_size:
        raise OSError("размер созданной копии не совпадает с оригиналом")
    if _sha256_file(source) != _sha256_file(target):
        raise OSError("контрольная сумма созданной копии не совпадает с оригиналом")


def secure_move_files(paths: Sequence[str], destination: str, passes: int = 3) -> OperationResult:
    dest_dir = Path(destination).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = OperationResult()
    passes = _clamp_passes(passes)

    for path_text in paths:
        source = Path(path_text).expanduser()
        target: Path | None = None
        try:
            expected_stat = _validate_destructive_path(source)
            target = _unique_target(dest_dir, source)
            while True:
                try:
                    _copy_exclusive(source, target)
                    break
                except FileExistsError:
                    target = _unique_target(dest_dir, source)
            _verify_secure_move_copy(source, target, expected_stat)
            overwritten_stat = _overwrite_file(source, passes, expected_stat)
            _unlink_verified(source, overwritten_stat)
            result.succeeded += 1
        except OSError as exc:
            result.failures.append(f"{source}: {exc}")
            # Копия при неудаче стирания сохраняется как резерв, но явно
            # сообщается пользователю, что исходный файл остался на месте.
    return result


def _overwrite_file(path: Path, passes: int, expected_stat=None):
    flags = os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    fd = os.open(path, flags)
    try:
        opened_stat = os.fstat(fd)
        if not stat_module.S_ISREG(opened_stat.st_mode):
            raise OSError("путь не является обычным файлом")
        if getattr(opened_stat, "st_nlink", 1) > 1:
            raise OSError("файл имеет жёсткие ссылки; перезапись затронула бы другие имена")
        if expected_stat is not None:
            expected_identity = _file_identity(expected_stat)
            opened_identity = _file_identity(opened_stat)
            if all(expected_identity) and opened_identity != expected_identity:
                raise OSError("файл был заменён до начала перезаписи")

        size = opened_stat.st_size
        if size == 0:
            return opened_stat
        chunk_size_max = 1024 * 1024
        with os.fdopen(fd, "r+b", buffering=0, closefd=True) as file_handle:
            fd = -1
            for pass_index in range(max(1, passes)):
                file_handle.seek(0)
                remaining = size
                use_random = pass_index % 2 == 0
                while remaining:
                    chunk_size = min(chunk_size_max, remaining)
                    data = os.urandom(chunk_size) if use_random else b"\x00" * chunk_size
                    written = file_handle.write(data)
                    if written != chunk_size:
                        raise OSError("неполная запись при перезаписи")
                    remaining -= written
                file_handle.flush()
                os.fsync(file_handle.fileno())
            return os.fstat(file_handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
