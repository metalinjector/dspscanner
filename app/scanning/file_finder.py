"""Безопасный и быстрый рекурсивный поиск файлов по нескольким путям."""
from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path
from threading import Event
from typing import Callable, Iterable, Iterator, Optional, Set

from app.config import IGNORED_FOLDER_NAMES, TEMP_FILE_PREFIXES, FileEntry


def _is_ignored_dir(name: str) -> bool:
    return name.lower() in IGNORED_FOLDER_NAMES or name.startswith(".")


def is_temp_file(filename: str) -> bool:
    return filename.startswith(TEMP_FILE_PREFIXES)


def iter_files_safe(
    paths: Iterable[str],
    extensions: Set[str],
    max_size_bytes: int,
    cancel_event: Optional[Event] = None,
    include_empty: bool = False,
    on_dir_visited: Optional[Callable[[str], None]] = None,
    on_skip: Optional[Callable[[str, str], None]] = None,
) -> Iterator[FileEntry]:
    """Итерирует по обычным файлам без перехода в символьные ссылки-каталоги.

    ``os.scandir`` использует кэш ``DirEntry.stat`` и обычно заметно быстрее
    связки ``os.walk + Path.stat`` на больших деревьях и сетевых дисках.
    Дубликаты определяются по (device, inode), а при недоступности inode — по
    нормализованному абсолютному пути.
    """
    seen: set[object] = set()
    normalized_exts = {str(ext).lower() for ext in extensions}
    max_size_bytes = max(0, int(max_size_bytes))

    def skip(path: str, reason: str) -> None:
        if on_skip:
            on_skip(path, reason)

    for raw_path in paths:
        if cancel_event is not None and cancel_event.is_set():
            return
        path_str = str(raw_path).strip()
        if not path_str:
            continue
        root_path = Path(path_str).expanduser()

        try:
            root_stat = root_path.stat()
        except OSError:
            skip(str(root_path), "missing_or_inaccessible_root")
            continue

        if on_dir_visited:
            on_dir_visited(str(root_path))

        if stat_module.S_ISREG(root_stat.st_mode):
            entry = _make_entry(
                root_path, normalized_exts, max_size_bytes, seen, skip, root_stat,
                include_empty=include_empty,
            )
            if entry:
                yield entry
            continue
        if not stat_module.S_ISDIR(root_stat.st_mode):
            skip(str(root_path), "not_regular")
            continue

        stack = [root_path]
        while stack:
            if cancel_event is not None and cancel_event.is_set():
                return
            current = stack.pop()
            child_dirs: list[Path] = []
            try:
                with os.scandir(current) as iterator:
                    for dir_entry in iterator:
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        try:
                            if dir_entry.is_dir(follow_symlinks=False):
                                if not _is_ignored_dir(dir_entry.name):
                                    child_dirs.append(Path(dir_entry.path))
                                continue
                            if not dir_entry.is_file(follow_symlinks=True):
                                continue
                        except OSError:
                            skip(dir_entry.path, "inaccessible")
                            continue

                        if is_temp_file(dir_entry.name):
                            skip(dir_entry.path, "temp")
                            continue
                        try:
                            entry_stat = dir_entry.stat(follow_symlinks=True)
                        except OSError:
                            skip(dir_entry.path, "inaccessible")
                            continue
                        entry = _make_entry(
                            Path(dir_entry.path), normalized_exts, max_size_bytes,
                            seen, skip, entry_stat, include_empty=include_empty,
                        )
                        if entry:
                            yield entry
            except OSError:
                skip(str(current), "inaccessible_directory")
                continue

            # Разворачиваем, чтобы порядок был близок к обычному обходу сверху вниз.
            stack.extend(reversed(child_dirs))


def _make_entry(
    path: Path,
    extensions: Set[str],
    max_size_bytes: int,
    seen: set[object],
    on_skip: Callable[[str, str], None],
    file_stat=None,
    *,
    include_empty: bool = False,
) -> Optional[FileEntry]:
    ext = path.suffix.lower()
    if ext not in extensions:
        return None

    try:
        file_stat = file_stat or path.stat()
    except OSError:
        on_skip(str(path), "inaccessible")
        return None

    inode = getattr(file_stat, "st_ino", 0)
    device = getattr(file_stat, "st_dev", 0)
    if inode:
        key: object = (device, inode)
    else:
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
    if key in seen:
        on_skip(str(path), "duplicate")
        return None
    seen.add(key)

    if not stat_module.S_ISREG(file_stat.st_mode):
        on_skip(str(path), "not_regular")
        return None
    if file_stat.st_size == 0 and not include_empty:
        on_skip(str(path), "empty")
        return None
    if file_stat.st_size > max_size_bytes:
        on_skip(str(path), "too_big")
        return None

    return FileEntry(path=path, size=file_stat.st_size, modified=file_stat.st_mtime, extension=ext)
