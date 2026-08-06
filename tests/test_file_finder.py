"""Обход дерева: границы сканирования, ссылки, дубликаты, фильтры."""
from __future__ import annotations

import os

from app.scanning.file_finder import iter_files_safe

from conftest import requires_symlinks

BIG = 10**9


def _names(entries):
    return sorted(entry.path.name for entry in entries)


def _scan(paths, extensions=(".txt",), max_size=BIG, **kwargs):
    return list(iter_files_safe([str(p) for p in paths], set(extensions), max_size, **kwargs))


def test_finds_regular_files_recursively(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "c.pdf").write_text("c", encoding="utf-8")
    assert _names(_scan([tmp_path])) == ["a.txt", "b.txt"]


@requires_symlinks
def test_symlink_to_file_outside_tree_is_not_read(tmp_path):
    """Ссылка наружу дала бы текст чужого каталога в отчёте и в письме."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("СЕКРЕТ", encoding="utf-8")
    scan = tmp_path / "scan"
    scan.mkdir()
    (scan / "real.txt").write_text("настоящий", encoding="utf-8")
    os.symlink(outside / "secret.txt", scan / "innocent.txt")

    skips = []
    entries = _scan([scan], on_skip=lambda path, reason: skips.append(reason))
    assert _names(entries) == ["real.txt"]
    assert "symlink" in skips


@requires_symlinks
def test_symlink_to_directory_is_not_traversed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("СЕКРЕТ", encoding="utf-8")
    scan = tmp_path / "scan"
    scan.mkdir()
    (scan / "own.txt").write_text("своё", encoding="utf-8")
    os.symlink(outside, scan / "link_dir")
    assert _names(_scan([scan])) == ["own.txt"]


@requires_symlinks
def test_symlink_inside_tree_loses_nothing(tmp_path):
    """Цель внутри дерева всё равно обходится по своему настоящему пути."""
    scan = tmp_path / "scan"
    scan.mkdir()
    (scan / "real.txt").write_text("настоящий", encoding="utf-8")
    os.symlink(scan / "real.txt", scan / "alias.txt")
    assert _names(_scan([scan])) == ["real.txt"]


@requires_symlinks
def test_symlinked_root_still_works(tmp_path):
    """Корень выбирает оператор — это доверенный вход, его раскрываем."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.txt").write_text("a", encoding="utf-8")
    link_root = tmp_path / "link_root"
    os.symlink(real, link_root)
    assert _names(_scan([link_root])) == ["a.txt"]


def test_single_file_path_as_root(tmp_path):
    target = tmp_path / "one.txt"
    target.write_text("x", encoding="utf-8")
    assert _names(_scan([target])) == ["one.txt"]


def test_same_file_listed_twice_is_deduplicated(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    skips = []
    entries = _scan([tmp_path, tmp_path], on_skip=lambda path, reason: skips.append(reason))
    assert _names(entries) == ["a.txt"]
    assert "duplicate" in skips


def test_temp_files_are_skipped(tmp_path):
    (tmp_path / "~$draft.txt").write_text("x", encoding="utf-8")
    (tmp_path / "good.txt").write_text("x", encoding="utf-8")
    skips = []
    assert _names(_scan([tmp_path], on_skip=lambda p, r: skips.append(r))) == ["good.txt"]
    assert "temp" in skips


def test_oversized_and_empty_files_are_skipped(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 500, encoding="utf-8")
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("x", encoding="utf-8")
    assert _names(_scan([tmp_path], max_size=100)) == ["ok.txt"]


def test_empty_files_are_kept_when_searching_by_name(tmp_path):
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    assert _names(_scan([tmp_path], include_empty=True)) == ["empty.txt"]


def test_missing_root_is_reported_not_raised(tmp_path):
    skips = []
    assert _scan([tmp_path / "нет-такого"], on_skip=lambda p, r: skips.append(r)) == []
    assert skips == ["missing_or_inaccessible_root"]


def test_hidden_and_service_directories_are_ignored(tmp_path):
    for name in (".git", "node_modules", "__pycache__"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.txt").write_text("x", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("x", encoding="utf-8")
    assert _names(_scan([tmp_path])) == ["visible.txt"]
