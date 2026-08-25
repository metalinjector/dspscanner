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


def test_secure_delete_shreds_the_checked_file(window, tmp_path, monkeypatch):
    """Кнопка действительно удаляет отмеченный файл с диска."""
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

    window._secure_delete_checked()
    assert window.secure_worker is not None
    window.secure_worker.wait(30_000)
    qt_app_process_events(window)

    assert not victim.exists()


def test_secure_move_copies_then_shreds(window, tmp_path, monkeypatch):
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

    window._secure_move_checked()
    assert window.secure_worker is not None
    window.secure_worker.wait(30_000)
    qt_app_process_events(window)

    assert not source.exists()
    assert (destination / "doc.txt").read_text(encoding="utf-8") == "содержимое"


def test_copy_keeps_the_original(window, tmp_path, monkeypatch):
    """Копирование отмеченных не трогает оригиналы."""
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

    window._copy_checked()
    assert window.copy_worker is not None
    window.copy_worker.wait(30_000)
    qt_app_process_events(window)

    assert source.exists(), "копирование не должно удалять оригинал"
    assert (destination / "doc.txt").read_text(encoding="utf-8") == "содержимое"
    # Выбранная папка запоминается в поле рядом с кнопкой копирования.
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
    window._secure_delete_checked()
    window._secure_move_checked()
    window._copy_checked()

    assert shown == ["Нет отмеченных файлов"] * 3
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
    window._secure_delete_checked()

    assert shown == ["Операция"]
    assert window.secure_worker is None


def test_running_operation_disables_every_entry_point(window):
    """Пока идёт операция, все кнопки над файлами заблокированы."""
    buttons = (window.copy_btn, window.secure_delete_btn, window.secure_move_btn)

    _feed_context(window, TYPICAL_CONTEXT)
    window._set_all_checked(True)
    assert all(button.isEnabled() for button in buttons)

    window._set_secure_controls_enabled(False)
    assert not any(button.isEnabled() for button in buttons)

    window._set_secure_controls_enabled(True)
    assert all(button.isEnabled() for button in buttons)


def test_buttons_stay_disabled_while_nothing_is_checked(window):
    """Разблокировка не должна «оживлять» кнопки на пустом выборе."""
    buttons = (window.copy_btn, window.secure_delete_btn, window.secure_move_btn)

    _feed_context(window, TYPICAL_CONTEXT)
    assert not any(button.isEnabled() for button in buttons)

    window._set_secure_controls_enabled(True)
    assert not any(button.isEnabled() for button in buttons)


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


# --------------------------------------------------------------------- #
# Единая вкладка «Результаты» с переключателем группировки
# --------------------------------------------------------------------- #
def test_files_tab_is_gone(window):
    """Вкладок остаётся две: «Результаты» и «Журнал»."""
    titles = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    assert len(titles) == 2
    assert titles[0].startswith("Результаты")
    assert titles[1] == "Журнал"


def test_both_groupings_live_in_one_tab(window):
    """Обе таблицы — страницы одного стека, а не разные вкладки."""
    from app.gui.main_window import _GROUP_BY_FILE, _GROUP_BY_MATCH

    assert window.results_stack.count() == 2
    assert window.results_stack.widget(_GROUP_BY_MATCH) is window.results_table
    assert window.results_stack.widget(_GROUP_BY_FILE) is window.found_files_table
    # По умолчанию — привычный режим совпадений.
    assert window.results_stack.currentIndex() == _GROUP_BY_MATCH
    assert window.active_table() is window.results_table


def test_switching_grouping_swaps_the_visible_table(window):
    from app.gui.main_window import _GROUP_BY_FILE, _GROUP_BY_MATCH

    _feed_context(window, TYPICAL_CONTEXT)

    window.group_by_file_radio.setChecked(True)
    assert window.results_stack.currentIndex() == _GROUP_BY_FILE
    assert window.active_table() is window.found_files_table
    assert window.active_model() is window.files_model

    window.group_by_match_radio.setChecked(True)
    assert window.results_stack.currentIndex() == _GROUP_BY_MATCH
    assert window.active_model() is window.results_model


def test_grouping_changes_row_count_but_not_the_columns(window):
    """Ровно то, ради чего режимы и нужны: одни данные, разная детализация."""
    window._on_results_found([
        SearchResult("a.txt", "/x/a.txt", "договор", "про договор", "txt", "2026"),
        SearchResult("a.txt", "/x/a.txt", "акт", "про акт", "txt", "2026"),
    ])

    assert window.results_model.rowCount() == 2, "строка на каждое слово"
    assert window.files_model.rowCount() == 1, "одна строка на файл"
    assert window.results_model.columnCount() == window.files_model.columnCount()


def test_checkmarks_survive_a_grouping_switch(window):
    """Пользователь отмечает файлы, а не строки: смена вида их не теряет."""
    window._on_results_found([
        SearchResult("a.txt", "/x/a.txt", "договор", "про договор", "txt", "2026"),
        SearchResult("b.txt", "/x/b.txt", "акт", "про акт", "txt", "2026"),
    ])

    index = window.results_model.index(0, CHECK_COLUMN)
    window.results_model.setData(index, Qt_checked(), Qt_check_role())
    checked_before = window.results_model.checked_paths()

    window.group_by_file_radio.setChecked(True)
    assert window.files_model.checked_paths() == checked_before

    window.group_by_match_radio.setChecked(True)
    assert window.results_model.checked_paths() == checked_before


def test_one_filter_serves_both_groupings(window):
    """Фильтр общий: набранный запрос не сбрасывается переключением вида."""
    window._on_results_found([
        SearchResult("a.txt", "/x/a.txt", "договор", "про договор", "txt", "2026"),
        SearchResult("b.txt", "/x/b.txt", "акт", "про акт", "txt", "2026"),
    ])

    window.filter_edit.setText("договор")
    assert window.proxy_model.rowCount() == 1
    assert window.files_proxy_model.rowCount() == 1

    window.group_by_file_radio.setChecked(True)
    assert window.filter_edit.text() == "договор"
    assert window.files_proxy_model.rowCount() == 1


def test_select_all_applies_to_the_visible_grouping(window):
    window._on_results_found([
        SearchResult("a.txt", "/x/a.txt", "договор", "про договор", "txt", "2026"),
        SearchResult("b.txt", "/x/b.txt", "акт", "про акт", "txt", "2026"),
    ])

    window.group_by_file_radio.setChecked(True)
    window._set_all_checked(True)
    assert sorted(window.files_model.checked_paths()) == ["/x/a.txt", "/x/b.txt"]
    # И сразу же доступны во втором режиме.
    assert sorted(window.results_model.checked_paths()) == ["/x/a.txt", "/x/b.txt"]

    window._set_all_checked(False)
    assert window.files_model.checked_paths() == []
    assert window.results_model.checked_paths() == []


def test_operations_use_the_visible_grouping(window, tmp_path, monkeypatch):
    """Удаление в режиме «по файлам» берёт галочки этой же таблицы."""
    from PySide6.QtWidgets import QMessageBox

    victim = tmp_path / "secret.txt"
    victim.write_text("секретно", encoding="utf-8")
    window._on_results_found([
        SearchResult(victim.name, str(victim), "секрет", "секретно", "txt", "2026")
    ])

    window.group_by_file_radio.setChecked(True)
    window.files_model.set_all_checked(True)
    assert window._checked_paths() == [str(victim)]

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)

    window._secure_delete_checked()
    assert window.secure_worker is not None
    window.secure_worker.wait(30_000)
    qt_app_process_events(window)

    assert not victim.exists()


def test_destructive_buttons_live_in_their_own_box(window):
    """Необратимые операции отделены рамкой от копирования и экспорта."""
    from PySide6.QtWidgets import QGroupBox

    def _box_of(button):
        parent = button.parent()
        while parent is not None and not isinstance(parent, QGroupBox):
            parent = parent.parent()
        return parent

    danger_box = _box_of(window.secure_delete_btn)
    assert danger_box is not None
    assert danger_box is _box_of(window.secure_move_btn)
    assert "Безопасные операции" in danger_box.title()

    # Обычное копирование остаётся снаружи опасной рамки.
    assert _box_of(window.copy_btn) is not danger_box


def test_summary_counts_files_not_rows(window):
    """В режиме совпадений строк больше, чем файлов, — счётчик про файлы."""
    window._on_results_found([
        SearchResult("a.txt", "/x/a.txt", "договор", "про договор", "txt", "2026"),
        SearchResult("a.txt", "/x/a.txt", "акт", "про акт", "txt", "2026"),
    ])
    window._set_all_checked(True)

    assert window.results_model.rowCount() == 2
    assert "отмечено файлов: 1" in window.checked_summary_label.text()


def test_tab_title_shows_the_number_of_files(window):
    _feed_context(window, TYPICAL_CONTEXT)
    assert window.tabs.tabText(0) == "Результаты (1)"


def Qt_checked():
    from PySide6.QtCore import Qt

    return Qt.Checked


def Qt_check_role():
    from PySide6.QtCore import Qt

    return Qt.CheckStateRole


def test_disabled_coloured_buttons_look_disabled(qt_app):
    """Заблокированная «Безопасно удалить» не должна оставаться красной.

    У #dangerButton и #primaryButton фон задан явно, и общее правило
    QPushButton:disabled его не перебивает — кнопка выглядела нажимаемой,
    хотя ничего не делала. Для необратимого удаления это опаснее всего.
    """
    from app.gui.theme import apply_modern_theme

    apply_modern_theme(qt_app)
    style = qt_app.styleSheet()

    assert "QPushButton#dangerButton:disabled" in style
    assert "QPushButton#primaryButton:disabled" in style


def test_danger_zone_has_its_own_frame_style(qt_app):
    """Рамка необратимых операций отличается от обычных групп."""
    from app.gui.theme import apply_modern_theme

    apply_modern_theme(qt_app)
    assert "QGroupBox#dangerZoneBox" in qt_app.styleSheet()


def test_tooltip_html_renders_the_blank_line_between_paragraphs(qt_app):
    """Пустая строка исходника доходит до подсказки как пустая строка.

    Между движком и всплывающей подсказкой два места, где перенос мог
    потеряться: экранирование HTML и стиль контейнера. Проверяем итоговую
    разметку, а не только результат поиска.
    """
    from app.gui.results_model import result_tooltip_html

    result = SearchResult(
        "b.pdf",
        "/x/b.pdf",
        "беспилотники",
        "…короткий контекст…",
        "pdf",
        "2026-01-01",
        tooltip_context="Для служебного пользования, беспилотники.\n\nЛекарственные поражения печени.",
    )

    html = result_tooltip_html(result)

    # Абзацы разделены двумя <br>, а не склеены в один поток.
    assert "<br><br>Лекарственные" in html.replace("<br />", "<br>")
    # Сырой перевод строки в разметку не попадает — иначе pre-wrap удвоит отступ.
    assert "\n" not in html


def test_checkbox_delegate_draws_exactly_one_indicator(qt_app):
    """Делегат не должен рисовать чекбокс поверх нарисованного стилем.

    QTableView сам рисует индикатор, когда модель отдаёт CheckStateRole:
    вместе с индикатором делегата получались два наложенных чекбокса, и при
    нажатии они расходились по состоянию — «двоение». Делегат обязан снять
    флаг HasCheckIndicator перед тем, как отдать ячейку стилю.
    """
    from PySide6.QtWidgets import QStyleOptionViewItem
    from app.gui.results_model import CheckboxDelegate

    delegate = CheckboxDelegate()
    model = ResultsTableModel()
    model.add_results([_result()])
    index = model.index(0, 0)

    # Модель действительно отдаёт состояние галочки, иначе тест бессмыслен.
    option = QStyleOptionViewItem()
    delegate.initStyleOption(option, index)
    assert option.features & QStyleOptionViewItem.HasCheckIndicator

    seen_features = []

    class _Style:
        def drawControl(self, element, opt, painter, widget=None):
            seen_features.append(bool(opt.features & QStyleOptionViewItem.HasCheckIndicator))

    class _Painter:
        """Заглушка QPainter: считает вызовы, ничего не рисуя."""

        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def record(*args, **kwargs):
                self.calls.append(name)

            return record

    class _Option(QStyleOptionViewItem):
        """Подсовывает делегату наш _Style вместо системного."""

        @property
        def widget(self):
            return None

    painter = _Painter()
    fake_option = QStyleOptionViewItem(option)
    monkey_style = _Style()

    import app.gui.results_model as rm

    original = rm.QApplication.style
    rm.QApplication.style = staticmethod(lambda: monkey_style)
    try:
        delegate.paint(painter, fake_option, index)
    finally:
        rm.QApplication.style = original

    assert seen_features == [False], "стилю ушёл флаг индикатора — будет второй чекбокс"
    # Свой индикатор делегат всё-таки нарисовал.
    assert "drawRoundedRect" in painter.calls


def test_checkbox_indicator_fits_inside_the_narrow_column(qt_app):
    """Индикатор помещается в колонку ✓ целиком, а не наполовину.

    Системный чекбокс прижимался к левому краю ячейки и обрезался: колонка
    шириной 34 px уже, чем отступы стиля.
    """
    from app.gui.results_model import CheckboxDelegate

    assert CheckboxDelegate._BOX_SIDE <= 34 - 4
    # Строка таблицы имеет высоту 24 px — индикатор должен в неё влезать.
    assert CheckboxDelegate._BOX_SIDE <= 24 - 4


# --------------------------------------------------------------------- #
# Списки ключевых слов: кнопка + модалка вместо двух textarea
# --------------------------------------------------------------------- #


def test_terms_field_keeps_the_text_api_of_the_old_editor(qt_app):
    """Кнопка должна вести себя как прежний QTextEdit.

    Сохранение настроек, автосохранение и запуск поиска работают с полем
    как с текстом. Если API разойдётся, конфигурация молча перестанет
    сохранять ключевые слова.
    """
    from app.gui.terms_field import TermsField

    field = TermsField("Слова", "Диалог", "пусто")
    field.setPlainText("пилот, служебная записка\nдоговор")

    assert field.terms() == ["пилот", "служебная записка", "договор"]
    # Ровно то, что уйдёт в настройки: по одному значению в строке.
    assert field.toPlainText() == "пилот\nслужебная записка\nдоговор"

    changes = []
    field.textChanged.connect(lambda: changes.append(field.toPlainText()))
    field.set_terms(["новое"])
    assert changes == ["новое"]

    # Повторная установка того же значения не должна дёргать автосохранение.
    field.set_terms(["новое"])
    assert len(changes) == 1


def test_terms_field_button_shows_the_count_and_a_preview(qt_app):
    """На кнопке видно, сколько терминов задано, без открытия модалки."""
    from app.gui.terms_field import TermsField

    field = TermsField("Слова в содержимом", "Диалог", "список пуст")
    assert "список пуст" in field.text()

    field.set_terms(["пилот", "договор", "секретно", "охрана", "приказ"])
    label = field.text()
    assert "5" in label
    assert "пилот" in label
    # Превью не обрывается посреди слова: остаток сворачивается в счётчик.
    assert "и ещё" in label or "пилот, договор, секретно, охрана, приказ" in label


def test_terms_dialog_adds_parses_and_deduplicates(qt_app):
    """Поле ввода принимает несколько значений сразу и не плодит дубликаты."""
    from app.gui.terms_field import TermsDialog

    dialog = TermsDialog("Ключевые слова", ["договор"])

    dialog.input.setText("пилот, служебная записка; охран*")
    dialog._add_from_input()
    assert dialog.terms() == ["договор", "пилот", "служебная записка", "охран*"]

    # Регистр и «ё» не создают второй записи — как и в parse_terms.
    dialog.input.setText("ДОГОВОР")
    dialog._add_from_input()
    assert dialog.terms().count("договор") == 1
    assert len(dialog.terms()) == 4


def test_terms_dialog_removes_only_checked_rows(qt_app):
    """Удаляются отмеченные галочками строки, остальные остаются нетронутыми."""
    from PySide6.QtCore import Qt as _Qt
    from app.gui.terms_field import TermsDialog

    dialog = TermsDialog("Ключевые слова", ["пилот", "договор", "секретно"])
    dialog.list.item(1).setCheckState(_Qt.Checked)

    dialog._remove_checked()

    assert dialog.terms() == ["пилот", "секретно"]


def test_terms_dialog_remove_button_needs_a_checkmark(qt_app):
    """Без отметок «Удалить» заблокирована — нечего удалять."""
    from PySide6.QtCore import Qt as _Qt
    from app.gui.terms_field import TermsDialog

    dialog = TermsDialog("Ключевые слова", ["пилот", "договор"])
    assert not dialog.remove_btn.isEnabled()

    dialog.list.item(0).setCheckState(_Qt.Checked)
    assert dialog.remove_btn.isEnabled()

    dialog._set_all_checked(False)
    assert not dialog.remove_btn.isEnabled()


def test_terms_dialog_rows_are_checkable(qt_app):
    """Каждая строка списка — одно значение с чекбоксом."""
    from PySide6.QtCore import Qt as _Qt
    from app.gui.terms_field import TermsDialog

    terms = ["пилот", "служебная записка", "договор"]
    dialog = TermsDialog("Ключевые слова", terms)

    assert dialog.list.count() == len(terms)
    for row, term in enumerate(terms):
        item = dialog.list.item(row)
        assert item.text() == term
        assert item.flags() & _Qt.ItemIsUserCheckable
        assert item.checkState() == _Qt.Unchecked


def test_terms_tooltip_scrolls_up_and_wraps_around(qt_app):
    """Подсказка прокручивает перечень вверх, дойдя до конца — начинает заново."""
    from app.gui.terms_field import TermsTooltip

    tooltip = TermsTooltip()
    tooltip.set_terms("Слова: 40", [f"термин {i}" for i in range(40)])
    tooltip.start()

    bar = tooltip._area.verticalScrollBar()
    assert bar.maximum() > 0, "длинный список должен быть прокручиваемым"

    tooltip._scroll_once()
    assert bar.value() > 0, "перечень не поехал вверх"

    bar.setValue(bar.maximum())
    tooltip._scroll_once()
    assert bar.value() == 0, "после конца список должен начинаться сначала"

    tooltip.stop()
    assert not tooltip._timer.isActive()


def test_terms_tooltip_does_not_scroll_a_short_list(qt_app):
    """Короткий список не дёргается: прокручивать нечего."""
    from app.gui.terms_field import TermsTooltip

    tooltip = TermsTooltip()
    tooltip.set_terms("Слова: 2", ["пилот", "договор"])
    tooltip.start()

    assert not tooltip._timer.isActive()
    tooltip.stop()


def test_left_panel_uses_term_buttons_instead_of_text_areas(window):
    """В левой панели больше нет многострочных полей ввода терминов."""
    from PySide6.QtWidgets import QTextEdit
    from app.gui.terms_field import TermsField

    assert isinstance(window.words_edit, TermsField)
    assert isinstance(window.filename_words_edit, TermsField)
    assert not isinstance(window.words_edit, QTextEdit)


def test_search_still_reads_terms_from_the_buttons(window):
    """Поиск получает термины из кнопок — конвейер не заметил подмены."""
    window.words_edit.set_terms(["пилот", "служебная записка"])
    window.filename_words_edit.set_terms(["договор"])

    settings = window._collect_settings()

    assert settings.words == ["пилот", "служебная записка"]
    assert settings.filename_words == ["договор"]


def test_terms_survive_a_save_and_load_cycle(tmp_path, qt_app, monkeypatch):
    """Список терминов переживает перезапуск приложения.

    Настройки хранят термины строкой; после замены textarea на кнопку
    сохранение и чтение должны сойтись, иначе ключевые слова тихо пропадут
    при следующем запуске.
    """
    from app.logging_utils import setup_logger
    from app.gui.main_window import MainWindow

    logger, handler = setup_logger()

    first = MainWindow(logger=logger, qt_log_handler=handler)
    first.words_edit.set_terms(["пилот", "служебная записка"])
    first.filename_words_edit.set_terms(["договор"])
    first._auto_save_scan_config()
    first.close()

    second = MainWindow(logger=logger, qt_log_handler=handler)
    try:
        assert second.words_edit.terms() == ["пилот", "служебная записка"]
        assert second.filename_words_edit.terms() == ["договор"]
    finally:
        second.close()


def test_terms_dialog_shows_ruled_lines_when_empty(qt_app):
    """Пустой список выглядит разлинованным листом, а не голой рамкой.

    Разлиновку строк даёт QSS, но у пустого списка строк нет — по одной
    рамке непонятно, что значения вносятся по одному в строку.
    """
    from app.gui.terms_field import RuledList, TermsDialog

    dialog = TermsDialog("Ключевые слова", [])

    assert isinstance(dialog.list, RuledList)
    assert dialog.list._empty_hint, "нет подсказки о том, как заполнять список"
    # Отрисовка пустого списка не должна падать.
    dialog.list.resize(300, 200)
    dialog.list.grab()
