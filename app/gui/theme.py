"""
Современная тёмная тема в духе Fluent/Material 3 (QSS), применяемая ко
всему приложению без тяжёлых внешних зависимостей.

Рефакторинг v2: убраны артефакты, вызывавшие перерасчёт геометрии при
обновлении виджетов (в частности `QTabWidget::pane { top: -1px }`,
из-за которого при переключении вкладок "прыгала" высота таб-бара,
и растягивающиеся секции заголовков таблиц). Все размеры фиксированы
через QSS или `setMinimumSize`, что устраняет хаотичное изменение
границ интерфейса во время сканирования.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#4C8DFF"
ACCENT_HOVER = "#6AA1FF"
BG_DARK = "#15181E"
BG_PANEL = "#1D2129"
BG_CARD = "#232833"
BORDER = "#667287"
# Более тусклая рамка для структурных (не интерактивных) контейнеров —
# QGroupBox, панель вкладок, статус-бар. Интерактивные поля ввода/кнопки
# продолжают использовать более яркий BORDER, чтобы отличаться от декора.
BORDER_SUBTLE = "#3A4150"
TEXT = "#E6E9EF"
TEXT_MUTED = "#9AA3B2"
DANGER = "#E5484D"
SUCCESS = "#3DD68C"
WARNING = "#F2B84B"

def _write_svg_temp(file_name: str, svg: str) -> str:
    """Сохраняет небольшую SVG-иконку во временный каталог и возвращает путь,
    пригодный для использования в Qt StyleSheets."""
    import os
    import tempfile
    from pathlib import Path as _P

    path = _P(tempfile.gettempdir()) / file_name
    try:
        # Имя файла в общем временном каталоге предсказуемо (одинаково при
        # каждом запуске). На многопользовательской машине O_NOFOLLOW не даёт
        # записи пойти по символьной ссылке, которую заранее мог создать
        # другой пользователь по этому пути (классическая /tmp symlink-атака).
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(svg)
    except OSError:
        return ""
    return path.as_posix()


_CHECK_ICON = _write_svg_temp(
    "dspscanner_check.svg",
    (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
        '<path d="M2 6.5 L4.8 9.2 L10 3.2" stroke="#0A0C10" stroke-width="2.2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
)
_UP_ARROW_ICON = _write_svg_temp(
    "dspscanner_spin_up.svg",
    (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
        '<path d="M1.6 6.7 L5 3.2 L8.4 6.7" stroke="#C5CCD8" stroke-width="1.9" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
)
_DOWN_ARROW_ICON = _write_svg_temp(
    "dspscanner_spin_down.svg",
    (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
        '<path d="M1.6 3.3 L5 6.8 L8.4 3.3" stroke="#C5CCD8" stroke-width="1.9" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
)
_DASH_ICON = _write_svg_temp(
    "dspscanner_dash.svg",
    (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
        '<path d="M2 6 L10 6" stroke="#E6E9EF" stroke-width="2.2" '
        'stroke-linecap="round"/></svg>'
    ),
)

QSS = f"""
* {{
    font-family: "Segoe UI", "SF Pro Display", "Inter", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QWidget#centralWidget {{
    background-color: {BG_DARK};
}}
/* ВАЖНО: раньше здесь было глобальное `QWidget {{ background: transparent }}`.
   Это правило распространялось и на viewport QTableView/QListWidget — области
   прокрутки переставали затирать предыдущий кадр, и при добавлении строк во
   время сканирования старый текст просвечивал под новым («наложение» надписей
   во всех столбцах таблицы результатов). Прозрачность убрана; фон контейнеров
   обеспечивается наследованием от QMainWindow/центрального виджета. */

/* Вкладки — фиксированная высота таб-бара, без "прыжков" при переключении */
QTabWidget::pane {{
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 10px;
    background-color: {BG_PANEL};
}}
QTabWidget::tab-bar {{
    alignment: left;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 9px 18px;
    margin-right: 2px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-weight: 600;
    min-width: 90px;
}}
QTabBar::tab:selected {{
    color: {TEXT};
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    border-bottom: none;
}}
QTabBar::tab:hover {{
    color: {TEXT};
}}

QGroupBox {{
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 10px;
    margin-top: 18px;
    padding: 18px 10px 10px 10px;
    background-color: {BG_CARD};
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: -3px;
    padding: 0 6px;
    color: {TEXT_MUTED};
}}

QPushButton {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 600;
    min-height: 28px;
}}
QPushButton:hover {{
    background-color: #2A3040;
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: #1A1E27;
}}
QPushButton:disabled {{
    color: {TEXT_MUTED};
    border-color: {BORDER};
}}
QPushButton#primaryButton {{
    background-color: {ACCENT};
    color: #0A0C10;
    border: none;
}}
QPushButton#primaryButton:hover {{
    background-color: {ACCENT_HOVER};
}}
QPushButton#dangerButton {{
    background-color: {DANGER};
    color: white;
    border: none;
}}
QPushButton#dangerButton:hover {{
    background-color: #F2696D;
}}
QPushButton#toolButton {{
    min-height: 18px;
    padding: 4px 10px;
    font-weight: 500;
}}

QPushButton#helpButton {{
    min-width: 22px;
    max-width: 22px;
    min-height: 22px;
    max-height: 22px;
    padding: 0;
    border-radius: 11px;
    background-color: {BG_CARD};
    color: {ACCENT_HOVER};
    border: 1px solid {ACCENT};
    font-weight: 700;
}}
QPushButton#helpButton:hover {{
    background-color: {ACCENT};
    color: #0A0C10;
}}
QPushButton#stopButton {{
    background-color: {BG_CARD};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    min-width: 90px;
}}
QPushButton#stopButton:disabled {{
    color: #5A6372;
    border-color: #3A4150;
    background-color: #1A1E27;
}}
QPushButton#stopButton[active="true"] {{
    background-color: #7A2222;
    color: white;
    border: none;
}}
QPushButton#stopButton[active="true"]:hover {{
    background-color: {DANGER};
}}

QLineEdit, QTextEdit, QPlainTextEdit, QListWidget, QTableView, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {BG_DARK};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px;
    selection-background-color: {ACCENT};
}}
/* Без явной min-height комбобокс и спинбоксы в QGridLayout ужимались ниже
   высоты строки текста — значения («Авто (рекомендуется)», «200,00»)
   обрезались по нижней кромке. */
QComboBox, QSpinBox, QDoubleSpinBox {{
    min-height: 24px;
}}
QSpinBox, QDoubleSpinBox {{
    padding-right: 26px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 20px;
    border-left: 1px solid #677181;
    border-bottom: 1px solid #677181;
    border-top-right-radius: 7px;
    background-color: #2A3040;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 20px;
    border-left: 1px solid #677181;
    border-top: 1px solid #677181;
    border-bottom-right-radius: 7px;
    background-color: #2A3040;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background-color: #333B4F;
    border-color: {ACCENT};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url({_UP_ARROW_ICON});
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url({_DOWN_ARROW_ICON});
    width: 10px;
    height: 10px;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid {ACCENT};
    padding: 5px;
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
}}
QHeaderView::section {{
    background-color: {BG_CARD};
    color: {TEXT_MUTED};
    padding: 7px 8px;
    border: none;
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
}}
QTableView {{
    gridline-color: #3A4252;
    alternate-background-color: {BG_PANEL};
    /* Перерисовка без пересчёта геометрии колонок при добавлении строк */
    show-decoration-selected: 1;
}}
QListWidget::item {{
    border-bottom: 1px solid #262C38;
}}
QListWidget::item:selected {{
    background-color: #2B4771;
}}
QListWidget#processingList {{
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 8px;
}}
QSplitter::handle:horizontal {{
    width: 4px;
    background-color: #2A3140;
    border-left: 1px solid #454F63;
}}
QSplitter::handle:horizontal:hover {{
    background-color: {ACCENT};
}}

QProgressBar {{
    background-color: {BG_DARK};
    border: 1px solid {BORDER};
    border-radius: 8px;
    text-align: center;
    min-height: 22px;
    max-height: 22px;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 8px;
}}

QCheckBox, QRadioButton {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border-radius: 5px;
    border: 1px solid #7A8394;
    background-color: #11151C;
}}
QCheckBox::indicator:hover {{
    border-color: #9BA5B7;
    background-color: #171C24;
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    image: url({_CHECK_ICON});
}}
QCheckBox::indicator:checked:hover {{
    background-color: {ACCENT_HOVER};
}}
QCheckBox::indicator:indeterminate {{
    background-color: #2B4771;
    border-color: {ACCENT};
    image: url({_DASH_ICON});
}}
QCheckBox::indicator:indeterminate:disabled {{
    background-color: #232833;
    border-color: #3A4150;
}}
QCheckBox::indicator:disabled {{
    border: 1px solid #3A4150;
    background-color: #191D25;
}}
QCheckBox:disabled, QRadioButton:disabled {{
    color: #5A6372;
}}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: #5A6372;
    border-color: #3A4150;
    background-color: #191D25;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {ACCENT};
}}

/* Карточки статистики — фиксированная ширина, чтобы цифры разной длины
   не "прыгали" при обновлении */
QLabel#cardValue {{
    font-size: 22px;
    font-weight: 700;
    color: {ACCENT};
    min-width: 60px;
}}
QLabel#cardLabel {{
    color: #B4BDCC;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
}}
QWidget#statCard {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
    min-width: 110px;
    max-width: 110px;
    min-height: 64px;
    max-height: 64px;
}}

/* Вогнутая (гравированная) линия-разделитель секций: тёмная кромка
   сверху + светлый блик снизу создают эффект углублённой канавки */
QFrame#divider {{
    border: none;
    border-top: 1px solid #080A0E;
    border-bottom: 1px solid #8C98AA;
    border-radius: 2px;
    background: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 #07090D,
        stop:0.28 #1B222D,
        stop:0.45 #4A5468,
        stop:0.6 #667287,
        stop:0.78 #303A49,
        stop:1 #0A0D12
    );
}}

/* Заголовки секций левой панели */
QLabel#sectionLabel {{
    color: #B4BDCC;
    font-size: 11px;
    font-weight: 700;
    padding-top: 2px;
    letter-spacing: 0.4px;
}}
QLabel#hintLabel {{
    color: #B4BDCC;
    font-size: 11px;
}}

/* Левая панель настроек и её область прокрутки */
QWidget#leftPanel {{
    background-color: {BG_DARK};
}}
QScrollArea#leftScroll {{
    background-color: {BG_DARK};
    border: none;
}}


QListWidget#pathsList {{
    padding: 8px;
}}
QListWidget#pathsList::item {{
    background-color: #232833;
    border: 1px solid #78859A;
    border-radius: 9px;
    padding: 6px 12px;
    margin: 1px;
}}
QListWidget#pathsList::item:hover {{
    background-color: #2A3040;
    border-color: #9AA8BC;
}}
QListWidget#pathsList::item:selected {{
    background-color: #2B4771;
    border-color: #6AA1FF;
    color: #FFFFFF;
}}

QFrame#contextCard {{
    background-color: #1D2129;
    border: 1px solid #667287;
    border-radius: 9px;
}}
QLabel#contextCardCaption {{
    color: #9AA3B2;
    font-weight: 700;
}}
QScrollArea#contextsScrollArea {{
    border: 1px solid #667287;
    border-radius: 9px;
    background-color: #15181E;
}}
QDialogButtonBox {{
    padding-top: 5px;
    padding-bottom: 3px;
}}

QStatusBar {{
    background-color: {BG_PANEL};
    border-top: 1px solid {BORDER_SUBTLE};
    color: {TEXT_MUTED};
}}
QStatusBar::item {{
    border: none;
}}
QStatusBar QLabel {{
    padding: 0 8px;
}}
QStatusBar QPushButton {{
    padding: 3px 10px;
    min-height: 20px;
    max-height: 24px;
}}


QToolTip {{
    background-color: {BG_CARD};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 8px;
    opacity: 255;
}}

QMenu {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 24px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background-color: {ACCENT};
    color: #0A0C10;
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}

QToolBar {{
    background-color: {BG_PANEL};
    border: none;
    spacing: 6px;
    padding: 4px;
}}
"""


def apply_modern_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG_DARK))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Base, QColor(BG_DARK))
    palette.setColor(QPalette.AlternateBase, QColor(BG_PANEL))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.Button, QColor(BG_CARD))
    palette.setColor(QPalette.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.HighlightedText, QColor("#0A0C10"))
    app.setPalette(palette)
    app.setStyleSheet(QSS)
