"""
Главное окно приложения DSP Scanner.

Аудит v3 (реализация рекомендаций отчёта по отладке):
    1. Автопереключение на вкладку "Результаты" (`setCurrentIndex(0)`)
       при старте сканирования, чтобы пользователь сразу видел совпадения.
    2. Добавлена подробная диагностика в журнал при старте сканирования
       (пути, ключевые слова, выбранные типы, статус OCR).
    3. Компактный список обрабатываемых файлов (`processing_list`, макс. 50 шт.)
       размещён ВЫШЕ прогресс-бара, чтобы наглядно видеть процесс в реальном
       времени.
    4. Статус-метка (`progress_label`) сделана фиксированной по структуре:
       только `Обработано: X / Y`, что исключает скачки ширины.
    5. Добавлена кнопка "🔍 Проверить 1 файл…" для быстрой изоляции и
       диагностики проблем чтения конкретного документа минуя настройки
       поиска.
    6. Кнопка "Стоп" визуально меняет состояние на красное во время
       активного сканирования (через QSS `[active="true"]`).
    7. Если совпадений 0, всплывает информативное окно с возможными причинами
       и рекомендациями.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from html import escape
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QSize, QSortFilterProxyModel, QSettings, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QIcon,
    QPixmap,
    QPainter,
    QClipboard,
)
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QTabWidget,
    QGroupBox,
    QListWidget,
    QListView,
    QPushButton,
    QLabel,
    QLineEdit,
    QTextEdit,
    QCheckBox,
    QComboBox,
    QSpinBox,
    QDoubleSpinBox,
    QProgressBar,
    QTableView,
    QAbstractItemView,
    QFileDialog,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QHeaderView,
    QStatusBar,
    QListWidgetItem,
    QMenu,
    QDialog,
    QDialogButtonBox,
    QApplication,
    QScrollArea,
    QFrame,
)

from app.config import (
    APP_VERSION,
    CONTEXT_CHARS_DEFAULT,
    MAX_FILE_SIZE_MB_DEFAULT,
    PDF_PAGE_LIMIT_DEFAULT,
    DocReadMethod,
    ScanSettings,
    SUPPORTED_EXTENSIONS,
    DEFAULT_COPY_DESTINATION,
    REPORTS_DIR,
    ScanReport,
)
from app.gui.results_model import (
    ResultsTableModel,
    HighlightDelegate,
    file_tooltip_html,
)
from app.gui.workers import (
    ScanWorker,
    CopyWorker,
    SecureOpWorker,
    EmailReportWorker,
    SingleFileTestWorker,
)
from app.gui.settings_dialog import SettingsDialog
from app.gui.email_settings_dialog import EmailSettingsDialog
from app.gui.unreadable_files_dialog import UnreadableFilesDialog
from app.gui.result_contexts_dialog import ResultContextsDialog
from app.fileops.file_operations import CopyMapping, OperationResult
from app.export.excel_export import export_to_excel
from app.export.text_exports import export_to_csv, export_to_markdown
from app.scanning.term_parser import parse_terms
from app.email_settings import EmailSettings, load_email_settings
from app.reporting import build_search_info
from app.settings_store import AppSettings, load_settings, save_settings
from app.default_paths import default_search_paths
from app.scan_config_store import (
    auto_config_path,
    ensure_config_dir,
    existing_auto_config_path,
    load_scan_config,
    save_scan_config,
)


_CONTEXT_COLUMN_INDEX = 3
_CONTEXT_COLUMN_MIN_WIDTH = 260
_CONTEXT_COLUMN_MAX_WIDTH = 900
# Поля ячейки в делегате подсветки плюс запас на полужирные фрагменты
# совпадений: измеряем обычным шрифтом, а рисуются они шире.
_CONTEXT_COLUMN_PADDING = 28


def _resource_path(relative: str) -> Path:
    """Путь к ресурсу в исходниках и в будущей сборке PyInstaller."""
    import sys

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / relative


def _make_app_icon() -> QIcon:
    """Загружает фирменную многомасштабную иконку с безопасным fallback."""
    for relative in ("assets/dsp_scanner.ico", "assets/dsp_scanner.svg"):
        icon_path = _resource_path(relative)
        if icon_path.is_file():
            icon = QIcon(str(icon_path))
            if not icon.isNull():
                return icon

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#163A70"/>'
        '<path d="M16 12h25l9 9v23a8 8 0 0 1-8 8H16a8 8 0 0 1-8-8V20a8 8 0 0 1 8-8z" fill="#2F7DE1"/>'
        '<path d="M41 12v10h9" fill="none" stroke="#BFE0FF" stroke-width="3"/>'
        '<circle cx="29" cy="32" r="9" fill="none" stroke="white" stroke-width="4"/>'
        '<path d="M36 39l9 9" stroke="white" stroke-width="4" stroke-linecap="round"/>'
        "</svg>"
    )
    from PySide6.QtSvg import QSvgRenderer

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    renderer = QSvgRenderer(svg.encode())
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


class StatCard(QWidget):
    """Карточка статистики с фиксированными размерами."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setAlignment(Qt.AlignCenter)
        self.value_label = QLabel("0")
        self.value_label.setObjectName("cardValue")
        self.value_label.setAlignment(Qt.AlignCenter)
        caption = QLabel(label)
        caption.setObjectName("cardLabel")
        caption.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.value_label)
        layout.addWidget(caption)
        self.setFixedSize(110, 64)

    def set_value(self, value) -> None:
        try:
            text = f"{int(value):,}".replace(",", " ")
        except (TypeError, ValueError):
            text = str(value)
        self.value_label.setText(text)
        # Карточка занимает фиксированную ширину (setFixedSize) — очень
        # большие счётчики (сотни тысяч файлов) уменьшают кегль, а не
        # обрезаются молча без многоточия.
        font = self.value_label.font()
        font.setPointSize(22 if len(text) <= 7 else (18 if len(text) <= 9 else 14))
        self.value_label.setFont(font)


class MainWindow(QMainWindow):
    def __init__(self, logger: logging.Logger, qt_log_handler, parent=None):
        super().__init__(parent)
        self.logger = logger
        self.qt_log_handler = qt_log_handler
        self.settings_store = QSettings("DSPScanner", "DSPScanner")

        self.setWindowTitle("DSP Scanner — поиск текста в документах")
        self.resize(1400, 880)
        self.setMinimumSize(1100, 700)

        icon = _make_app_icon()
        self.setWindowIcon(icon)
        QApplication.setWindowIcon(icon)

        self.results_model = ResultsTableModel(self)
        self.proxy_model = QSortFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.results_model)
        self.proxy_model.setFilterKeyColumn(-1)
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy_model.setDynamicSortFilter(False)

        self.scan_worker: Optional[ScanWorker] = None
        self.copy_worker: Optional[CopyWorker] = None
        self.secure_worker: Optional[SecureOpWorker] = None
        self.error_copy_worker: Optional[CopyWorker] = None
        self.error_secure_worker: Optional[SecureOpWorker] = None
        self.email_worker: Optional[EmailReportWorker] = None
        self.single_file_worker: Optional[SingleFileTestWorker] = None
        self.copy_mappings: List[CopyMapping] = []
        self.last_report: Optional[ScanReport] = None
        self.last_search_info: dict = {}
        self._active_search_info: dict = {}
        self._found_file_paths: set[str] = set()
        self._unreadable_entries = []
        self.email_settings = EmailSettings()
        self._closing_after_scan = False
        # Флаги подгонки ширины «Контекста» выставляются до _build_ui():
        # обработчик sectionResized подключается уже там.
        self._context_column_user_sized = False
        self._adjusting_context_column = False
        self._restoring_scan_config = True
        self._updating_paths_programmatically = False
        self._paths_mode = "custom"

        # Таймер для отображения длительности сканирования в реальном времени
        self._scan_timer: Optional[QTimer] = None
        self._scan_start_time: float = 0.0

        self._build_menu()
        self._build_ui()
        self._connect_log_handler()
        self._setup_auto_save()
        self._restore_settings()
        self._restoring_scan_config = False

        # Восстановление геометрии окна и состояния сплиттера
        geom = self.settings_store.value("geometry")
        if geom:
            self.restoreGeometry(geom)
        sp = self.settings_store.value("splitter")
        if sp:
            self.splitter.restoreState(sp)
        # Создаёт переносимый JSON уже при первом запуске, даже если пользователь
        # пока не изменил ни одного поля.
        self._auto_save_scan_config()

        # Обнуление статистики при новом запуске (если включено в настройках)
        if load_settings().reset_stats_on_start:
            self._reset_statistics()

    # ------------------------------------------------------------------ #
    # Меню
    # ------------------------------------------------------------------ #
    def _build_menu(self) -> None:
        menubar = self.menuBar()

        # Файл
        file_menu = menubar.addMenu("&Файл")

        save_cfg_action = QAction("Сохранить конфигурацию поиска…", self)
        save_cfg_action.setShortcut("Ctrl+S")
        save_cfg_action.setStatusTip("Сохранить конфигурацию поиска в файл")
        save_cfg_action.triggered.connect(self._save_scan_config)
        file_menu.addAction(save_cfg_action)

        load_cfg_action = QAction("Загрузить конфигурацию поиска…", self)
        load_cfg_action.setShortcut("Ctrl+O")
        load_cfg_action.setStatusTip("Загрузить конфигурацию поиска из файла")
        load_cfg_action.triggered.connect(self._load_scan_config)
        file_menu.addAction(load_cfg_action)

        file_menu.addSeparator()

        test_action = QAction("Проверить 1 файл (быстрая диагностика)…", self)
        test_action.setShortcut("Ctrl+T")
        test_action.setStatusTip("Проверить извлечение текста из одного файла")
        test_action.triggered.connect(self._test_single_file)
        file_menu.addAction(test_action)

        file_menu.addSeparator()

        exit_action = QAction("Выход", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.setStatusTip("Выход из программы")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Настройки
        settings_menu = menubar.addMenu("&Настройки")
        params_action = QAction("Параметры…", self)
        params_action.setShortcut("Ctrl+,")
        params_action.setStatusTip("Параметры приложения")
        params_action.triggered.connect(self._open_settings)
        settings_menu.addAction(params_action)

        email_action = QAction("Почта и автоматическая отправка…", self)
        email_action.setShortcut("Ctrl+E")
        email_action.setStatusTip("Настройки отправки отчёта по email")
        email_action.triggered.connect(self._open_email_settings)
        settings_menu.addAction(email_action)

        # Справка
        help_menu = menubar.addMenu("&Справка")
        about_action = QAction("О программе", self)
        about_action.setStatusTip("О программе")
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("centralWidget")
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 6)
        root_layout.setSpacing(8)

        # --- Блок мониторинга: список файлов + прогресс-бар ---
        monitor_box = QWidget()
        monitor_layout = QVBoxLayout(monitor_box)
        monitor_layout.setContentsMargins(0, 0, 0, 0)
        monitor_layout.setSpacing(6)

        # Список обрабатываемых файлов (макс 50 шт.)
        processing_caption = QLabel("Обрабатывается:")
        processing_caption.setObjectName("hintLabel")
        monitor_layout.addWidget(processing_caption)
        self.processing_list = QListWidget()
        self.processing_list.setObjectName("processingList")
        self.processing_list.setMaximumHeight(68)  # 3 строки
        self.processing_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.processing_list.setFocusPolicy(Qt.NoFocus)
        monitor_layout.addWidget(self.processing_list)

        # Прогресс-бар и Стоп
        progress_row = QHBoxLayout()
        progress_row.setSpacing(8)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setMinimumWidth(400)
        progress_row.addWidget(self.progress_bar, 1)

        self.progress_label = QLabel("Обработано: 0 / 0")
        self.progress_label.setMinimumWidth(200)
        self.progress_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        progress_row.addWidget(self.progress_label)

        self.stop_btn = QPushButton("■ Стоп")
        self.stop_btn.setObjectName("stopButton")
        self.stop_btn.setProperty("active", False)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._cancel_scan)
        self.stop_btn.setFixedWidth(100)
        progress_row.addWidget(self.stop_btn)
        monitor_layout.addLayout(progress_row)

        root_layout.addWidget(monitor_box)

        # --- Сплиттер: слева настройки, справа вкладки ---
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(4)
        self.splitter.addWidget(self._build_left_panel())
        self.splitter.addWidget(self._build_right_panel())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        # Немного расширяем левую часть: три основных чекбокса параметров
        # должны помещаться в одну строку без переноса и наложения.
        self.splitter.setSizes([500, 900])
        root_layout.addWidget(self.splitter, 1)

        self.setCentralWidget(central)

        # --- Статус-бар ---
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Готово")

    # -- Левая панель (настройки + статистика + кнопки) ------------------- #
    @staticmethod
    def _divider() -> QFrame:
        """Вогнутая (гравированная) разделительная линия между секциями:
        тёмная кромка сверху + светлый блик снизу дают эффект углубления."""
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(4)
        return line

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("sectionLabel")
        return label

    def _help_button(self, title: str, text: str) -> QPushButton:
        button = QPushButton("?")
        button.setObjectName("helpButton")
        button.setFixedSize(22, 22)
        button.setToolTip("Показать пояснение")
        button.clicked.connect(lambda: QMessageBox.information(self, title, text))
        return button

    def _section_header(self, text: str, help_title: str, help_text: str) -> QWidget:
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self._section_label(text))
        row.addWidget(self._help_button(help_title, help_text))
        row.addStretch(1)
        return header

    def _build_left_panel(self) -> QWidget:
        """Панель настроек.

        Редизайн v4: прежняя компоновка (три QGroupBox + QGridLayout) при
        нехватке высоты окна «складывалась»: минимальная высота содержимого
        превышала доступную, и Qt накладывал строки сетки друг на друга
        (комбобокс «Метод .doc» наезжал на чекбокс «Целые слова», спинбоксы
        слипались). Теперь панель:
          * обёрнута в QScrollArea — при любой высоте окна содержимое
            сохраняет свои размеры, а при нехватке места появляется
            прокрутка вместо наложения;
          * свёрстана плоскими секциями с вогнутыми линиями-разделителями
            вместо тяжёлых рамок GroupBox — компактнее по вертикали и чище;
          * каждый ряд параметров имеет гарантированную высоту.
        """
        container = QWidget()
        container.setObjectName("leftPanel")
        container.setMinimumWidth(475)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 10, 4)
        layout.setSpacing(8)

        # ---- Секция: пути -------------------------------------------- #
        paths_header = QWidget(container)
        paths_header_layout = QHBoxLayout(paths_header)
        paths_header_layout.setContentsMargins(0, 0, 0, 0)
        paths_header_layout.setSpacing(8)
        self.default_paths_btn = QPushButton("Выставить по умолчанию")
        self.default_paths_btn.setObjectName("toolButton")
        self.default_paths_btn.setToolTip(
            "Заменить текущий список на профиль пользователя и все доступные "
            "диски, кроме C:. После этого набор будет автоматически пересчитываться "
            "при запуске, пока пользователь снова не изменит список вручную."
        )
        self.default_paths_btn.clicked.connect(self._set_default_search_paths)
        paths_header_layout.addWidget(self.default_paths_btn)
        paths_header_layout.addWidget(self._section_label("Пути для поиска"))
        paths_header_layout.addStretch(1)
        layout.addWidget(paths_header)
        self.paths_list = QListWidget()
        self.paths_list.setObjectName("pathsList")
        self.paths_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.paths_list.setViewMode(QListView.IconMode)
        self.paths_list.setFlow(QListView.LeftToRight)
        self.paths_list.setWrapping(True)
        self.paths_list.setResizeMode(QListView.Adjust)
        self.paths_list.setMovement(QListView.Static)
        self.paths_list.setSpacing(6)
        self.paths_list.setWordWrap(False)
        self.paths_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.paths_list.setFixedHeight(86)
        self.paths_list.setToolTip(
            "Пути отображаются компактными кнопками. Для удаления выделите "
            "один или несколько путей и нажмите «Удалить»."
        )
        layout.addWidget(self.paths_list)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 3, 0, 3)
        btn_row.setSpacing(8)
        add_folder_btn = QPushButton("+ Папка")
        add_file_btn = QPushButton("+ Файл")
        remove_btn = QPushButton("− Удалить")
        add_folder_btn.clicked.connect(self._add_folder)
        add_file_btn.clicked.connect(self._add_file)
        remove_btn.clicked.connect(self._remove_selected_paths)
        for b in (add_folder_btn, add_file_btn, remove_btn):
            b.setObjectName("toolButton")  # компактный стиль (см. QSS)
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

        layout.addWidget(self._divider())

        # ---- Секция: ключевые слова ----------------------------------- #
        terms_help = (
            "Каждое слово или словосочетание указывайте с новой строки, "
            "через запятую или точку с запятой. Обычный пробел НЕ разделяет термины, "
            "поэтому фраза «служебная записка» ищется целиком.\n\n"
            "Пример:\nпилот\nслужебная записка, охран*; договор\n\n"
            "Символ * означает продолжение до 30 букв/цифр/дефисов: "
            "«охран*» найдёт «охрана» и «охранник». Без включённого режима "
            "«Целые слова» обычная часть слова тоже находится: «пилот» найдёт «беспилотник»."
        )
        layout.addWidget(
            self._section_header(
                "Ключевые слова / фразы внутри файлов",
                "Как вводить ключевые слова",
                terms_help,
            )
        )
        self.words_edit = QTextEdit()
        self.words_edit.setToolTip("Термины для поиска в содержимом документов")
        self.words_edit.setPlaceholderText(
            "содержимое: по одному в строке, через запятую или ;\nнапример: пилот, служебная записка"
        )
        self.words_edit.setMinimumHeight(66)
        layout.addWidget(self.words_edit)

        layout.addWidget(
            self._section_header(
                "Ключевые слова / фразы в названиях файлов",
                "Как вводить ключевые слова",
                terms_help,
            )
        )
        self.filename_words_edit = QTextEdit()
        self.filename_words_edit.setToolTip(
            "Термины для поиска только в названиях файлов"
        )
        self.filename_words_edit.setPlaceholderText(
            "названия файлов: по одному в строке, через запятую или ;\nпусто — поиск по названиям не выполняется"
        )
        self.filename_words_edit.setMinimumHeight(66)
        layout.addWidget(self.filename_words_edit)

        layout.addWidget(self._divider())

        # ---- Секция: параметры ---------------------------------------- #
        layout.addWidget(self._section_label("Параметры"))

        # Типы файлов — одна строка чекбоксов
        types_row = QHBoxLayout()
        types_row.setSpacing(14)
        types_row.addWidget(QLabel("Типы файлов:"))
        self.type_checks = {}
        for ext in SUPPORTED_EXTENSIONS:
            cb = QCheckBox(ext.lstrip("."))
            cb.setChecked(True)
            self.type_checks[ext] = cb
            types_row.addWidget(cb)
        types_row.addStretch(1)
        layout.addLayout(types_row)

        # Метод .doc — подпись и комбобокс в одну строку
        method_row = QHBoxLayout()
        method_row.setSpacing(10)
        method_row.addWidget(QLabel("Метод .doc:"))
        self.doc_method_combo = QComboBox()
        self.doc_method_combo.setToolTip(
            "Метод чтения legacy .doc: AUTO — автоопределение, "
            "ЭВРИСТИКА — OLE2 без внешних программ, ВНЕШНИЙ — через LibreOffice"
        )
        for method in DocReadMethod:
            self.doc_method_combo.addItem(method.label, method)
        method_row.addWidget(self.doc_method_combo, 1)
        layout.addLayout(method_row)

        self.heuristic_external_check = QCheckBox(
            "Использовать доступные внешние программы"
        )
        self.heuristic_external_check.setChecked(True)
        self.heuristic_external_check.setToolTip(
            "После завершения быстрой эвристики повторно открыть только те .doc, "
            "которые не удалось распознать, через LibreOffice или MS Word."
        )
        layout.addWidget(self.heuristic_external_check)
        self.doc_method_combo.currentIndexChanged.connect(
            self._update_heuristic_external_visibility
        )
        self._update_heuristic_external_visibility()

        # Три основных переключателя поиска — строго в одну строку.
        toggles = QHBoxLayout()
        toggles.setSpacing(18)
        self.case_sensitive_check = QCheckBox("Учитывать регистр")
        self.case_sensitive_check.setToolTip("Поиск с учётом регистра букв")
        self.whole_word_check = QCheckBox("Целые слова")
        self.whole_word_check.setToolTip(
            "Выключено: термин может быть частью слова («пилот» найдёт «беспилотник»).\n"
            "Включено: совпадают только отдельные целые слова."
        )
        self.ocr_check = QCheckBox("OCR для PDF")
        self.ocr_check.setChecked(True)  # Включён по умолчанию
        self.ocr_check.setToolTip(
            "Распознавание сканированных PDF без текстового слоя (Tesseract)"
        )
        toggles.addWidget(self.case_sensitive_check)
        toggles.addWidget(self.whole_word_check)
        toggles.addWidget(self.ocr_check)
        toggles.addStretch(1)
        layout.addLayout(toggles)

        self.precount_check = QCheckBox("Сначала подсчитать все файлы")
        self.precount_check.setChecked(True)
        self.precount_check.setToolTip(
            "Включено: перед чтением выполняется быстрый обход каталогов, и\n"
            "прогресс сразу знает общее число файлов — полоса заполняется\n"
            "равномерно от 0 до 100%.\n"
            "Выключено: обработка стартует немедленно, но общее число\n"
            "уточняется по ходу, поэтому заполнение полосы скачет назад.\n"
            "Отключайте на очень медленных сетевых дисках, где сам обход\n"
            "каталогов занимает заметное время."
        )
        layout.addWidget(self.precount_check)

        pdf_pages_row = QHBoxLayout()
        pdf_pages_row.setSpacing(8)
        self.limit_pdf_pages_check = QCheckBox("Искать только на первых")
        self.limit_pdf_pages_check.setChecked(False)
        self.limit_pdf_pages_check.setToolTip(
            "Ограничить чтение и OCR PDF первыми N страницами. "
            "Настройка действует только при включённом OCR."
        )
        self.pdf_page_limit_spin = QSpinBox()
        self.pdf_page_limit_spin.setRange(1, 100_000)
        self.pdf_page_limit_spin.setValue(PDF_PAGE_LIMIT_DEFAULT)
        self.pdf_page_limit_spin.setFixedWidth(92)
        self.pdf_page_limit_spin.setToolTip(
            "Максимальное число первых страниц каждого PDF для чтения и OCR"
        )
        pdf_pages_row.addWidget(self.limit_pdf_pages_check)
        pdf_pages_row.addWidget(self.pdf_page_limit_spin)
        pdf_pages_row.addWidget(QLabel("страницах PDF"))
        pdf_pages_row.addStretch(1)
        layout.addLayout(pdf_pages_row)
        self.ocr_check.toggled.connect(self._update_pdf_page_limit_controls)
        self.limit_pdf_pages_check.toggled.connect(
            self._update_pdf_page_limit_controls
        )
        self._update_pdf_page_limit_controls()

        # Числовые параметры — компактно в одну строку
        numbers = QHBoxLayout()
        numbers.setSpacing(8)
        size_label = QLabel("Размер, МБ:")
        size_label.setToolTip("Максимальный размер обрабатываемого файла")
        numbers.addWidget(size_label)
        numbers.addWidget(
            self._help_button(
                "Размер, МБ",
                "Максимальный размер одного файла, который программа будет открывать и анализировать. "
                "Более крупные файлы пропускаются, чтобы избежать чрезмерного расхода памяти и долгого зависания. "
                "Значение 25 МБ используется по умолчанию и подходит для большинства задач.",
            )
        )
        self.max_size_spin = QDoubleSpinBox()
        self.max_size_spin.setRange(1, 5000)
        self.max_size_spin.setValue(MAX_FILE_SIZE_MB_DEFAULT)
        self.max_size_spin.setDecimals(0)
        self.max_size_spin.setFixedWidth(84)
        self.max_size_spin.setToolTip("Максимальный размер обрабатываемого файла, МБ")
        numbers.addWidget(self.max_size_spin)
        numbers.addSpacing(12)
        ctx_label = QLabel("Контекст:")
        ctx_label.setToolTip(
            "Сколько символов вокруг совпадения показывать в результатах"
        )
        numbers.addWidget(ctx_label)
        numbers.addWidget(
            self._help_button(
                "Контекст",
                "Количество символов до и после найденного совпадения, показываемых в таблице «Результаты». "
                "Во всплывающей подсказке это же число используется как количество слов с каждой стороны, "
                "но не менее 30 слов слева и 30 справа. В окне всех совпадений текст ещё на 50% шире. "
                "Параметр не влияет на сам факт поиска.",
            )
        )
        self.context_spin = QSpinBox()
        self.context_spin.setRange(20, 1000)
        self.context_spin.setValue(CONTEXT_CHARS_DEFAULT)
        self.context_spin.setFixedWidth(84)
        self.context_spin.setToolTip(
            "Сколько символов вокруг совпадения показывать в результатах"
        )
        numbers.addWidget(self.context_spin)
        numbers.addStretch(1)
        layout.addLayout(numbers)

        layout.addWidget(self._divider())

        # ---- Секция: статистика --------------------------------------- #
        layout.addWidget(self._section_label("Статистика последнего сканирования"))
        stats_grid = QGridLayout()
        stats_grid.setSpacing(8)
        self.stat_cards = {}
        stat_names = [
            ("processed", "Обработано"),
            ("found", "Совпадений"),
            ("docx_count", "DOCX"),
            ("doc_count", "DOC"),
            ("txt_count", "TXT"),
            ("pdf_count", "PDF"),
            ("errors", "Ошибок"),
            ("locked", "Заблокир."),
            ("scan_time", "Время, с"),
        ]
        for i, (key, label) in enumerate(stat_names):
            card = StatCard(label)
            self.stat_cards[key] = card
            stats_grid.addWidget(card, i // 4, i % 4)
        layout.addLayout(stats_grid)

        layout.addStretch(1)

        # Прокрутка: при нехватке высоты окна появляется скроллбар,
        # содержимое НИКОГДА не сжимается ниже минимума и не накладывается.
        scroll = QScrollArea()
        scroll.setObjectName("leftScroll")
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Закреплённая панель действий ПОД прокруткой: главная кнопка
        # «Начать поиск» видна всегда, независимо от высоты окна.
        outer = QWidget()
        outer.setObjectName("leftPanelOuter")
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(6)
        outer_layout.addWidget(scroll, 1)
        outer_layout.addWidget(self._divider())

        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        actions_row.setContentsMargins(4, 4, 10, 6)
        self.start_btn = QPushButton("▶  Начать поиск")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.clicked.connect(self._start_scan)
        self.start_btn.setMinimumHeight(40)
        self.start_btn.setShortcut("Ctrl+Return")
        self.test_file_btn = QPushButton("🔍 Проверить 1 файл…")
        self.test_file_btn.setToolTip(
            "Быстрая проверка извлечения текста из одного конкретного документа"
        )
        self.test_file_btn.clicked.connect(self._test_single_file)
        self.test_file_btn.setMinimumHeight(40)
        actions_row.addWidget(self.start_btn, 5)
        actions_row.addWidget(self.test_file_btn, 4)
        outer_layout.addLayout(actions_row)

        outer.setMinimumWidth(400)
        return outer

    # -- Правая панель (вкладки) ------------------------------------------ #
    def _build_right_panel(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_results_tab(), "Результаты")
        self.tabs.addTab(self._build_files_tab(), "Файлы")
        self.tabs.addTab(self._build_log_tab(), "Журнал")
        layout.addWidget(self.tabs)
        return widget

    # -- Вкладка: Результаты ----------------------------------------------- #
    def _build_results_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(10, 10, 10, 12)
        layout.setSpacing(10)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 2)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Фильтр по любому столбцу…")
        self.filter_edit.setToolTip("Фильтрация по всем колонкам результатов в реальном времени")
        self.filter_edit.textChanged.connect(self.proxy_model.setFilterFixedString)
        filter_row.addWidget(self.filter_edit)
        layout.addLayout(filter_row)

        self.results_table = QTableView()
        self.results_table.setModel(self.proxy_model)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setSortingEnabled(True)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setShowGrid(True)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.verticalHeader().setDefaultSectionSize(24)
        self.results_table.setWordWrap(False)

        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        # Ширина «Контекста» подгоняется под реальные строки, а не берётся
        # фиксированной: длина контекста задаётся настройкой и обычно намного
        # меньше, чем прежние 900 px, из-за чего справа оставалась пустота.
        # Режим Interactive сохранён намеренно: ResizeToContents измеряет
        # каждую строку модели при любой перекомпоновке, а строк бывают сотни
        # тысяч.
        self.results_table.setColumnWidth(3, _CONTEXT_COLUMN_MIN_WIDTH)
        header.sectionResized.connect(self._on_results_section_resized)

        self.results_table.setItemDelegateForColumn(
            0, HighlightDelegate(filename_column=True)
        )
        self.results_table.setItemDelegateForColumn(3, HighlightDelegate())

        self.results_table.clicked.connect(self._on_result_table_clicked)
        self.results_table.doubleClicked.connect(self._open_selected_result)
        self.results_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results_table.customContextMenuRequested.connect(
            self._show_results_context_menu
        )
        layout.addWidget(self.results_table, 1)

        export_row = QHBoxLayout()
        export_row.setContentsMargins(0, 6, 0, 4)
        export_row.setSpacing(8)
        export_excel_btn = QPushButton("Экспорт в Excel")
        export_excel_btn.setObjectName("exportExcelButton")
        export_excel_btn.setToolTip("Сохранить результаты в .xlsx")
        export_csv_btn = QPushButton("Экспорт в CSV")
        export_csv_btn.setObjectName("exportCsvButton")
        export_csv_btn.setToolTip("Сохранить результаты в .csv")
        export_md_btn = QPushButton("Экспорт в Markdown")
        export_md_btn.setObjectName("exportMarkdownButton")
        export_md_btn.setToolTip("Сохранить результаты в .md")
        open_folder_btn = QPushButton("Открыть папку файла")
        open_folder_btn.setObjectName("openResultFolderButton")
        open_folder_btn.setToolTip("Открыть папку выбранного файла в проводнике")
        export_excel_btn.clicked.connect(self._export_excel)
        export_csv_btn.clicked.connect(self._export_csv)
        export_md_btn.clicked.connect(self._export_markdown)
        open_folder_btn.clicked.connect(self._open_selected_folder)
        for b in (export_excel_btn, export_csv_btn, export_md_btn, open_folder_btn):
            b.setMinimumHeight(36)
            export_row.addWidget(b)
        export_row.addStretch(1)
        layout.addLayout(export_row)
        return widget

    # -- Вкладка: Файлы --------------------------------------------------- #
    def _build_files_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(10, 10, 10, 12)
        layout.setSpacing(10)

        info_label = QLabel(
            "Найденные файлы появляются здесь сразу во время сканирования. "
            "Отображаются уникальные полные пути из столбца «Путь» вкладки «Результаты». "
            "Наведите курсор на файл, чтобы увидеть расширенные контексты совпадений. "
            "Все вхождения доступны по нажатию на число во вкладке «Результаты». "
            "Отметьте нужные файлы чекбоксами слева."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #9AA3B2; padding: 4px;")
        layout.addWidget(info_label)

        select_row = QHBoxLayout()
        select_row.setContentsMargins(0, 3, 0, 3)
        select_row.setSpacing(8)
        select_all_btn = QPushButton("Отметить все")
        select_all_btn.setToolTip("Выбрать все файлы в списке")
        clear_all_btn = QPushButton("Снять все")
        clear_all_btn.setToolTip("Снять выделение со всех файлов")
        select_all_btn.clicked.connect(lambda: self._set_all_found_checked(True))
        clear_all_btn.clicked.connect(lambda: self._set_all_found_checked(False))
        select_row.addWidget(select_all_btn)
        select_row.addWidget(clear_all_btn)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        self.found_files_list = QListWidget()
        self.found_files_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.found_files_list.setAlternatingRowColors(True)
        self.found_files_list.itemDoubleClicked.connect(
            lambda item: self._open_path(item.data(Qt.UserRole) or item.text())
        )
        layout.addWidget(self.found_files_list, 1)

        copy_box = QGroupBox("Копирование отмеченных файлов")
        copy_layout = QHBoxLayout(copy_box)
        copy_layout.setContentsMargins(16, 32, 16, 16)
        copy_layout.setSpacing(8)
        self.copy_dest_edit = QLineEdit(str(DEFAULT_COPY_DESTINATION))
        browse_btn = QPushButton("Обзор…")
        browse_btn.clicked.connect(self._browse_copy_dest)
        self.copy_btn = QPushButton("Скопировать отмеченные")
        self.copy_btn.setObjectName("primaryButton")
        self.copy_btn.clicked.connect(self._copy_found_files)
        copy_layout.addWidget(QLabel("Папка:"))
        copy_layout.addWidget(self.copy_dest_edit, 1)
        copy_layout.addWidget(browse_btn)
        copy_layout.addWidget(self.copy_btn)
        layout.addWidget(copy_box)

        secure_row = QHBoxLayout()
        secure_row.setContentsMargins(0, 4, 0, 4)
        secure_row.setSpacing(8)
        self.secure_delete_btn = QPushButton("Безопасно удалить отмеченные")
        self.secure_delete_btn.setObjectName("dangerButton")
        self.secure_delete_btn.clicked.connect(self._secure_delete_selected)
        self.secure_move_btn = QPushButton("Безопасно переместить отмеченные…")
        self.secure_move_btn.clicked.connect(self._secure_move_selected)
        secure_row.addWidget(self.secure_delete_btn)
        secure_row.addWidget(self.secure_move_btn)
        secure_row.addStretch(1)
        layout.addLayout(secure_row)
        return widget

    # -- Вкладка: Журнал --------------------------------------------------- #
    def _build_log_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(10, 10, 10, 12)
        layout.setSpacing(10)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        layout.addWidget(self.log_view, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 4, 0, 4)
        btn_row.setSpacing(8)
        clear_btn = QPushButton("Очистить журнал")
        clear_btn.clicked.connect(self.log_view.clear)
        btn_row.addWidget(clear_btn)
        self.unreadable_files_btn = QPushButton("Файлы с ошибками чтения…")
        self.unreadable_files_btn.setToolTip(
            "Показать файлы, которые не удалось прочитать ни одним доступным способом"
        )
        self.unreadable_files_btn.clicked.connect(self._show_unreadable_files)
        btn_row.addWidget(self.unreadable_files_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)
        return widget

    # ------------------------------------------------------------------ #
    # Логирование
    # ------------------------------------------------------------------ #
    def _connect_log_handler(self) -> None:
        self.qt_log_handler.new_record.connect(self._append_log)

    def _append_log(self, level: str, message: str) -> None:
        color = {"ERROR": "#E5484D", "CRITICAL": "#E5484D", "WARNING": "#F2B84B"}.get(
            level, "#9AA3B2"
        )
        self.log_view.appendHtml(f'<span style="color:{color}">{escape(message)}</span>')

    # ------------------------------------------------------------------ #
    # Файлы, которые не удалось прочитать
    # ------------------------------------------------------------------ #
    def _update_unreadable_files_button(self) -> None:
        count = len(self._unreadable_entries)
        self.unreadable_files_btn.setText(
            f"Файлы с ошибками чтения ({count})…"
            if count
            else "Файлы с ошибками чтения…"
        )
        self.unreadable_files_btn.setVisible(count > 0)

    def _show_unreadable_files(self) -> None:
        if not self._unreadable_entries:
            self._update_unreadable_files_button()
            QMessageBox.information(
                self,
                "Ошибки чтения",
                "В последнем сканировании нет файлов с окончательной ошибкой чтения.",
            )
            return
        if any(
            worker is not None and worker.isRunning()
            for worker in (
                self.copy_worker,
                self.secure_worker,
                self.error_copy_worker,
                self.error_secure_worker,
            )
        ):
            QMessageBox.information(
                self,
                "Файловая операция",
                "Операция с ошибочными файлами уже выполняется.",
            )
            return

        dialog = UnreadableFilesDialog(self._unreadable_entries, self)
        if dialog.exec() != QDialog.Accepted or not dialog.selected_action:
            return
        paths = dialog.paths
        if not paths:
            QMessageBox.warning(
                self,
                "Файлы недоступны",
                "Нет существующих обычных файлов для операции.",
            )
            return

        self.unreadable_files_btn.setEnabled(False)
        if dialog.selected_action == UnreadableFilesDialog.ACTION_COPY:
            destination = dialog.destination
            self.error_copy_worker = CopyWorker(paths, destination)
            self.error_copy_worker.finished_ok.connect(
                lambda mappings, expected=len(paths), target=destination: (
                    self._on_unreadable_copy_finished(mappings, expected, target)
                )
            )
            self.error_copy_worker.failed.connect(self._on_unreadable_operation_failed)
            self.status_bar.showMessage("Копирование файлов с ошибками чтения…")
            self.error_copy_worker.start()
            return

        mode = (
            "move"
            if dialog.selected_action == UnreadableFilesDialog.ACTION_MOVE
            else "delete"
        )
        self.error_secure_worker = SecureOpWorker(
            mode,
            paths,
            dialog.destination,
            passes=load_settings().secure_passes,
        )
        self.error_secure_worker.finished_ok.connect(
            lambda result, operation=mode: self._on_unreadable_secure_finished(
                operation, result
            )
        )
        self.error_secure_worker.failed.connect(self._on_unreadable_operation_failed)
        self.status_bar.showMessage(
            "Безопасное перемещение файлов с ошибками чтения…"
            if mode == "move"
            else "Безопасное удаление файлов с ошибками чтения…"
        )
        self.error_secure_worker.start()

    def _on_unreadable_copy_finished(
        self, mappings: List[CopyMapping], expected: int, destination: str
    ) -> None:
        self.unreadable_files_btn.setEnabled(True)
        copied = len(mappings)
        self.status_bar.showMessage(
            f"Скопировано ошибочных файлов: {copied} из {expected}"
        )
        if copied < expected:
            QMessageBox.warning(
                self,
                "Копирование завершено частично",
                f"Скопировано файлов: {copied} из {expected}.\n\n"
                f"Папка назначения:\n{destination}",
            )
        else:
            QMessageBox.information(
                self,
                "Копирование завершено",
                f"Скопировано файлов: {copied}.\n\nПапка назначения:\n{destination}",
            )
        if copied:
            self._open_path(destination)
        self._maybe_close_after_workers()

    def _sync_unreadable_entries_after_operation(self) -> None:
        remaining = []
        for entry in self._unreadable_entries:
            try:
                if Path(entry.file_path).exists():
                    remaining.append(entry)
            except OSError:
                # Ошибка доступа не означает, что файл исчез: оставляем его в списке.
                remaining.append(entry)
        self._unreadable_entries = remaining
        if self.last_report is not None:
            self.last_report.unreadable_files = list(remaining)
        self._update_unreadable_files_button()

    def _on_unreadable_secure_finished(
        self, operation: str, result: OperationResult
    ) -> None:
        self.unreadable_files_btn.setEnabled(True)
        self._refresh_found_files_after_operation()
        self._sync_unreadable_entries_after_operation()
        action_text = "перемещено" if operation == "move" else "удалено"
        message = f"Успешно {action_text} файлов: {result.succeeded}"
        if result.failures:
            preview = "\n".join(result.failures[:8])
            extra = (
                f"\n…ещё ошибок: {len(result.failures) - 8}"
                if len(result.failures) > 8
                else ""
            )
            QMessageBox.warning(
                self,
                "Операция завершена частично",
                f"{message}\nНе обработано: {len(result.failures)}\n\n{preview}{extra}",
            )
        else:
            QMessageBox.information(self, "Готово", message)
        self.status_bar.showMessage(message)
        self._maybe_close_after_workers()

    def _on_unreadable_operation_failed(self, message: str) -> None:
        self.unreadable_files_btn.setEnabled(True)
        QMessageBox.critical(self, "Ошибка файловой операции", message)
        self._maybe_close_after_workers()

    # ------------------------------------------------------------------ #
    # Быстрая проверка одного документа (диагностика)
    # ------------------------------------------------------------------ #
    def _collect_settings_silent(self) -> Optional[ScanSettings]:
        """Собирает настройки без вывода всплывающих окон об ошибках."""
        paths = [self.paths_list.item(i).text() for i in range(self.paths_list.count())]
        words = parse_terms(self.words_edit.toPlainText())
        filename_words = parse_terms(self.filename_words_edit.toPlainText())
        file_types = {ext for ext, cb in self.type_checks.items() if cb.isChecked()}
        return ScanSettings(
            paths=paths,
            words=words,
            filename_words=filename_words,
            file_types=file_types,
            doc_read_method=self._doc_method_from_data(
                self.doc_method_combo.currentData()
            ),
            case_sensitive=self.case_sensitive_check.isChecked(),
            whole_word=self.whole_word_check.isChecked(),
            context_chars=self.context_spin.value(),
            max_file_size_mb=self.max_size_spin.value(),
            use_ocr_for_pdf=self.ocr_check.isChecked(),
            limit_pdf_pages=(
                self.ocr_check.isChecked()
                and self.limit_pdf_pages_check.isChecked()
            ),
            pdf_page_limit=self.pdf_page_limit_spin.value(),
            use_external_programs_after_heuristic_failure=(
                self.heuristic_external_check.isChecked()
            ),
            precount_files=self.precount_check.isChecked(),
        )

    def _test_single_file(self) -> None:
        if self.single_file_worker is not None and self.single_file_worker.isRunning():
            QMessageBox.information(
                self,
                "Проверка выполняется",
                "Дождитесь окончания текущей проверки файла.",
            )
            return

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл для проверки чтения и OCR",
            filter="Документы (*.docx *.doc *.txt *.pdf);;Все файлы (*.*)",
        )
        if not path:
            return

        file_path = Path(path)
        settings = self._collect_settings_silent() or ScanSettings()
        settings.doc_read_method = self._doc_method_from_data(
            self.doc_method_combo.currentData()
        )
        settings.use_ocr_for_pdf = self.ocr_check.isChecked()
        settings.limit_pdf_pages = (
            settings.use_ocr_for_pdf and self.limit_pdf_pages_check.isChecked()
        )
        settings.pdf_page_limit = self.pdf_page_limit_spin.value()

        # Чтение и OCR уходят в отдельный поток: на многостраничном скане
        # операция длится минуты, а общего таймаута у неё нет.
        self.test_file_btn.setEnabled(False)
        self.status_bar.showMessage(f"Проверка чтения: {file_path.name}…")
        self.single_file_worker = SingleFileTestWorker(file_path, settings)
        self.single_file_worker.finished_ok.connect(
            lambda outcome, elapsed, target=file_path: self._on_single_file_tested(
                target, outcome, elapsed
            )
        )
        self.single_file_worker.failed.connect(self._on_single_file_test_failed)
        self.single_file_worker.start()

    def _on_single_file_test_failed(self, message: str) -> None:
        self.test_file_btn.setEnabled(True)
        self.status_bar.showMessage("Проверка файла завершилась ошибкой")
        self.logger.error("Диагностика чтения файла не выполнена: %s", message)
        QMessageBox.critical(self, "Ошибка проверки файла", message)
        self._maybe_close_after_workers()

    def _on_single_file_tested(self, file_path: Path, outcome, elapsed: float) -> None:
        self.test_file_btn.setEnabled(True)
        self.status_bar.showMessage(f"Проверка завершена: {file_path.name}")
        if self._closing_after_scan:
            self._maybe_close_after_workers()
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Диагностика чтения: {file_path.name}")
        dlg.resize(750, 550)
        dlg_layout = QVBoxLayout(dlg)

        status_text = (
            f"<b>Полный путь:</b> {file_path}<br>"
            f"<b>Использованный ридер / метод:</b> {outcome.method_used or 'не определён'}<br>"
            f"<b>Время извлечения:</b> {elapsed:.2f} с<br>"
        )

        if outcome.locked:
            status_text += "<span style='color:#E5484D'><b>Статус:</b> ФАЙЛ ЗАБЛОКИРОВАН другим процессом!</span>"
        elif outcome.error:
            status_text += f"<span style='color:#E5484D'><b>Критическая ошибка:</b> {outcome.error}</span>"
        elif outcome.warning:
            status_text += f"<span style='color:#F2B84B'><b>Предупреждение / OCR:</b> {outcome.warning}</span><br><b>Статус:</b> Прочитан с предупреждениями"
        else:
            status_text += (
                "<span style='color:#3DD68C'><b>Статус:</b> Успешно прочитан</span>"
            )

        info_label = QLabel(status_text)
        info_label.setStyleSheet(
            "padding: 6px; background-color: #1D2129; border-radius: 8px;"
        )
        info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dlg_layout.addWidget(info_label)

        dlg_layout.addWidget(QLabel("<b>Извлечённый текст (первые 4000 символов):</b>"))
        text_preview = QPlainTextEdit()
        text_preview.setReadOnly(True)
        text_preview.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        text_preview.setPlainText(
            outcome.text[:4000] if outcome.text else "<Текст отсутствует или пуст>"
        )
        dlg_layout.addWidget(text_preview, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok)
        btns.accepted.connect(dlg.accept)
        dlg_layout.addWidget(btns)

        dlg.exec()

    # ------------------------------------------------------------------ #
    # Управление путями
    # ------------------------------------------------------------------ #
    @staticmethod
    def _path_item_key(value: str) -> str:
        return value.replace("/", "\\").rstrip("\\").casefold()

    def _add_path_chip(self, raw_path: str) -> bool:
        """Добавляет путь как компактную горизонтальную «кнопку»-чип.

        Полный путь остаётся текстом элемента и сохраняется в JSON без
        преобразований; визуальная ширина ограничивается, а полный текст
        доступен во всплывающей подсказке.
        """
        value = str(raw_path).strip()
        if not value:
            return False
        key = self._path_item_key(value)
        for index in range(self.paths_list.count()):
            if self._path_item_key(self.paths_list.item(index).text()) == key:
                return False

        item = QListWidgetItem(value)
        item.setToolTip(value)
        width = min(390, max(120, self.paths_list.fontMetrics().horizontalAdvance(value) + 30))
        item.setSizeHint(QSize(width, 34))
        self.paths_list.addItem(item)
        return True

    def _replace_search_paths(self, paths: list[str], *, mode: str) -> None:
        """Атомарно заменяет список путей без ложной пометки как пользовательский."""
        self._updating_paths_programmatically = True
        try:
            self.paths_list.clear()
            seen: set[str] = set()
            for raw_path in paths:
                value = str(raw_path).strip()
                if not value:
                    continue
                key = self._path_item_key(value)
                if key in seen:
                    continue
                seen.add(key)
                self._add_path_chip(value)
            self._paths_mode = "default" if mode == "default" else "custom"
        finally:
            self._updating_paths_programmatically = False

    def _set_default_search_paths(self, *_args, notify: bool = True) -> None:
        """Устанавливает профиль пользователя и все диски, кроме C:."""
        paths = default_search_paths()
        self._replace_search_paths(paths, mode="default")
        if not self._restoring_scan_config:
            self._schedule_auto_save()
        if notify and hasattr(self, "status_bar"):
            self.status_bar.showMessage(
                f"Пути по умолчанию установлены: {len(paths)}",
                5000,
            )

    def _on_paths_model_changed(self, *_args) -> None:
        """Фиксирует любое ручное изменение как пользовательскую конфигурацию."""
        if self._restoring_scan_config or self._updating_paths_programmatically:
            return
        self._paths_mode = "custom"
        self._schedule_auto_save()

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку для поиска")
        if folder:
            self._add_path_chip(folder)

    def _add_file(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите файлы",
            filter="Документы (*.docx *.doc *.txt *.pdf);;Все файлы (*.*)",
        )
        for f in files:
            self._add_path_chip(f)

    def _remove_selected_paths(self) -> None:
        for item in self.paths_list.selectedItems():
            self.paths_list.takeItem(self.paths_list.row(item))

    # ------------------------------------------------------------------ #
    # Сканирование
    # ------------------------------------------------------------------ #
    def _collect_settings(self) -> Optional[ScanSettings]:
        paths = [self.paths_list.item(i).text() for i in range(self.paths_list.count())]
        words = parse_terms(self.words_edit.toPlainText())
        filename_words = parse_terms(self.filename_words_edit.toPlainText())

        file_types = {ext for ext, cb in self.type_checks.items() if cb.isChecked()}

        settings = ScanSettings(
            paths=paths,
            words=words,
            filename_words=filename_words,
            file_types=file_types,
            doc_read_method=self._doc_method_from_data(
                self.doc_method_combo.currentData()
            ),
            case_sensitive=self.case_sensitive_check.isChecked(),
            whole_word=self.whole_word_check.isChecked(),
            context_chars=self.context_spin.value(),
            max_file_size_mb=self.max_size_spin.value(),
            max_workers=0,  # "авто" — берётся из AppSettings в _start_scan
            use_ocr_for_pdf=self.ocr_check.isChecked(),
            limit_pdf_pages=(
                self.ocr_check.isChecked()
                and self.limit_pdf_pages_check.isChecked()
            ),
            pdf_page_limit=self.pdf_page_limit_spin.value(),
            use_external_programs_after_heuristic_failure=(
                self.heuristic_external_check.isChecked()
            ),
            precount_files=self.precount_check.isChecked(),
        )
        errors = settings.validate()
        if errors:
            QMessageBox.warning(self, "Проверка настроек", "\n".join(errors))
            return None
        return settings

    @staticmethod
    def _doc_method_from_data(data) -> DocReadMethod:
        """Восстанавливает DocReadMethod из данных QComboBox.

        PySide6 сериализует str-подкласс enum в обычную строку, поэтому
        currentData()/itemData() могут вернуть 'str' вместо enum. Приводим
        к DocReadMethod по строковому значению; неизвестное/пустое -> AUTO.
        """
        if isinstance(data, DocReadMethod):
            return data
        if isinstance(data, str):
            try:
                return DocReadMethod(data)
            except ValueError:
                return DocReadMethod.AUTO
        return DocReadMethod.AUTO

    def _update_heuristic_external_visibility(self, *_args) -> None:
        """Показывает внешний fallback только для режима быстрой эвристики."""
        is_heuristic = (
            self._doc_method_from_data(self.doc_method_combo.currentData())
            == DocReadMethod.HEURISTIC
        )
        self.heuristic_external_check.setVisible(is_heuristic)

    def _update_pdf_page_limit_controls(self, *_args) -> None:
        """Активирует ограничение страниц только при включённом OCR."""
        ocr_enabled = self.ocr_check.isChecked()
        self.limit_pdf_pages_check.setEnabled(ocr_enabled)
        self.pdf_page_limit_spin.setEnabled(
            ocr_enabled and self.limit_pdf_pages_check.isChecked()
        )

    def _reset_statistics(self) -> None:
        """Обнуляет карточки статистики и очищает результаты."""
        for card in self.stat_cards.values():
            card.set_value(0)
        self.results_model.clear()
        self.found_files_list.clear()
        self._found_file_paths.clear()
        self._update_files_tab_title()
        self._unreadable_entries = []
        self._update_unreadable_files_button()
        self.processing_list.clear()
        self.progress_bar.setValue(0)
        self.progress_label.setText("Обработано: 0 / 0")
        self.status_bar.clearMessage()
        self.last_report = None

    def _start_scan(self) -> None:
        settings = self._collect_settings()
        if settings is None:
            return

        # Применяем max_workers из AppSettings
        app_s = load_settings()
        if settings.max_workers <= 0:
            settings.max_workers = app_s.max_workers
        settings.max_matches_per_word = app_s.max_matches_per_word

        self._save_settings()
        self._active_search_info = build_search_info(settings)
        self.results_model.clear()
        self.found_files_list.clear()
        self._found_file_paths.clear()
        self._update_files_tab_title()
        self._unreadable_entries = []
        self._update_unreadable_files_button()
        self.processing_list.clear()

        self.progress_bar.setMaximum(0)  # "бесконечный" режим до первого обновления
        self.progress_bar.setValue(0)
        self.progress_label.setText("Обработано: 0 / 0")

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.stop_btn.setProperty("active", True)
        self.stop_btn.style().polish(self.stop_btn)

        # Автопереключение на "Результаты" (рекомендация отчёта)
        self.tabs.setCurrentIndex(0)

        # Подробная запись в лог (рекомендация отчёта)
        log_msg = (
            f"Старт сканирования | путей={len(settings.paths)} | "
            f"терминов_внутри={len(settings.words)} | терминов_в_именах={len(settings.filename_words)} | "
            f"типы={sorted(settings.file_types)} | метод_doc={settings.doc_read_method.value} | "
            f"внешний_повтор_после_эвристики="
            f"{settings.use_external_programs_after_heuristic_failure} | "
            f"OCR={settings.use_ocr_for_pdf} | "
            f"лимит_PDF_страниц="
            f"{settings.pdf_page_limit if settings.limit_pdf_pages else 'все'} | "
            f"потоков={settings.max_workers} | лимит_совпадений={settings.max_matches_per_word} | "
            f"предподсчёт={settings.precount_files}"
        )
        self.logger.info(log_msg)

        # Гарантированно отключаем предыдущие соединения (если старый
        # ScanWorker ещё не доставил финальные сигналы в очереди Qt)
        try:
            self.scan_worker.progress.disconnect()
        except (TypeError, RuntimeError, AttributeError):
            pass
        try:
            self.scan_worker.results_found.disconnect()
        except (TypeError, RuntimeError, AttributeError):
            pass
        try:
            self.scan_worker.log_message.disconnect()
        except (TypeError, RuntimeError, AttributeError):
            pass
        try:
            self.scan_worker.finished_ok.disconnect()
        except (TypeError, RuntimeError, AttributeError):
            pass
        try:
            self.scan_worker.failed.disconnect()
        except (TypeError, RuntimeError, AttributeError):
            pass
        try:
            self.scan_worker.stats_update.disconnect()
        except (TypeError, RuntimeError, AttributeError):
            pass

        self.scan_worker = ScanWorker(settings)
        self.scan_worker.progress.connect(self._on_progress)
        self.scan_worker.results_found.connect(self._on_results_found)
        # Используем именованную ссылку на slot, чтобы можно было отключиться
        self.scan_worker.log_message.connect(self._on_log_message)
        self.scan_worker.finished_ok.connect(self._on_scan_finished)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.stats_update.connect(self._on_stats_update)
        self.scan_worker.start()

        # Запускаем таймер для отображения длительности сканирования
        self._scan_start_time = time.monotonic()
        self.stat_cards["scan_time"].set_value(0)
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(100)  # обновление каждые 100мс
        self._scan_timer.timeout.connect(self._update_scan_timer)
        self._scan_timer.start()

    def _cancel_scan(self) -> None:
        if self.scan_worker is not None:
            self.scan_worker.cancel()
            self.stop_btn.setEnabled(False)
            self.stop_btn.setProperty("active", False)
            self.stop_btn.style().polish(self.stop_btn)
            self.logger.warning("Сканирование прервано пользователем")

    def _on_progress(self, current: int, total: int, message: str) -> None:
        # total == 0 означает подготовительную фазу: идёт предварительный
        # подсчёт либо поиск первых файлов. Общее число ещё не известно,
        # поэтому «Обработано: 0 / 0» вводило бы в заблуждение, а полоса
        # остаётся в неопределённом режиме и просто показывает активность.
        if total <= 0:
            if message:
                self.progress_label.setText(message)
                self.status_bar.showMessage(message)
            return

        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.progress_label.setText(f"Обработано: {current} / {total}")

        if message:
            self.processing_list.addItem(message)
            self.processing_list.scrollToBottom()
            # Ограничиваем список 50 элементами, чтобы не нагружать виджет
            while self.processing_list.count() > 50:
                self.processing_list.takeItem(0)

    def _on_stats_update(self, stats) -> None:
        """Динамически обновляет карточки статистики во время сканирования."""
        for key, card in self.stat_cards.items():
            if key == "scan_time":
                continue  # время обновляется отдельным таймером
            if key == "found":
                continue  # found обновляется в _on_results_found
            card.set_value(getattr(stats, key, 0))

    def _on_results_found(self, results: list) -> None:
        """Показывает совпадения сразу после обработки каждого файла."""
        if not results:
            return

        scrollbar = self.results_table.verticalScrollBar()
        follow_tail = scrollbar.value() >= max(0, scrollbar.maximum() - 2)
        self.results_model.add_results(results)

        for result in results:
            self._add_found_file_path(result.full_path)

        self._auto_fit_context_column()

        occurrence_count = self.results_model.occurrence_count()
        self.stat_cards["found"].set_value(occurrence_count)
        self.status_bar.showMessage(
            f"Сканирование выполняется: найдено совпадений — {occurrence_count}; "
            f"строк — {self.results_model.rowCount()}"
        )
        if follow_tail:
            self.results_table.scrollToBottom()

    def _on_results_section_resized(self, index: int, _old: int, _new: int) -> None:
        """Ручное перетаскивание границы отключает автоподгонку.

        Пользователь выставил ширину сам — переопределять её при следующей
        порции результатов было бы навязчиво.
        """
        if index == _CONTEXT_COLUMN_INDEX and not self._adjusting_context_column:
            self._context_column_user_sized = True

    def _auto_fit_context_column(self) -> None:
        """Подгоняет «Контекст» под самую длинную строку столбца.

        Измеряется одна строка, а не вся модель: длина контекста ограничена
        настройкой, поэтому самая длинная по числу символов практически всегда
        и самая широкая. Возможная погрешность пропорционального шрифта
        перекрывается запасом ``_CONTEXT_COLUMN_PADDING``.
        """
        if self._context_column_user_sized:
            return
        longest = self.results_model.longest_context()
        if not longest:
            return
        width = self.results_table.fontMetrics().horizontalAdvance(longest)
        width = max(
            _CONTEXT_COLUMN_MIN_WIDTH,
            min(_CONTEXT_COLUMN_MAX_WIDTH, width + _CONTEXT_COLUMN_PADDING),
        )
        if width == self.results_table.columnWidth(_CONTEXT_COLUMN_INDEX):
            return
        self._adjusting_context_column = True
        try:
            self.results_table.setColumnWidth(_CONTEXT_COLUMN_INDEX, width)
        finally:
            self._adjusting_context_column = False

    def _update_scan_timer(self) -> None:
        """Обновляет карточку 'Время сканирования' в реальном времени."""
        if self._scan_start_time > 0:
            elapsed = time.monotonic() - self._scan_start_time
            self.stat_cards["scan_time"].set_value(f"{elapsed:.1f}")

    def _stop_scan_timer(self) -> None:
        """Останавливает таймер сканирования."""
        if self._scan_timer is not None:
            self._scan_timer.stop()
            self._scan_timer.deleteLater()
            self._scan_timer = None

    def _on_scan_failed(self, message: str) -> None:
        self._stop_scan_timer()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setProperty("active", False)
        self.stop_btn.style().polish(self.stop_btn)
        self.status_bar.showMessage("Сканирование завершилось внутренней ошибкой")
        self.logger.error("Сбой рабочего потока сканирования: %s", message)
        self._unreadable_entries = []
        self._update_unreadable_files_button()
        QMessageBox.critical(self, "Ошибка сканирования", message)
        if self._closing_after_scan:
            self._closing_after_scan = False
            self.close()

    def _on_log_message(self, level: str, msg: str) -> None:
        """Именованный обработчик для логов сканирования (чтобы можно было
        отключить сигнал при повторном запуске)."""
        self.logger.log(getattr(logging, level, logging.INFO), msg)

    def _on_scan_finished(self, report: ScanReport) -> None:
        self._stop_scan_timer()
        self.last_report = report
        self.last_search_info = dict(self._active_search_info)
        self._unreadable_entries = list(report.unreadable_files)
        self._update_unreadable_files_button()
        self._populate_found_files()
        self._auto_fit_context_column()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setProperty("active", False)
        self.stop_btn.style().polish(self.stop_btn)

        status = "отменено" if report.cancelled else "завершено"
        if report.stats.processed > 0:
            self.progress_bar.setMaximum(report.stats.processed)
            self.progress_bar.setValue(report.stats.processed)
        self.progress_label.setText(
            f"Обработано: {report.stats.processed} / {report.stats.processed}"
        )

        self.status_bar.showMessage(
            f"Сканирование {status}: {len(report.results)} совпадений за {report.elapsed_seconds:.1f} с"
        )

        stats = report.stats
        for key, card in self.stat_cards.items():
            if key == "scan_time":
                card.set_value(f"{report.elapsed_seconds:.1f}")
            else:
                card.set_value(getattr(stats, key))

        self.logger.info(
            "Сканирование завершено (%s): обработано=%d, найдено=%d, ошибок=%d, время=%.1fс",
            status,
            stats.processed,
            len(report.results),
            stats.errors,
            report.elapsed_seconds,
        )

        if not report.cancelled:
            self._maybe_auto_send_report(report)

        if self._closing_after_scan:
            self._closing_after_scan = False
            self.close()
            return

        # Если совпадений 0 — выводим всплывающее окно с анализом причин (рекомендация отчёта)
        if len(report.results) == 0 and not report.cancelled:
            QMessageBox.information(
                self,
                "Совпадений не найдено",
                f"Сканирование успешно завершено за {report.elapsed_seconds:.1f} с, но искомые слова не обнаружены.\n\n"
                "Рекомендации по поиску:\n"
                "1. Убедитесь, что ключевые слова написаны без опечаток.\n"
                "2. Если включена опция «Учитывать регистр» или «Только целые слова», попробуйте отключить их.\n"
                "3. Проверьте, что в параметрах выбраны нужные типы файлов.\n"
                "4. Для отсканированных PDF без текстового слоя убедитесь, что Tesseract OCR настроен корректно.\n\n"
                "Для быстрой проверки извлечения текста из конкретного подозрительного файла "
                "воспользуйтесь кнопкой «🔍 Проверить 1 файл…».",
            )

    # ------------------------------------------------------------------ #
    # Действия на вкладке "Результаты"
    # ------------------------------------------------------------------ #
    def _current_result(self):
        indexes = self.results_table.selectionModel().selectedRows()
        if not indexes:
            return None
        source_index = self.proxy_model.mapToSource(indexes[0])
        return self.results_model.result_at(source_index.row())

    def _on_result_table_clicked(self, proxy_index) -> None:
        """Открывает все контексты при нажатии на число совпадений."""
        if not proxy_index.isValid() or proxy_index.column() != 2:
            return
        source_index = self.proxy_model.mapToSource(proxy_index)
        results = self.results_model.group_results_at(source_index.row())
        if results:
            ResultContextsDialog(results, self).exec()

    def _open_selected_result(self, proxy_index=None) -> None:
        # Двойной щелчок по счётчику не должен одновременно открывать файл.
        if proxy_index is not None and proxy_index.isValid() and proxy_index.column() == 2:
            return
        result = self._current_result()
        if result:
            self._open_path(result.full_path)

    def _open_selected_folder(self) -> None:
        result = self._current_result()
        if result:
            self._open_path(str(Path(result.full_path).parent))

    def _show_results_context_menu(self, position) -> None:
        result = self._current_result()
        menu = QMenu(self)
        open_file_action = menu.addAction("Открыть файл")
        open_folder_action = menu.addAction("Открыть папку файла")
        menu.addSeparator()
        copy_path_action = menu.addAction("Копировать полный путь")
        copy_context_action = menu.addAction("Копировать контекст")
        menu.addSeparator()
        select_all_action = menu.addAction("Выделить все совпадения в этом файле")

        if result is None:
            for a in (
                open_file_action,
                open_folder_action,
                copy_path_action,
                copy_context_action,
                select_all_action,
            ):
                a.setEnabled(False)

        action = menu.exec(self.results_table.viewport().mapToGlobal(position))
        if action is None:
            return

        if action == open_file_action and result:
            self._open_path(result.full_path)
        elif action == open_folder_action and result:
            self._open_path(str(Path(result.full_path).parent))
        elif action == copy_path_action and result:
            QApplication.clipboard().setText(result.full_path, QClipboard.Clipboard)
            self.status_bar.showMessage("Путь скопирован в буфер обмена")
        elif action == copy_context_action and result:
            QApplication.clipboard().setText(result.context, QClipboard.Clipboard)
            self.status_bar.showMessage("Контекст скопирован в буфер обмена")
        elif action == select_all_action and result:
            for row in range(self.results_model.rowCount()):
                r = self.results_model.result_at(row)
                if r.full_path == result.full_path:
                    idx = self.results_model.index(row, 0)
                    proxy_idx = self.proxy_model.mapFromSource(idx)
                    self.results_table.selectRow(proxy_idx.row())

    @staticmethod
    def _open_path(path: str) -> None:
        if not path:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).expanduser())))

    def _search_info(self) -> dict:
        # После сканирования отчёт должен описывать фактически использованные
        # параметры, даже если пользователь уже изменил поля интерфейса.
        if self.last_search_info:
            info = dict(self.last_search_info)
            info["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return info
        paths = [self.paths_list.item(i).text() for i in range(self.paths_list.count())]
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "paths": paths,
            "words": parse_terms(self.words_edit.toPlainText()),
            "filename_words": parse_terms(self.filename_words_edit.toPlainText()),
            "doc_method": self.doc_method_combo.currentText(),
            "external_after_heuristic_failure": (
                self.heuristic_external_check.isChecked()
            ),
        }

    def _export_excel(self) -> None:
        if not self.last_report:
            QMessageBox.information(
                self, "Нет данных", "Сначала выполните сканирование."
            )
            return
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        default_name = str(
            REPORTS_DIR / f"search_results_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить Excel отчёт", default_name, "Excel (*.xlsx)"
        )
        if not path:
            return
        ok = export_to_excel(
            self.last_report.results,
            self.last_report.stats,
            self.last_report.errors,
            self._search_info(),
            path,
        )
        if ok:
            QMessageBox.information(self, "Готово", f"Отчёт сохранён:\n{path}")
            self._open_path(path)
        else:
            QMessageBox.warning(
                self, "Ошибка", "Не удалось создать Excel-файл (проверьте openpyxl)."
            )

    def _export_csv(self) -> None:
        if not self.last_report:
            QMessageBox.information(
                self, "Нет данных", "Сначала выполните сканирование."
            )
            return
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        default_name = str(
            REPORTS_DIR / f"search_results_{datetime.now():%Y%m%d_%H%M%S}.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить CSV", default_name, "CSV (*.csv)"
        )
        if not path:
            return
        ok = export_to_csv(self.last_report.results, path, self.last_report.errors)
        if ok:
            QMessageBox.information(self, "Готово", f"CSV сохранён:\n{path}")
            self._open_path(path)
        else:
            QMessageBox.warning(self, "Ошибка", "Не удалось создать CSV-файл.")

    def _export_markdown(self) -> None:
        if not self.last_report:
            QMessageBox.information(
                self, "Нет данных", "Сначала выполните сканирование."
            )
            return
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        default_name = str(
            REPORTS_DIR / f"search_results_{datetime.now():%Y%m%d_%H%M%S}.md"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить Markdown", default_name, "Markdown (*.md)"
        )
        if not path:
            return
        ok = export_to_markdown(
            self.last_report.results, self._search_info(), self.last_report.errors, path
        )
        if ok:
            QMessageBox.information(self, "Готово", f"Markdown сохранён:\n{path}")
            self._open_path(path)
        else:
            QMessageBox.warning(self, "Ошибка", "Не удалось создать Markdown-файл.")

    # ------------------------------------------------------------------ #
    # Действия на вкладке "Файлы"
    # ------------------------------------------------------------------ #
    def _populate_found_files(self) -> None:
        expected_paths = self.results_model.unique_paths()
        expected_set = set(expected_paths)

        # Удаляем только устаревшие строки и не сбрасываем галочки у файлов,
        # которые уже появились во время сканирования.
        for index in range(self.found_files_list.count() - 1, -1, -1):
            item = self.found_files_list.item(index)
            full_path = str(item.data(Qt.UserRole) or item.text())
            if full_path not in expected_set:
                self.found_files_list.takeItem(index)
                self._found_file_paths.discard(full_path)
        self._update_files_tab_title()

        for full_path in self.results_model.unique_paths():
            self._add_found_file_path(full_path)

    def _add_found_file_path(self, full_path: str) -> None:
        """Добавляет путь или обновляет его rich-text подсказку с контекстами."""
        if not full_path:
            return

        tooltip = file_tooltip_html(
            full_path,
            self.results_model.results_for_path(full_path),
        )
        if full_path in self._found_file_paths:
            for index in range(self.found_files_list.count()):
                item = self.found_files_list.item(index)
                if str(item.data(Qt.UserRole) or item.text()) == full_path:
                    item.setToolTip(tooltip)
                    break
            return

        item = QListWidgetItem(full_path)
        item.setData(Qt.UserRole, full_path)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Unchecked)
        item.setToolTip(tooltip)
        self.found_files_list.addItem(item)
        self._found_file_paths.add(full_path)
        self._update_files_tab_title()

    def _update_files_tab_title(self) -> None:
        """Число найденных файлов видно на самой вкладке, не только по её содержимому."""
        count = self.found_files_list.count()
        self.tabs.setTabText(1, f"Файлы ({count})" if count else "Файлы")

    def _set_all_found_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for index in range(self.found_files_list.count()):
            self.found_files_list.item(index).setCheckState(state)

    def _checked_found_paths(self) -> List[str]:
        paths: List[str] = []
        for index in range(self.found_files_list.count()):
            item = self.found_files_list.item(index)
            if item.checkState() == Qt.Checked:
                paths.append(str(item.data(Qt.UserRole) or item.text()))
        return paths

    def _browse_copy_dest(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Папка назначения для копирования"
        )
        if folder:
            self.copy_dest_edit.setText(folder)

    def _copy_found_files(self) -> None:
        if any(
            worker is not None and worker.isRunning()
            for worker in (
                self.copy_worker,
                self.secure_worker,
                self.error_copy_worker,
                self.error_secure_worker,
            )
        ):
            QMessageBox.information(
                self, "Копирование", "Другая файловая операция уже выполняется."
            )
            return
        paths = self._checked_found_paths()
        if not paths:
            QMessageBox.information(
                self, "Нет отмеченных файлов", "Отметьте файлы чекбоксами слева."
            )
            return
        destination = self.copy_dest_edit.text().strip()
        if not destination:
            QMessageBox.warning(self, "Не указан путь", "Укажите папку назначения.")
            return

        self.copy_worker = CopyWorker(paths, destination)
        self.copy_worker.finished_ok.connect(self._on_copy_finished)
        self.copy_worker.failed.connect(self._on_copy_failed)
        self.copy_btn.setEnabled(False)
        self.status_bar.showMessage("Копирование отмеченных файлов…")
        self.copy_worker.start()

    def _on_copy_finished(self, mappings: List[CopyMapping]) -> None:
        self.copy_btn.setEnabled(True)
        self.copy_mappings = mappings
        self.status_bar.showMessage(f"Скопировано файлов: {len(mappings)}")
        QMessageBox.information(
            self,
            "Копирование завершено",
            f"Скопировано {len(mappings)} файлов в:\n{self.copy_dest_edit.text()}",
        )
        self._open_path(self.copy_dest_edit.text())
        self._maybe_close_after_workers()

    def _on_copy_failed(self, message: str) -> None:
        self.copy_btn.setEnabled(True)
        QMessageBox.critical(self, "Ошибка копирования", message)
        self._maybe_close_after_workers()

    def _secure_delete_selected(self) -> None:
        if any(
            worker is not None and worker.isRunning()
            for worker in (
                self.copy_worker,
                self.secure_worker,
                self.error_copy_worker,
                self.error_secure_worker,
            )
        ):
            QMessageBox.information(
                self, "Операция", "Файловая операция уже выполняется."
            )
            return
        paths = self._checked_found_paths()
        if not paths:
            QMessageBox.information(
                self, "Нет отмеченных файлов", "Отметьте файлы чекбоксами слева."
            )
            return
        confirm = QMessageBox.question(
            self,
            "Необратимое удаление",
            f"Безопасно удалить {len(paths)} отмеченных файлов?\n\n"
            "Оригиналы будут многократно перезаписаны и удалены. Действие необратимо.",
        )
        if confirm != QMessageBox.Yes:
            return
        from app.settings_store import load_settings

        self.secure_worker = SecureOpWorker(
            "delete", paths, passes=load_settings().secure_passes
        )
        self.secure_worker.finished_ok.connect(
            lambda result: self._on_secure_finished("удалено", result)
        )
        self.secure_worker.failed.connect(self._on_secure_failed)
        self._set_secure_controls_enabled(False)
        self.secure_worker.start()

    def _secure_move_selected(self) -> None:
        if any(
            worker is not None and worker.isRunning()
            for worker in (
                self.copy_worker,
                self.secure_worker,
                self.error_copy_worker,
                self.error_secure_worker,
            )
        ):
            QMessageBox.information(
                self, "Операция", "Файловая операция уже выполняется."
            )
            return
        paths = self._checked_found_paths()
        if not paths:
            QMessageBox.information(
                self, "Нет отмеченных файлов", "Отметьте файлы чекбоксами слева."
            )
            return
        destination = QFileDialog.getExistingDirectory(
            self, "Куда безопасно переместить файлы"
        )
        if not destination:
            return
        confirm = QMessageBox.question(
            self,
            "Безопасное перемещение",
            f"Переместить {len(paths)} отмеченных файлов в:\n{destination}\n\n"
            "Сначала будет создана новая копия, затем оригинал будет многократно "
            "перезаписан и удалён с прежнего места.",
        )
        if confirm != QMessageBox.Yes:
            return
        from app.settings_store import load_settings

        self.secure_worker = SecureOpWorker(
            "move",
            paths,
            destination,
            passes=load_settings().secure_passes,
        )
        self.secure_worker.finished_ok.connect(
            lambda result: self._on_secure_finished("перемещено", result)
        )
        self.secure_worker.failed.connect(self._on_secure_failed)
        self._set_secure_controls_enabled(False)
        self.secure_worker.start()

    def _set_secure_controls_enabled(self, enabled: bool) -> None:
        self.secure_delete_btn.setEnabled(enabled)
        self.secure_move_btn.setEnabled(enabled)
        self.copy_btn.setEnabled(enabled)

    def _refresh_found_files_after_operation(self) -> None:
        removed_paths: set[str] = set()
        for index in range(self.found_files_list.count() - 1, -1, -1):
            item = self.found_files_list.item(index)
            source_text = str(item.data(Qt.UserRole) or item.text())
            if not Path(source_text).exists():
                removed_paths.add(source_text)
                self.found_files_list.takeItem(index)
                self._found_file_paths.discard(source_text)
        if removed_paths:
            self.results_model.remove_paths(removed_paths)
            if self.last_report is not None:
                self.last_report.results = [
                    result
                    for result in self.last_report.results
                    if result.full_path not in removed_paths
                ]
                self.last_report.stats.found = len(self.last_report.results)
                self.stat_cards["found"].set_value(self.last_report.stats.found)
        if removed_paths:
            self._update_files_tab_title()

    def _on_secure_finished(self, action: str, result: OperationResult) -> None:
        self._set_secure_controls_enabled(True)
        self._refresh_found_files_after_operation()
        message = f"Успешно {action} файлов: {result.succeeded}"
        if result.failures:
            preview = "\n".join(result.failures[:8])
            extra = (
                f"\n…ещё ошибок: {len(result.failures) - 8}"
                if len(result.failures) > 8
                else ""
            )
            QMessageBox.warning(
                self,
                "Операция завершена частично",
                f"{message}\nНе обработано: {len(result.failures)}\n\n{preview}{extra}",
            )
        else:
            QMessageBox.information(self, "Готово", message)
        self._maybe_close_after_workers()

    def _on_secure_failed(self, message: str) -> None:
        self._set_secure_controls_enabled(True)
        QMessageBox.critical(self, "Ошибка", message)
        self._maybe_close_after_workers()

    # ------------------------------------------------------------------ #
    # Диалог настроек и о программе
    # ------------------------------------------------------------------ #
    def _open_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec():
            self._auto_save_scan_config()
            self.status_bar.showMessage("Настройки сохранены")

    def _open_email_settings(self) -> None:
        dialog = EmailSettingsDialog(self.email_settings, self)
        if dialog.exec():
            self.email_settings = dialog.result_settings
            self._auto_save_scan_config()
            self.status_bar.showMessage("Настройки почты сохранены в переносимом JSON")

    def _maybe_auto_send_report(self, report: ScanReport) -> None:
        settings = self.email_settings.sanitized()
        if not settings.auto_send:
            return
        if not report.results and not settings.send_when_no_matches:
            self.logger.info("Автоотправка пропущена: совпадений нет")
            return
        if self.email_worker is not None and self.email_worker.isRunning():
            self.logger.warning("Предыдущая отправка отчёта ещё не завершена")
            return
        errors = settings.validation_errors(require_delivery=True)
        if errors:
            message = " ".join(errors)
            self.logger.error("Автоотправка не выполнена: %s", message)
            QMessageBox.warning(self, "Отправка отчёта", message)
            return

        self.status_bar.showMessage("Формирование и отправка отчёта по email…")
        self.email_worker = EmailReportWorker(
            report, self._search_info(), settings, self
        )
        self.email_worker.finished_ok.connect(self._on_email_sent)
        self.email_worker.failed.connect(self._on_email_failed)
        self.email_worker.start()

    def _on_email_sent(self, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        sent = data.get("sent")
        recipients = ", ".join(getattr(sent, "recipients", ()))
        self.logger.info("Отчёт отправлен по email: %s", recipients)
        self.status_bar.showMessage(f"Отчёт отправлен: {recipients}")
        self._maybe_close_after_workers()

    def _on_email_failed(self, message: str) -> None:
        self.logger.error("Не удалось отправить отчёт по email: %s", message)
        self.status_bar.showMessage("Ошибка отправки отчёта по email")
        QMessageBox.warning(self, "Ошибка отправки email", message)
        self._maybe_close_after_workers()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "О программе",
            f"<h2>DSP Scanner {APP_VERSION}</h2>"
            "<p>Инструмент поиска ключевых слов в документах DOCX, DOC, TXT и PDF.</p>"
            "<p>Python-версия с модульной архитектурой и графическим интерфейсом PySide6/Qt6.</p>"
            "<p><b>Разработчик:</b> Bytsko Dmitry<br>"
            "<b>Email:</b> <a href='mailto:gbiwork@gmail.com'>gbiwork@gmail.com</a></p>"
            "<p>Возможности:</p>"
            "<ul>"
            "<li>Точное чтение DOCX (python-docx + парсер вложений altChunk MHTML)</li>"
            "<li>Чтение легаси DOC через LibreOffice / MS Word COM / эвристику OLE2</li>"
            "<li>Чтение PDF через PyMuPDF с мощным многоязычным OCR (Tesseract)</li>"
            "<li>Быстрая диагностика подозрительных документов в один клик</li>"
            "<li>Экспорт в Excel / CSV / Markdown и автоматическая отправка по SMTP</li>"
            "<li>Графический и командный (CLI) режим для серверного запуска</li>"
            "<li>Автосохранение параметров в переносимый JSON (DSPScanner_Config)</li>"
            "<li>Копирование и безопасное (многопроходное) удаление файлов</li>"
            "</ul>",
        )

    # ------------------------------------------------------------------ #
    # Сохранение/загрузка конфигурации сканирования
    # ------------------------------------------------------------------ #
    def _current_scan_config(self) -> dict:
        """Снимок всех параметров поиска, пригодный для переносимого JSON."""
        return {
            "schema_version": 6,
            "paths": [
                self.paths_list.item(i).text() for i in range(self.paths_list.count())
            ],
            "paths_mode": self._paths_mode,
            "words": self.words_edit.toPlainText(),
            "filename_words": self.filename_words_edit.toPlainText(),
            "file_types": [
                ext for ext, cb in self.type_checks.items() if cb.isChecked()
            ],
            "doc_method": self._doc_method_from_data(
                self.doc_method_combo.currentData()
            ).value,
            "external_after_heuristic_failure": (
                self.heuristic_external_check.isChecked()
            ),
            "precount_files": self.precount_check.isChecked(),
            "case_sensitive": self.case_sensitive_check.isChecked(),
            "whole_word": self.whole_word_check.isChecked(),
            "use_ocr": self.ocr_check.isChecked(),
            "limit_pdf_pages": self.limit_pdf_pages_check.isChecked(),
            "pdf_page_limit": self.pdf_page_limit_spin.value(),
            "max_size_mb": self.max_size_spin.value(),
            "context_chars": self.context_spin.value(),
            "copy_dest": self.copy_dest_edit.text(),
            "application": load_settings().to_mapping(),
            "email": self.email_settings.to_mapping(),
        }

    def _apply_scan_config(self, config: dict) -> None:
        """Безопасно применяет JSON, игнорируя неизвестные и неверные поля."""
        self._restoring_scan_config = True
        try:
            raw_paths = config.get("paths", [])
            paths = (
                [str(item) for item in raw_paths if isinstance(item, str)]
                if isinstance(raw_paths, list)
                else []
            )
            raw_paths_mode = config.get("paths_mode")
            paths_mode = "default" if raw_paths_mode == "default" else "custom"
            words = config.get("words", "")
            words = words if isinstance(words, str) else ""
            filename_words = config.get("filename_words", "")
            filename_words = filename_words if isinstance(filename_words, str) else ""
            # Совместимость с конфигурациями до 2.5.0.
            if not filename_words and bool(config.get("search_filenames", False)):
                filename_words = words

            raw_types = config.get("file_types", list(SUPPORTED_EXTENSIONS))
            file_types = (
                {str(item).lower() for item in raw_types if isinstance(item, str)}
                if isinstance(raw_types, list)
                else set(SUPPORTED_EXTENSIONS)
            )

            if paths_mode == "default":
                # Пересчитываем набор для текущего компьютера: профиль и диски
                # могут отличаться от машины, на которой был создан JSON.
                self._replace_search_paths(default_search_paths(), mode="default")
            else:
                # Старые JSON без paths_mode считаются пользовательскими, чтобы
                # не перезаписать уже настроенные вручную каталоги.
                self._replace_search_paths(paths, mode="custom")
            self.words_edit.setPlainText(words)
            self.filename_words_edit.setPlainText(filename_words)
            for ext, checkbox in self.type_checks.items():
                checkbox.setChecked(ext in file_types)

            doc_method = config.get("doc_method", DocReadMethod.AUTO.value)
            for index in range(self.doc_method_combo.count()):
                if (
                    self._doc_method_from_data(
                        self.doc_method_combo.itemData(index)
                    ).value
                    == doc_method
                ):
                    self.doc_method_combo.setCurrentIndex(index)
                    break
            self.heuristic_external_check.setChecked(
                bool(config.get("external_after_heuristic_failure", True))
            )
            self._update_heuristic_external_visibility()
            self.precount_check.setChecked(bool(config.get("precount_files", True)))

            self.case_sensitive_check.setChecked(
                bool(config.get("case_sensitive", False))
            )
            self.whole_word_check.setChecked(bool(config.get("whole_word", False)))
            self.ocr_check.setChecked(bool(config.get("use_ocr", True)))
            self.limit_pdf_pages_check.setChecked(
                bool(config.get("limit_pdf_pages", False))
            )
            try:
                self.pdf_page_limit_spin.setValue(
                    int(config.get("pdf_page_limit", PDF_PAGE_LIMIT_DEFAULT))
                )
            except (TypeError, ValueError, OverflowError):
                self.pdf_page_limit_spin.setValue(PDF_PAGE_LIMIT_DEFAULT)
            self._update_pdf_page_limit_controls()
            try:
                self.max_size_spin.setValue(
                    float(config.get("max_size_mb", MAX_FILE_SIZE_MB_DEFAULT))
                )
            except (TypeError, ValueError, OverflowError):
                self.max_size_spin.setValue(MAX_FILE_SIZE_MB_DEFAULT)
            try:
                self.context_spin.setValue(
                    int(config.get("context_chars", CONTEXT_CHARS_DEFAULT))
                )
            except (TypeError, ValueError, OverflowError):
                self.context_spin.setValue(CONTEXT_CHARS_DEFAULT)
            copy_dest = config.get("copy_dest")
            if isinstance(copy_dest, str) and copy_dest:
                self.copy_dest_edit.setText(copy_dest)

            application = config.get("application")
            if isinstance(application, dict):
                if not save_settings(AppSettings.from_mapping(application)):
                    self.logger.warning(
                        "Не удалось применить глобальные параметры из JSON"
                    )
            self.email_settings = load_email_settings(config)
        finally:
            self._restoring_scan_config = False

    def _setup_auto_save(self) -> None:
        """Подключает отложенное автосохранение, чтобы не писать JSON на каждый символ."""
        ensure_config_dir()
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.setInterval(600)
        self._auto_save_timer.timeout.connect(self._auto_save_scan_config)

        self.words_edit.textChanged.connect(self._schedule_auto_save)
        self.filename_words_edit.textChanged.connect(self._schedule_auto_save)
        self.copy_dest_edit.textChanged.connect(self._schedule_auto_save)
        for checkbox in self.type_checks.values():
            checkbox.toggled.connect(self._schedule_auto_save)
        for checkbox in (
            self.case_sensitive_check,
            self.whole_word_check,
            self.ocr_check,
            self.limit_pdf_pages_check,
            self.heuristic_external_check,
            self.precount_check,
        ):
            checkbox.toggled.connect(self._schedule_auto_save)
        self.doc_method_combo.currentIndexChanged.connect(self._schedule_auto_save)
        self.max_size_spin.valueChanged.connect(self._schedule_auto_save)
        self.context_spin.valueChanged.connect(self._schedule_auto_save)
        self.pdf_page_limit_spin.valueChanged.connect(self._schedule_auto_save)

        model = self.paths_list.model()
        model.rowsInserted.connect(self._on_paths_model_changed)
        model.rowsRemoved.connect(self._on_paths_model_changed)
        model.modelReset.connect(self._on_paths_model_changed)
        model.dataChanged.connect(self._on_paths_model_changed)

    def _schedule_auto_save(self, *_args) -> None:
        if not self._restoring_scan_config:
            self._auto_save_timer.start()

    def _auto_save_scan_config(self) -> None:
        if self._restoring_scan_config:
            return
        try:
            target = save_scan_config(self._current_scan_config())
            self.logger.debug("Автоконфигурация сохранена: %s", target)
        except OSError as exc:
            self.logger.warning(
                "Не удалось автоматически сохранить конфигурацию: %s", exc
            )

    def _save_scan_config(self) -> None:
        default_path = str(auto_config_path())
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить конфигурацию поиска", default_path, "JSON (*.json)"
        )
        if not path:
            return
        try:
            target = save_scan_config(self._current_scan_config(), path)
            self.status_bar.showMessage(f"Конфигурация сохранена: {target}")
        except OSError as exc:
            QMessageBox.warning(
                self, "Ошибка", f"Не удалось сохранить конфигурацию:\n{exc}"
            )

    def _load_scan_config(self) -> None:
        start_dir = str(existing_auto_config_path() or auto_config_path())
        path, _ = QFileDialog.getOpenFileName(
            self, "Загрузить конфигурацию поиска", start_dir, "JSON (*.json)"
        )
        if not path:
            return
        try:
            config = load_scan_config(path)
            if config is None:
                raise ValueError("Файл конфигурации не найден")
            self._apply_scan_config(config)
            # Импортированная конфигурация сразу становится текущей автоматической.
            target = save_scan_config(self._current_scan_config())
            self.status_bar.showMessage(f"Конфигурация загружена; автокопия: {target}")
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(
                self, "Ошибка", f"Не удалось загрузить конфигурацию:\n{exc}"
            )

    # ------------------------------------------------------------------ #
    # Сохранение/восстановление базовых настроек
    # ------------------------------------------------------------------ #
    def _save_settings(self) -> None:
        # QSettings оставлен для совместимости со старыми версиями; источником
        # истины для параметров поиска теперь является переносимый JSON.
        paths = [self.paths_list.item(i).text() for i in range(self.paths_list.count())]
        self.settings_store.setValue("paths", paths)
        self.settings_store.setValue("paths_mode", self._paths_mode)
        self.settings_store.setValue("words", self.words_edit.toPlainText())
        self.settings_store.setValue(
            "filename_words", self.filename_words_edit.toPlainText()
        )
        self.settings_store.setValue("copy_dest", self.copy_dest_edit.text())
        self._auto_save_timer.stop()
        self._auto_save_scan_config()

    def _restore_settings(self) -> None:
        ensure_config_dir()
        existing = existing_auto_config_path()
        if existing is not None:
            try:
                config = load_scan_config(existing)
                if config is not None:
                    self._apply_scan_config(config)
                    self.logger.info("Параметры поиска загружены из JSON: %s", existing)
                    return
            except (OSError, ValueError, TypeError) as exc:
                self.logger.warning(
                    "Не удалось прочитать автоконфигурацию %s: %s", existing, exc
                )

        # Однократная миграция данных прежних версий из QSettings.
        paths = self.settings_store.value("paths", [])
        if isinstance(paths, str):
            paths = [paths]
        migrated_paths = [str(path) for path in (paths or []) if str(path).strip()]
        migrated_mode = str(self.settings_store.value("paths_mode", "custom"))
        if migrated_mode == "default":
            self._set_default_search_paths(notify=False)
        elif migrated_paths:
            self._replace_search_paths(migrated_paths, mode="custom")
        else:
            # Самый первый запуск: безопасно ограничиваем системный диск C:
            # профилем пользователя, а остальные доступные диски берём целиком.
            self._set_default_search_paths(notify=False)
        words = self.settings_store.value("words", "")
        if words:
            self.words_edit.setPlainText(words)
        filename_words = self.settings_store.value("filename_words", "")
        if filename_words:
            self.filename_words_edit.setPlainText(filename_words)
        copy_dest = self.settings_store.value("copy_dest", "")
        if copy_dest:
            self.copy_dest_edit.setText(copy_dest)

    def _workers_running(self) -> bool:
        return any(
            worker is not None and worker.isRunning()
            for worker in (
                self.scan_worker,
                self.copy_worker,
                self.secure_worker,
                self.error_copy_worker,
                self.error_secure_worker,
                self.email_worker,
                self.single_file_worker,
            )
        )

    def _maybe_close_after_workers(self) -> None:
        if self._closing_after_scan and not self._workers_running():
            self._closing_after_scan = False
            self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.settings_store.setValue("geometry", self.saveGeometry())
        self.settings_store.setValue("splitter", self.splitter.saveState())
        self._save_settings()
        if self._workers_running():
            self._closing_after_scan = True
            if self.scan_worker is not None and self.scan_worker.isRunning():
                self.scan_worker.cancel()
                self.status_bar.showMessage("Остановка сканирования перед выходом…")
            else:
                self.status_bar.showMessage(
                    "Ожидание завершения фоновой операции перед выходом…"
                )
            event.ignore()
            return
        super().closeEvent(event)
