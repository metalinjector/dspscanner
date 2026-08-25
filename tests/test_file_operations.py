"""Проверки безопасного удаления и перемещения.

Помимо самого содержимого файл оставляет следы: имя в записи каталога и
размер. Эти тесты фиксируют, что затираются все три, и что улучшения не
ломают главное — файл обязан быть удалён даже там, где затереть имя нельзя.
"""
from __future__ import annotations

from pathlib import Path

from app.fileops import file_operations as fops


def test_secure_delete_scrubs_the_file_name(tmp_path, monkeypatch):
    """Имя файла затирается переименованием до unlink.

    Имя вроде «увольнение_иванова.docx» само раскрывает содержание и после
    обычного unlink остаётся в записи каталога. Так же поступают `shred -u`
    и `srm`.
    """
    victim = tmp_path / "увольнение_иванова.docx"
    victim.write_bytes(b"CONFIDENTIAL" * 1000)

    renamed_to: list[str] = []
    original_rename = Path.rename

    def tracking_rename(self, target):
        renamed_to.append(Path(target).name)
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", tracking_rename)

    result = fops.secure_delete_files([str(victim)], passes=1)

    assert result.succeeded == 1
    assert not victim.exists()
    assert renamed_to, "файл удалён под исходным именем — оно осталось в каталоге"
    assert "увольнение" not in " ".join(renamed_to)


def test_secure_delete_truncates_before_unlink(tmp_path, monkeypatch):
    """Размер обнуляется: длина файла — тоже метаданные."""
    victim = tmp_path / "secret.bin"
    victim.write_bytes(b"A" * 4096)

    sizes_at_unlink: list[int] = []
    original_unlink = Path.unlink

    def tracking_unlink(self, *args, **kwargs):
        sizes_at_unlink.append(self.stat().st_size)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", tracking_unlink)

    assert fops.secure_delete_files([str(victim)], passes=1).succeeded == 1
    assert sizes_at_unlink == [0], f"файл удалён с ненулевым размером: {sizes_at_unlink}"


def test_secure_delete_overwrites_content_before_removing(tmp_path, monkeypatch):
    """К моменту удаления исходных байтов в файле уже нет."""
    victim = tmp_path / "plain.txt"
    victim.write_bytes(b"SECRET-MARKER-" * 500)

    captured: dict[str, bytes] = {}
    original_unlink = Path.unlink

    def capturing_unlink(self, *args, **kwargs):
        captured["data"] = self.read_bytes()
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", capturing_unlink)

    assert fops.secure_delete_files([str(victim)], passes=1).succeeded == 1
    assert b"SECRET-MARKER" not in captured["data"]


def test_secure_delete_still_removes_when_rename_is_refused(tmp_path, monkeypatch):
    """Запрет переименования не должен срывать удаление.

    Затирание имени — улучшение, а не обязательный шаг: содержимое к этому
    моменту уже уничтожено, поэтому файл обязан исчезнуть в любом случае
    (каталог только для чтения, чужая файловая система, гонка).
    """
    victim = tmp_path / "doc.txt"
    victim.write_bytes(b"data" * 100)

    def refusing_rename(self, target):
        raise OSError("переименование запрещено")

    monkeypatch.setattr(Path, "rename", refusing_rename)

    assert fops.secure_delete_files([str(victim)], passes=1).succeeded == 1
    assert not victim.exists()


def test_secure_delete_reports_the_new_name_if_unlink_fails(tmp_path, monkeypatch):
    """Если удалить не удалось, в ошибке видно, что искать.

    Имя к этому моменту уже случайное, и без подсказки пользователь не нашёл
    бы остаток на диске.
    """
    victim = tmp_path / "doc.txt"
    victim.write_bytes(b"data" * 100)

    def failing_unlink(self, *args, **kwargs):
        raise OSError("устройство занято")

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    result = fops.secure_delete_files([str(victim)], passes=1)

    assert result.succeeded == 0
    assert result.failures
    assert "содержимое уничтожено" in result.failures[0]


def test_secure_delete_handles_an_empty_file(tmp_path):
    """Пустой файл нечего перезаписывать, но удалить его нужно."""
    victim = tmp_path / "empty.txt"
    victim.touch()

    assert fops.secure_delete_files([str(victim)], passes=1).succeeded == 1
    assert not victim.exists()


def test_secure_delete_refuses_symlinks(tmp_path):
    """По ссылке нельзя затирать чужой файл."""
    target = tmp_path / "real.txt"
    target.write_bytes(b"important")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return  # символьные ссылки недоступны — проверять нечего

    result = fops.secure_delete_files([str(link)], passes=1)

    assert result.succeeded == 0
    assert target.read_bytes() == b"important"


def test_secure_move_verifies_copy_before_destroying_original(tmp_path):
    """Оригинал уничтожается только после сверки копии по SHA-256."""
    source = tmp_path / "src" / "doc.txt"
    source.parent.mkdir()
    payload = b"important" * 500
    source.write_bytes(payload)
    destination = tmp_path / "dst"

    result = fops.secure_move_files([str(source)], str(destination), passes=1)

    assert result.succeeded == 1
    assert not source.exists()
    assert (destination / "doc.txt").read_bytes() == payload


def test_secure_move_keeps_the_original_when_the_copy_cannot_be_verified(
    tmp_path, monkeypatch
):
    """Не сверив копию, оригинал не трогаем: иначе данные пропадут совсем."""
    source = tmp_path / "src" / "doc.txt"
    source.parent.mkdir()
    payload = b"important" * 200
    source.write_bytes(payload)
    destination = tmp_path / "dst"

    def broken_verify(*args, **kwargs):
        raise OSError("контрольная сумма не совпала")

    monkeypatch.setattr(fops, "_verify_secure_move_copy", broken_verify)

    result = fops.secure_move_files([str(source)], str(destination), passes=1)

    assert result.succeeded == 0
    assert source.read_bytes() == payload, "оригинал уничтожен без проверки копии"
