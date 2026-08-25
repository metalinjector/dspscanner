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


CONTEXT_COLUMN = 4
TYPICAL_CONTEXT = (
    "Для служебного пользования, беспилотники. Лекарственные поражения печени явля…"
)


def _feed_context(window, context, matched="беспилотники", name="f.pdf"):
    window._on_results_found([
        SearchResult(name, f"/d/{name}", "беспилот*", context, "pdf", "2026",
                     matched_text=matched)
    ])


def test_context_column_fits_the_actual_text(window):
    """Ширина колонки считается из контента, а не жёстко задана 900 px.

    Формула подгонки: ``min(MAX, ширина_текста + запас)``, ограниченная
    снизу ``MIN``. Проверяем именно её — она детерминирована и не зависит
    от шрифта окружения (в offscreen-рендере субститут рисует кириллицу
    шире, и колонка закономерно упирается в верхний предел).
    """
    from app.gui.main_window import (
        _CONTEXT_COLUMN_MAX_WIDTH,
        _CONTEXT_COLUMN_MIN_WIDTH,
        _CONTEXT_COLUMN_PADDING,
    )

    _feed_context(window, TYPICAL_CONTEXT)
    width = window.results_table.columnWidth(CONTEXT_COLUMN)
    text_width = window.results_table.fontMetrics().horizontalAdvance(TYPICAL_CONTEXT)

    expected = min(_CONTEXT_COLUMN_MAX_WIDTH, text_width + _CONTEXT_COLUMN_PADDING)
    expected = max(_CONTEXT_COLUMN_MIN_WIDTH, expected)
    assert width == expected


def test_all_result_columns_are_manually_resizable(window):
    """Каждую границу заголовка можно перетаскивать мышью."""
    from PySide6.QtWidgets import QHeaderView

    header = window.results_table.horizontalHeader()
    assert not header.stretchLastSection()
    assert all(
        header.sectionResizeMode(index) == QHeaderView.Interactive
        for index in range(window.results_model.columnCount())
    )


def test_fitted_column_does_not_cut_the_text(window):
    """Подгонка не режет текст многоточием, пока он влезает в предел.

    Обрезка допустима только когда контекст объективно шире верхнего
    предела колонки (зависит от шрифта окружения) — это спроектированный
    потолок, а не ошибка подгонки. Проверяем отсутствие обрезки в пределах
    потолка.
    """
    from PySide6.QtCore import Qt
    from app.gui.main_window import (
        _CONTEXT_COLUMN_MAX_WIDTH,
        _CONTEXT_COLUMN_PADDING,
    )

    _feed_context(window, TYPICAL_CONTEXT)
    width = window.results_table.columnWidth(CONTEXT_COLUMN)
    metrics = window.results_table.fontMetrics()
    text_width = metrics.horizontalAdvance(TYPICAL_CONTEXT)
    # Внутренняя область ячейки: делегат рисует в rect.adjusted(6, 0, -6, 0).
    available = width - 12

    # Неупёршаяся в предел подгонка гарантирует, что текст влезает целиком.
    if text_width <= _CONTEXT_COLUMN_MAX_WIDTH - _CONTEXT_COLUMN_PADDING:
        assert metrics.elidedText(TYPICAL_CONTEXT, Qt.ElideRight, available) == TYPICAL_CONTEXT
    else:
        # Упёрлись в потолок: подгонка корректно ограничена им, текст режется
        # по дизайну, а не из-за просчёта ширины.
        assert width == _CONTEXT_COLUMN_MAX_WIDTH


def test_context_column_respects_its_bounds(window):
    from app.gui.main_window import (
        _CONTEXT_COLUMN_MAX_WIDTH,
        _CONTEXT_COLUMN_MIN_WIDTH,
    )

    _feed_context(window, "…мало…")
    assert window.results_table.columnWidth(CONTEXT_COLUMN) == _CONTEXT_COLUMN_MIN_WIDTH

    window.results_model.clear()
    _feed_context(window, "очень длинный контекст " * 80, matched="контекст")
    assert window.results_table.columnWidth(CONTEXT_COLUMN) == _CONTEXT_COLUMN_MAX_WIDTH


def test_manual_resize_disables_auto_fit(window):
    """Пользователь выставил ширину сам — переопределять её навязчиво."""
    _feed_context(window, TYPICAL_CONTEXT)
    window.results_table.setColumnWidth(CONTEXT_COLUMN, 400)
    assert window._context_column_user_sized

    _feed_context(window, "очень длинный контекст " * 80, matched="контекст", name="g.pdf")
    assert window.results_table.columnWidth(CONTEXT_COLUMN) == 400


def test_auto_fit_grows_with_a_longer_context(window):
    _feed_context(window, "…короткий контекст с совпадением беспилотники тут…")
    narrow = window.results_table.columnWidth(CONTEXT_COLUMN)
    _feed_context(window, TYPICAL_CONTEXT * 2, name="g.pdf")
    assert window.results_table.columnWidth(CONTEXT_COLUMN) > narrow


def test_model_tracks_the_longest_context(qt_app):
    model = ResultsTableModel()
    model.add_results([
        _result(name="a.txt", context="короткий"),
        _result(name="b.txt", context="значительно более длинный контекст"),
    ])
    assert model.longest_context() == "значительно более длинный контекст"
    model.clear()
    assert model.longest_context() == ""


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

    return model.data(model.index(row, 1), Qt.DisplayRole)


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
    shown = {model.data(model.index(row, 7), Qt.DisplayRole) for row in range(model.rowCount())}
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


# --------------------------------------------------------------------- #
# Вкладка «Файлы»: та же подробность, что и во вкладке «Результаты»
# --------------------------------------------------------------------- #
FILE_COLUMN = 1
WORD_COLUMN = 2
OCCURRENCES_COLUMN = 3


def _files_model_with(results):
    """Модель файлов, наполненная через ту же синхронизацию, что и в GUI."""
    from app.gui.results_model import FilesTableModel

    results_model = ResultsTableModel()
    results_model.add_results(list(results))
    files_model = FilesTableModel()
    files_model.sync_with_results(results_model)
    return files_model


def test_files_tab_repeats_the_results_columns(qt_app):
    """Раскладка обеих таблиц совпадает: делегаты и индексы переиспользуются."""
    from PySide6.QtCore import Qt

    files_model = _files_model_with([_result()])
    results_model = ResultsTableModel()
    results_model.add_results([_result()])

    assert files_model.columnCount() == results_model.columnCount()
    headers = [
        files_model.headerData(column, Qt.Horizontal)
        for column in range(files_model.columnCount())
    ]
    assert headers == [
        "✓", "Файл", "Слово", "Кол-во совпадений", "Контекст", "Тип", "Изменён", "Путь",
    ]


def test_files_row_lists_every_term_found_in_the_file(qt_app):
    """Одна строка на файл, но термины файла не теряются."""
    from PySide6.QtCore import Qt

    files_model = _files_model_with([
        _result(word="договор", context="в тексте договор"),
        _result(word="акт", context="и акт тоже"),
        _result(word="договор", context="ещё договор"),
    ])
    index = files_model.index(0, WORD_COLUMN)
    # Дубликаты термина схлопываются, порядок — первого появления.
    assert files_model.data(index, Qt.DisplayRole) == "договор, акт"
    assert files_model.data(files_model.index(0, OCCURRENCES_COLUMN), Qt.DisplayRole) == 3


def test_files_row_exposes_highlight_roles_like_results(qt_app):
    """Подсветка в «Файл» и «Контекст» питается теми же ролями модели."""
    from PySide6.QtCore import Qt
    from app.gui.results_model import FilenameMatchRole, MatchRole

    files_model = _files_model_with([
        SearchResult("dogovor.txt", "/x/dogovor.txt", "договор",
                     "текст договор внутри", "txt", "2026", matched_text="договор"),
        SearchResult("dogovor.txt", "/x/dogovor.txt", "dogovor",
                     "[Имя файла] dogovor.txt", "txt", "2026", matched_text="dogovor"),
    ])

    # Колонка «Файл» подсвечивается совпадением в имени файла.
    assert files_model.data(files_model.index(0, FILE_COLUMN), FilenameMatchRole) is True
    assert files_model.data(files_model.index(0, FILE_COLUMN), MatchRole) == "dogovor"
    # Колонка «Контекст» — совпадением внутри текста документа.
    assert files_model.data(files_model.index(0, CONTEXT_COLUMN), MatchRole) == "договор"
    assert files_model.data(files_model.index(0, CONTEXT_COLUMN), Qt.DisplayRole) == (
        "текст договор внутри"
    )


def test_files_model_tracks_the_longest_context(qt_app):
    """Автоподгонка ширины «Контекста» опирается на ту же величину."""
    files_model = _files_model_with([
        _result(name="a.txt", context="короткий"),
        _result(name="b.txt", context="значительно более длинный контекст"),
    ])
    assert files_model.longest_context() == "значительно более длинный контекст"
    files_model.clear()
    assert files_model.longest_context() == ""


def test_files_context_column_is_fitted_to_the_text(window):
    """Колонка «Контекст» во «Файлах» подгоняется по той же формуле."""
    from app.gui.main_window import (
        _CONTEXT_COLUMN_MAX_WIDTH,
        _CONTEXT_COLUMN_MIN_WIDTH,
        _CONTEXT_COLUMN_PADDING,
    )

    _feed_context(window, TYPICAL_CONTEXT)
    width = window.found_files_table.columnWidth(CONTEXT_COLUMN)
    text_width = window.found_files_table.fontMetrics().horizontalAdvance(TYPICAL_CONTEXT)

    expected = min(_CONTEXT_COLUMN_MAX_WIDTH, text_width + _CONTEXT_COLUMN_PADDING)
    expected = max(_CONTEXT_COLUMN_MIN_WIDTH, expected)
    assert width == expected


def test_manual_resize_disables_files_auto_fit(window):
    """Ширина, выставленная руками, не переопределяется новыми результатами."""
    _feed_context(window, TYPICAL_CONTEXT)
    window.found_files_table.setColumnWidth(CONTEXT_COLUMN, 380)
    assert window._files_context_column_user_sized

    _feed_context(window, "очень длинный контекст " * 80, matched="контекст", name="g.pdf")
    assert window.found_files_table.columnWidth(CONTEXT_COLUMN) == 380


def test_files_tab_context_fit_is_independent_from_results(window):
    """Ручная ширина в одной вкладке не отключает подгонку в другой."""
    _feed_context(window, TYPICAL_CONTEXT)
    window.results_table.setColumnWidth(CONTEXT_COLUMN, 300)

    assert window._context_column_user_sized
    assert not window._files_context_column_user_sized


def test_all_files_columns_are_manually_resizable(window):
    from PySide6.QtWidgets import QHeaderView

    header = window.found_files_table.horizontalHeader()
    assert not header.stretchLastSection()
    assert all(
        header.sectionResizeMode(index) == QHeaderView.Interactive
        for index in range(window.files_model.columnCount())
    )


def test_clicking_the_files_counter_opens_every_context(window, monkeypatch):
    """Число совпадений во «Файлах» кликабельно так же, как в «Результатах»."""
    from app.gui import main_window as main_window_module

    opened = {}

    class _FakeDialog:
        def __init__(self, results, parent=None):
            opened["results"] = list(results)

        def exec(self):
            return 0

    monkeypatch.setattr(main_window_module, "ResultContextsDialog", _FakeDialog)

    _feed_context(window, "первый контекст беспилотники")
    _feed_context(window, "второй контекст беспилотники")
    proxy_index = window.files_proxy_model.index(0, OCCURRENCES_COLUMN)
    window._on_files_table_clicked(proxy_index)

    assert len(opened["results"]) == 2


def test_double_click_on_the_files_counter_does_not_open_the_file(window, monkeypatch):
    opened = []
    monkeypatch.setattr(
        type(window), "_open_path", staticmethod(lambda path: opened.append(path))
    )

    _feed_context(window, TYPICAL_CONTEXT)
    window._open_found_file_row(window.files_proxy_model.index(0, OCCURRENCES_COLUMN))
    window._open_found_file_row(window.files_proxy_model.index(0, CHECK_COLUMN))
    assert opened == []

    window._open_found_file_row(window.files_proxy_model.index(0, FILE_COLUMN))
    assert opened == ["/d/f.pdf"]


def test_contexts_dialog_names_the_term_when_several_are_shown(qt_app):
    """Из «Файлов» приходят разные термины — заголовок обязан их различать."""
    from app.gui.result_contexts_dialog import ResultContextsDialog

    dialog = ResultContextsDialog([
        _result(word="договор", context="про договор"),
        _result(word="акт", context="про акт"),
    ])
    title = dialog.windowTitle()
    assert "«договор»" in title and "«акт»" in title

    single = ResultContextsDialog([_result(word="договор")])
    assert single.windowTitle() == "Совпадения «договор» — a.txt"


# --------------------------------------------------------------------- #
# Безопасные операции по галочкам в обеих вкладках
# --------------------------------------------------------------------- #
CHECK_COLUMN = 0


def _check_all(window):
    window.results_model.set_all_checked(True)
    window.files_model.set_all_checked(True)


def test_checking_a_results_row_selects_the_file_for_operations(window):
    from PySide6.QtCore import Qt

    _feed_context(window, TYPICAL_CONTEXT)
    index = window.results_model.index(0, CHECK_COLUMN)
    assert window.results_model.data(index, Qt.CheckStateRole) == Qt.Unchecked

    window.results_model.setData(index, Qt.Checked, Qt.CheckStateRole)
    assert window.results_model.checked_paths() == ["/d/f.pdf"]
    assert window.results_model.has_checked()


def test_files_and_results_checkboxes_are_independent(window):
    """Вкладки отмечают файлы отдельно — операция берёт свой список."""
    _feed_context(window, TYPICAL_CONTEXT)
    window.results_model.set_all_checked(True)

    assert window.results_model.checked_paths() == ["/d/f.pdf"]
    assert window.files_model.checked_paths() == []


def test_secure_delete_from_results_shreds_the_checked_file(window, tmp_path, monkeypatch):
    """Кнопка «Результатов» действительно удаляет отмеченный файл с диска."""
    from PySide6.QtWidgets import QMessageBox

    victim = tmp_path / "secret.txt"
    victim.write_text("совершенно секретно", encoding="utf-8")

    window._on_results_found([
        SearchResult(victim.name, str(victim), "секрет", "секретно", "txt", "2026")
    ])
    window.results_model.set_all_checked(True)

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)

    window._results_secure_delete_checked()
    assert window.secure_worker is not None
    window.secure_worker.wait(30_000)
    qt_app_process_events(window)

    assert not victim.exists()


def test_secure_move_from_results_copies_then_shreds(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    source = tmp_path / "src" / "doc.txt"
    source.parent.mkdir()
    source.write_text("содержимое", encoding="utf-8")
    destination = tmp_path / "dst"
    destination.mkdir()

    window._on_results_found([
        SearchResult(source.name, str(source), "содержимое", "содержимое", "txt", "2026")
    ])
    window.results_model.set_all_checked(True)

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(destination))
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)

    window._results_secure_move_checked()
    assert window.secure_worker is not None
    window.secure_worker.wait(30_000)
    qt_app_process_events(window)

    assert not source.exists()
    assert (destination / "doc.txt").read_text(encoding="utf-8") == "содержимое"


def test_copy_from_results_keeps_the_original(window, tmp_path, monkeypatch):
    """Копирование отмеченных из «Результатов» не трогает оригиналы."""
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    source = tmp_path / "src" / "doc.txt"
    source.parent.mkdir()
    source.write_text("содержимое", encoding="utf-8")
    destination = tmp_path / "dst"

    window._on_results_found([
        SearchResult(source.name, str(source), "содержимое", "содержимое", "txt", "2026")
    ])
    window.results_model.set_all_checked(True)

    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(destination))
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(type(window), "_open_path", staticmethod(lambda path: None))

    window._results_copy_checked()
    assert window.copy_worker is not None
    window.copy_worker.wait(30_000)
    qt_app_process_events(window)

    assert source.exists(), "копирование не должно удалять оригинал"
    assert (destination / "doc.txt").read_text(encoding="utf-8") == "содержимое"
    # Выбранная папка становится общей для обеих вкладок.
    assert window.copy_dest_edit.text() == str(destination)


def test_operations_refuse_to_run_without_checkboxes(window, monkeypatch):
    """Без галочек операция не стартует, а объясняет, что отметить нечего."""
    from PySide6.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda parent, title, text, *a, **k: shown.append(title)
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

    _feed_context(window, TYPICAL_CONTEXT)
    window._results_secure_delete_checked()
    window._secure_delete_selected()

    assert shown == ["Нет отмеченных строк", "Нет отмеченных файлов"]
    assert window.secure_worker is None


def test_a_second_operation_waits_for_the_running_one(window, monkeypatch):
    """Два разрушающих прохода по одним файлам одновременно недопустимы."""
    from PySide6.QtWidgets import QMessageBox

    _feed_context(window, TYPICAL_CONTEXT)
    window.results_model.set_all_checked(True)
    monkeypatch.setattr(type(window), "_file_op_running", lambda self: True)

    shown = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda parent, title, text, *a, **k: shown.append(title)
    )
    window._results_secure_delete_checked()

    assert shown == ["Операция"]
    assert window.secure_worker is None


def test_running_operation_disables_every_entry_point(window):
    """Пока идёт операция, кнопки обеих вкладок заблокированы."""
    buttons = (
        window.results_copy_btn,
        window.results_secure_delete_btn,
        window.results_secure_move_btn,
        window.copy_btn,
        window.secure_delete_btn,
        window.secure_move_btn,
    )
    window._set_secure_controls_enabled(False)
    assert not any(button.isEnabled() for button in buttons)

    window._set_secure_controls_enabled(True)
    assert all(button.isEnabled() for button in buttons)


def qt_app_process_events(window):
    """Даёт сигналам воркера дойти до окна после wait()."""
    from PySide6.QtWidgets import QApplication

    QApplication.processEvents()


def test_checkbox_delegate_paints_without_crashing(qt_app):
    """QStyleOptionButton нельзя строить из QStyleOptionViewItem.

    Такой вызов PySide6 отвергает TypeError-ом внутри override-а paint(),
    и отрисовка таблицы валится вместе с процессом — регрессия видна только
    при реальном рендеринге, не при обращении к модели.
    """
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem
    from app.gui.results_model import CheckboxDelegate

    model = ResultsTableModel()
    model.add_results([_result()])
    model.setData(model.index(0, CHECK_COLUMN), Qt.Checked, Qt.CheckStateRole)

    image = QImage(40, 24, QImage.Format_ARGB32)
    painter = QPainter(image)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 40, 24)
    try:
        CheckboxDelegate().paint(painter, option, model.index(0, CHECK_COLUMN))
    finally:
        painter.end()


def test_right_click_does_not_toggle_the_checkbox(qt_app):
    """Правая кнопка открывает меню операций и не должна менять отметку."""
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QStyleOptionViewItem
    from app.gui.results_model import CheckboxDelegate

    model = ResultsTableModel()
    model.add_results([_result()])
    index = model.index(0, CHECK_COLUMN)
    delegate = CheckboxDelegate()

    def _release(button):
        return QMouseEvent(
            QEvent.MouseButtonRelease,
            QPointF(QPoint(5, 5)),
            QPointF(QPoint(5, 5)),
            button,
            button,
            Qt.NoModifier,
        )

    delegate.editorEvent(_release(Qt.RightButton), model, QStyleOptionViewItem(), index)
    assert model.data(index, Qt.CheckStateRole) == Qt.Unchecked

    delegate.editorEvent(_release(Qt.LeftButton), model, QStyleOptionViewItem(), index)
    assert model.data(index, Qt.CheckStateRole) == Qt.Checked
