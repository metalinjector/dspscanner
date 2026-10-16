"""Копирование и осторожные разрушительные операции с файлами."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat as stat_module
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Sequence


# Сколько раз переименовать файл перед удалением, чтобы затереть его имя в
# записи каталога. Больше трёх смысла не имеет: журналируемые ФС всё равно
# могут сохранить старые записи, и это ограничение не обходится из userspace.
_RENAME_PASSES = 3
_RENAME_COLLISION_RETRIES = 5


@dataclass
class CopyMapping:
    source_path: str
    target_name: str
    target_path: str


@dataclass
class OperationResult:
    succeeded: int = 0
    failures: list[str] = field(default_factory=list)
    # Пользователь прервал операцию: часть файлов осталась необработанной,
    # и это не ошибка — в отчёте нельзя показывать их как неудачные.
    cancelled: bool = False


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
    """Копирует без перезаписи и удаляет только свою недокопированную копию."""
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with open(source, "rb") as src, open(target, "xb") as dst:
            created = True
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, target, follow_symlinks=False)
    except Exception:
        # open(..., "xb") мог отказать, потому что имя занято. В этом случае
        # target принадлежит не нам; прежний cleanup удалял чужой файл.
        if created:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def copy_found_files(
    unique_paths: Sequence[str],
    destination: str,
    on_progress: Callable[[int, int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> List[CopyMapping]:
    dest_dir = Path(destination).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    mappings: List[CopyMapping] = []
    total = len(unique_paths)

    for index, source_text in enumerate(unique_paths, start=1):
        if is_cancelled and is_cancelled():
            break
        source = Path(source_text)
        if on_progress:
            on_progress(index, total, source.name)
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


def _ensure_writable(path: Path) -> None:
    """Снимает атрибут «Только для чтения», если он установлен."""
    try:
        current_mode = path.stat().st_mode
        if not (current_mode & stat_module.S_IWRITE):
            path.chmod(current_mode | stat_module.S_IWRITE)
    except OSError:
        pass


def _truncate_in_place(path: Path, expected_stat) -> None:
    """Обнуляет длину файла после перезаписи содержимого.

    Размер файла — тоже метаданные: по нему можно судить о том, что здесь
    лежало. Усечение выполняется отдельным шагом и не считается критичным:
    содержимое к этому моменту уже затёрто, поэтому любая ошибка здесь не
    должна срывать само удаление.
    """
    _ensure_writable(path)
    try:
        if path.stat().st_size == 0:
            return
    except OSError:
        return

    flags = os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        opened_stat = os.fstat(fd)
        if not stat_module.S_ISREG(opened_stat.st_mode):
            raise OSError("путь не является обычным файлом")
        expected_identity = _file_identity(expected_stat)
        opened_identity = _file_identity(opened_stat)
        if all(expected_identity) and opened_identity != expected_identity:
            raise OSError("файл был заменён во время операции")
        os.ftruncate(fd, 0)
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _scrub_name(path: Path) -> Path:
    """Переименовывает файл случайным именем для сокрытия исходного имени в каталоге.

    Сохраняет суффикс (расширение) файла, чтобы не вызывать ложных срабатываний
    эвристик антивирусов (ransomware detection), и ограничивается разумным числом
    попыток.
    """
    current = path
    suffix = current.suffix
    for _ in range(_RENAME_PASSES):
        stem_len = max(8, len(current.stem))
        for _attempt in range(_RENAME_COLLISION_RETRIES):
            random_name = secrets.token_hex(stem_len)[:stem_len]
            candidate = current.with_name(f"{random_name}{suffix}")
            if candidate.exists():
                continue
            try:
                current.rename(candidate)
                current = candidate
            except OSError:
                return current
            break
    return current


def _fsync_directory(directory: Path) -> None:
    """Сбрасывает на диск саму запись каталога после удаления.

    Без этого удаление может остаться только в кеше метаданных. На Windows
    каталог открыть через POSIX open нельзя — шаг пропускается.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _unlink_verified(path: Path, expected_stat) -> None:
    current = path.lstat()
    if stat_module.S_ISLNK(current.st_mode) or not stat_module.S_ISREG(current.st_mode):
        raise OSError("файл был заменён во время операции")
    expected_identity = _file_identity(expected_stat)
    current_identity = _file_identity(current)
    if all(expected_identity) and current_identity != expected_identity:
        raise OSError("файл был заменён во время операции")

    # Содержимое уже перезаписано; дальше убираются остальные следы —
    # размер и имя. Оба шага не критичны для самого удаления.
    try:
        _truncate_in_place(path, expected_stat)
    except OSError:
        pass
    scrubbed = _scrub_name(path)

    _ensure_writable(scrubbed)
    # На Windows после записи и переименования файл может быть кратковременно
    # заблокирован антивирусом или индексатором. Повторяем unlink с небольшой паузой.
    last_exc: OSError | None = None
    for attempt in range(5):
        try:
            scrubbed.unlink()
            last_exc = None
            break
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.05)
        except OSError as exc:
            last_exc = exc
            break

    if last_exc is not None:
        if scrubbed != path:
            # Иначе пользователь не найдёт остаток: имя уже случайное.
            raise OSError(
                f"{last_exc}; содержимое уничтожено, остался файл {scrubbed.name}"
            ) from last_exc
        raise last_exc
    _fsync_directory(scrubbed.parent)


def secure_delete_files(
    paths: Sequence[str],
    passes: int = 3,
    on_progress: Callable[[int, int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> OperationResult:
    result = OperationResult()
    passes = _clamp_passes(passes)
    total = len(paths)
    for index, path_text in enumerate(paths, start=1):
        if is_cancelled and is_cancelled():
            result.cancelled = True
            break
        path = Path(path_text).expanduser()
        if on_progress:
            on_progress(index, total, path.name)
        try:
            expected_stat = _validate_destructive_path(path)
            overwritten_stat = _overwrite_file(path, passes, expected_stat)
            _unlink_verified(path, overwritten_stat)
            result.succeeded += 1
        except Exception as exc:
            # Ловим Exception, а не только OSError: непредвиденная ошибка
            # одного файла не должна уносить с собой весь пакет — остальные
            # файлы пользователь всё равно просил обработать.
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


def secure_move_files(
    paths: Sequence[str],
    destination: str,
    passes: int = 3,
    on_progress: Callable[[int, int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> OperationResult:
    dest_dir = Path(destination).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = OperationResult()
    passes = _clamp_passes(passes)
    total = len(paths)

    for index, path_text in enumerate(paths, start=1):
        if is_cancelled and is_cancelled():
            result.cancelled = True
            break
        source = Path(path_text).expanduser()
        target: Path | None = None
        if on_progress:
            on_progress(index, total, source.name)
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
        except Exception as exc:
            result.failures.append(f"{source}: {exc}")
            # Копия при неудаче стирания сохраняется как резерв, но явно
            # сообщается пользователю, что исходный файл остался на месте.
    return result


def _overwrite_file(path: Path, passes: int, expected_stat=None):
    _ensure_writable(path)
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
            passes_count = max(1, passes)
            for pass_index in range(passes_count):
                file_handle.seek(0)
                remaining = size
                # NIST SP 800-88 рекомендует фиксированный шаблон (нули 0x00).
                # При многопроходной перезаписи чередуем: нули / 0xFF / случайные байты.
                # Это надёжно и не вызывает срабатывания антивирусов (ransomware detection).
                if passes_count == 1 or pass_index == 0:
                    pattern = b"\x00"
                    use_urandom = False
                elif pass_index == 1:
                    pattern = b"\xff"
                    use_urandom = False
                elif pass_index % 2 == 0:
                    use_urandom = True
                    pattern = b""
                else:
                    pattern = b"\xaa"
                    use_urandom = False

                while remaining:
                    chunk_size = min(chunk_size_max, remaining)
                    data = os.urandom(chunk_size) if use_urandom else pattern * chunk_size
                    written = file_handle.write(data)
                    if written != chunk_size:
                        raise OSError("неполная запись при перезаписи")
                    remaining -= written
                file_handle.flush()
                try:
                    os.fsync(file_handle.fileno())
                except OSError:
                    pass

            # Обнуляем размер файла прямо на открытом дескрипторе
            try:
                file_handle.truncate(0)
                file_handle.flush()
                os.fsync(file_handle.fileno())
            except OSError:
                pass
            return os.fstat(file_handle.fileno())
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
