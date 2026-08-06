"""Offscreen-проверка GUI: окно строится, модель и диагностика работают.

Тесты пропускаются, если PySide6 не установлен или в системе нет библиотек
Qt — тогда рабочим остаётся только CLI-режим, и это не повод падать.
"""
from __future__ import annotations

import os
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


@pytest.fixture
def window(qt_app):
    from app.logging_utils import setup_logger
    from app.gui.main_window import MainWindow

    logger, handler = setup_logger()
    created = MainWindow(logger=logger, qt_log_handler=handler)
    yield created
    created.close()


def test_counting_phase_does_not_show_zero_of_zero(window):
    """До подсчёта общее число неизвестно, и «0 / 0» вводило бы в заблуждение."""
    # _start_scan переводит полосу в неопределённый режим перед запуском.
    window.progress_bar.setMaximum(0)

    window._on_progress(0, 0, "Предварительный подсчёт файлов: 500")

    assert window.progress_label.text() == "Предварительный подсчёт файлов: 500"
    # Список предназначен для обрабатываемых файлов, а не для фазы подсчёта.
    assert window.processing_list.count() == 0
    # Полоса движется, но не притворяется, что знает долю выполнения.
    assert window.progress_bar.maximum() == 0


def test_known_total_switches_progress_to_determinate(window):
    window._on_progress(0, 0, "Предварительный подсчёт файлов: 500")
    window._on_progress(5, 100, "a.txt")
    assert window.progress_bar.maximum() == 100
    assert window.progress_bar.value() == 5
    assert window.progress_label.text() == "Обработано: 5 / 100"
    assert window.processing_list.count() == 1


def test_precount_is_enabled_by_default_and_reaches_settings(window):
    assert window.precount_check.isChecked()
    assert window._collect_settings_silent().precount_files is True
    window.precount_check.setChecked(False)
    assert window._collect_settings_silent().precount_files is False


def test_precount_choice_survives_config_round_trip(window):
    window.precount_check.setChecked(False)
    config = window._current_scan_config()
    assert config["precount_files"] is False
    window._apply_scan_config(config)
    assert window.precount_check.isChecked() is False


def test_config_without_the_key_keeps_precount_on(window):
    """Старые JSON, созданные до появления настройки, не должны её отключать."""
    window.precount_check.setChecked(False)
    window._apply_scan_config({"paths": [], "words": ""})
    assert window.precount_check.isChecked() is True


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


def _name(model, row):
    from PySide6.QtCore import Qt

    return model.data(model.index(row, 0), Qt.DisplayRole)


def _names(model):
    return sorted(_name(model, row) for row in range(model.rowCount()))


def test_unique_file_name_is_shown_as_is(qt_app):
    """Обычный случай ничего не теряет: папка не приписывается."""
    model = ResultsTableModel()
    model.add_results([_result(name="besp.pdf")])
    assert _names(model) == ["besp.pdf"]


def test_same_name_in_two_folders_gets_the_parent_folder(qt_app):
    """Иначе строки неразличимы: столбец «Путь» крайний справа и за экраном."""
    model = ResultsTableModel()
    model.add_results([
        SearchResult("f.pdf", os.path.join(os.sep, "docs", "Отдел кадров", "f.pdf"),
                     "беспилот*", "ctx", "pdf", "2026"),
        SearchResult("f.pdf", os.path.join(os.sep, "docs", "Архив", "f.pdf"),
                     "беспилот*", "ctx", "pdf", "2026"),
    ])
    assert _names(model) == ["f.pdf › Архив", "f.pdf › Отдел кадров"]


def test_colliding_parent_folder_expands_to_two_levels(qt_app):
    """Одной папки мало, когда совпадает и она."""
    model = ResultsTableModel()
    model.add_results([
        SearchResult("f.pdf", os.path.join(os.sep, "docs", "Архив", "2024", "f.pdf"),
                     "беспилот*", "ctx", "pdf", "2026"),
        SearchResult("f.pdf", os.path.join(os.sep, "docs", "Копии", "2024", "f.pdf"),
                     "беспилот*", "ctx", "pdf", "2026"),
    ])
    expected = sorted([
        f"f.pdf › {os.path.join('Архив', '2024')}",
        f"f.pdf › {os.path.join('Копии', '2024')}",
    ])
    assert _names(model) == expected


def test_label_updates_when_the_second_folder_arrives_later(qt_app):
    """Результаты приходят в GUI потоком: имя может стать неоднозначным потом."""
    model = ResultsTableModel()
    model.add_results([
        SearchResult("f.pdf", os.path.join(os.sep, "docs", "A", "f.pdf"),
                     "беспилот*", "ctx", "pdf", "2026"),
    ])
    assert _names(model) == ["f.pdf"]

    changed = []
    model.dataChanged.connect(lambda top, bottom, roles: changed.append(top.row()))
    model.add_results([
        SearchResult("f.pdf", os.path.join(os.sep, "docs", "B", "f.pdf"),
                     "беспилот*", "ctx", "pdf", "2026"),
    ])
    assert _names(model) == ["f.pdf › A", "f.pdf › B"]
    # Уже показанная строка обязана обновиться, иначе останется без папки.
    assert 0 in changed


def test_label_returns_to_plain_name_after_the_copy_is_removed(qt_app):
    model = ResultsTableModel()
    kept = os.path.join(os.sep, "docs", "A", "f.pdf")
    removed = os.path.join(os.sep, "docs", "B", "f.pdf")
    model.add_results([
        SearchResult("f.pdf", kept, "беспилот*", "ctx", "pdf", "2026"),
        SearchResult("f.pdf", removed, "беспилот*", "ctx", "pdf", "2026"),
    ])
    assert len(_names(model)) == 2

    model.remove_paths({removed})
    assert _names(model) == ["f.pdf"]


def test_sorting_by_the_file_column_still_sorts_by_name(qt_app):
    """Имя стоит первым, поэтому копии остаются рядом при сортировке."""
    from app.gui.results_model import disambiguating_labels

    labels = disambiguating_labels(
        "f.pdf",
        [os.path.join(os.sep, "z", "f.pdf"), os.path.join(os.sep, "a", "f.pdf")],
    )
    assert all(label.startswith("f.pdf") for label in labels.values())


def test_disambiguation_falls_back_to_the_full_parent_path(qt_app):
    """Когда различие лежит выше предела уточнения, показываем весь путь."""
    from app.gui.results_model import disambiguating_labels

    deep_a = os.path.join(os.sep, "root_a", "p1", "p2", "p3", "p4", "f.pdf")
    deep_b = os.path.join(os.sep, "root_b", "p1", "p2", "p3", "p4", "f.pdf")
    labels = disambiguating_labels("f.pdf", [deep_a, deep_b])
    assert len(set(labels.values())) == 2
    assert any("root_a" in label for label in labels.values())


def test_path_column_is_untouched(qt_app):
    """Полный путь остаётся ровно тем, что на диске: по нему открывают файл."""
    from PySide6.QtCore import Qt

    model = ResultsTableModel()
    first = os.path.join(os.sep, "docs", "A", "f.pdf")
    second = os.path.join(os.sep, "docs", "B", "f.pdf")
    model.add_results([
        SearchResult("f.pdf", first, "беспилот*", "ctx", "pdf", "2026"),
        SearchResult("f.pdf", second, "беспилот*", "ctx", "pdf", "2026"),
    ])
    shown = {model.data(model.index(row, 6), Qt.DisplayRole) for row in range(model.rowCount())}
    assert shown == {first, second}
    assert set(model.unique_paths()) == {first, second}


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
