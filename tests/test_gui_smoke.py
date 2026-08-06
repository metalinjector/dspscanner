"""Offscreen-проверка GUI: окно строится, модель и диагностика работают.

Тесты пропускаются, если PySide6 не установлен или в системе нет библиотек
Qt — тогда рабочим остаётся только CLI-режим, и это не повод падать.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI-режим не установлен")

try:
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # системные библиотеки Qt отсутствуют
    pytest.skip(f"Qt недоступен: {exc}", allow_module_level=True)

from app.config import ScanSettings, SearchResult
from app.gui.results_model import ResultsTableModel, highlight_html, is_filename_match
from app.gui.workers import SingleFileTestWorker


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _result(name="a.txt", word="договор", context="в тексте договор найден"):
    return SearchResult(name, f"/x/{name}", word, context, "txt", "2026-01-01")


def test_main_window_builds(qt_app):
    from app.logging_utils import setup_logger
    from app.gui.main_window import MainWindow

    logger, handler = setup_logger()
    window = MainWindow(logger=logger, qt_log_handler=handler)
    assert window.windowTitle()
    assert window.results_table.model() is not None
    window.close()


def test_results_model_groups_occurrences_of_one_term(qt_app):
    model = ResultsTableModel()
    model.add_results([_result(), _result(), _result(name="b.txt")])
    assert model.rowCount() == 2
    assert model.occurrence_count() == 3
    assert model.unique_paths() == ["/x/a.txt", "/x/b.txt"]


def test_results_model_clear_and_remove(qt_app):
    model = ResultsTableModel()
    model.add_results([_result(), _result(name="b.txt")])
    model.remove_paths({"/x/a.txt"})
    assert model.unique_paths() == ["/x/b.txt"]
    model.clear()
    assert model.rowCount() == 0


def test_filename_matches_are_distinguishable(qt_app):
    assert is_filename_match(_result(context="[Имя файла] договор.txt"))
    assert not is_filename_match(_result())


def test_highlight_escapes_html_from_documents(qt_app):
    """Контекст приходит из чужого документа и не должен становиться разметкой."""
    html = highlight_html("<script>договор</script>", "договор", "#fff")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_single_file_diagnostic_runs_outside_gui_thread(qt_app, tmp_path):
    """Чтение и OCR не должны выполняться в потоке событий Qt."""
    from PySide6.QtCore import QEventLoop, QTimer

    target = tmp_path / "proba.txt"
    target.write_text("Служебная записка о договоре", encoding="utf-8")

    worker = SingleFileTestWorker(target, ScanSettings(words=["договор"]))
    captured = {}
    loop = QEventLoop()
    worker.finished_ok.connect(lambda outcome, elapsed: (captured.update(outcome=outcome), loop.quit()))
    worker.failed.connect(lambda message: (captured.update(error=message), loop.quit()))
    QTimer.singleShot(60_000, loop.quit)
    worker.start()
    loop.exec()
    worker.wait(10_000)

    assert "error" not in captured, captured.get("error")
    assert "договоре" in captured["outcome"].text


def test_diagnostic_worker_reports_unreadable_file_without_raising(qt_app, tmp_path):
    from PySide6.QtCore import QEventLoop, QTimer

    worker = SingleFileTestWorker(tmp_path / "нет.txt", ScanSettings(words=["x"]))
    captured = {}
    loop = QEventLoop()
    worker.finished_ok.connect(lambda outcome, elapsed: (captured.update(outcome=outcome), loop.quit()))
    worker.failed.connect(lambda message: (captured.update(error=message), loop.quit()))
    QTimer.singleShot(30_000, loop.quit)
    worker.start()
    loop.exec()
    worker.wait(10_000)

    assert "error" not in captured
    assert captured["outcome"].error


def test_application_icon_has_a_fallback(qt_app):
    """В собранном виде ресурсов может не быть — иконка всё равно обязана быть."""
    from app.gui.main_window import _make_app_icon

    assert not _make_app_icon().isNull()


def test_theme_applies_without_errors(qt_app):
    from app.gui.theme import apply_modern_theme

    apply_modern_theme(qt_app)
    assert qt_app.styleSheet()


def test_repository_has_no_stray_python_app_directory():
    """README долго вёл в каталог python_app, которого в репозитории нет."""
    assert not (Path(__file__).resolve().parents[1] / "python_app").exists()
