"""
Диалог "Параметры" — настройка внешних инструментов (LibreOffice, Tesseract)
и глобальных параметров сканирования, которые неудобно размещать на основной
панели (бывают нужны реже, чем основные настройки поиска).

Кнопки «Определить» рядом с полями путей вызывают автопоиск исполняемых
файлов в PATH и стандартных каталогах установки, и подставляют найденный
путь в поле (или сообщают, что программа не найдена).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout,
    QMessageBox, QWidget,
)

from app.config import MAX_WORKERS_LIMIT
from app.settings_store import load_settings, save_settings, autodetect_external_programs


def _section_form(title: str) -> tuple[QGroupBox, QFormLayout]:
    """Создаёт секцию с гарантированным видимым зазором под заголовком.

    Одних ``contentsMargins`` у ``QGroupBox`` недостаточно: разные версии Qt и
    системные DPI могут частично поглощать верхнее поле рамкой заголовка.
    Отдельный фиксированный spacer-виджет делает отступ реальным элементом
    компоновки и сохраняет его на любом масштабе интерфейса.
    """
    box = QGroupBox(title)
    shell = QVBoxLayout(box)
    shell.setContentsMargins(18, 16, 18, 16)
    shell.setSpacing(0)

    title_gap = QWidget(box)
    title_gap.setObjectName("sectionTitleGap")
    title_gap.setFixedHeight(28)
    title_gap.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    shell.addWidget(title_gap)

    form = QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setVerticalSpacing(12)
    form.setHorizontalSpacing(16)  # зазор между подписями и полями ввода
    shell.addLayout(form)
    return box, form


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Параметры DSP Scanner")
        self.resize(620, 520)
        self.setMinimumSize(560, 420)

        s = load_settings()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(18)

        # Все секции помещаются в прокручиваемую область, чтобы при
        # нехватке высоты окна кнопки OK/Cancel всегда оставались видны.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(18)

        # --- Внешние программы ---
        ext_box, ext_layout = _section_form("Внешние программы")

        # LibreOffice
        self.libreoffice_path = QLineEdit(s.libreoffice_path or "")
        self.libreoffice_path.setPlaceholderText("не задан — будет использована эвристика OLE2")
        lo_btn = QPushButton("Выбрать…")
        lo_btn.clicked.connect(lambda: self._browse(self.libreoffice_path))
        lo_auto = QPushButton("Определить")
        lo_auto.setToolTip("Найти soffice в PATH или стандартном каталоге установки")
        lo_auto.clicked.connect(self._autodetect_libreoffice)
        lo_row = QHBoxLayout()
        lo_row.setSpacing(8)
        lo_row.addWidget(self.libreoffice_path, 1)
        lo_row.addWidget(lo_btn)
        lo_row.addWidget(lo_auto)
        ext_layout.addRow("LibreOffice (soffice):", lo_row)
        ext_layout.addRow(QLabel("Используется для точного чтения легаси .doc"))

        # Tesseract
        self.tesseract_path = QLineEdit(s.tesseract_path or "")
        self.tesseract_path.setPlaceholderText("не задан — OCR будет недоступен")
        ts_btn = QPushButton("Выбрать…")
        ts_btn.clicked.connect(lambda: self._browse(self.tesseract_path))
        ts_auto = QPushButton("Определить")
        ts_auto.setToolTip(
            "Найти tesseract в переносимой папке Tesseract-OCR рядом с программой, "
            "в PATH или стандартном каталоге установки"
        )
        ts_auto.clicked.connect(self._autodetect_tesseract)
        ts_row = QHBoxLayout()
        ts_row.setSpacing(8)
        ts_row.addWidget(self.tesseract_path, 1)
        ts_row.addWidget(ts_btn)
        ts_row.addWidget(ts_auto)
        ext_layout.addRow("Tesseract OCR:", ts_row)
        ext_layout.addRow(QLabel("Используется для распознавания сканированных PDF"))

        # Кнопка "Определить всё"
        detect_all_btn = QPushButton("Определить автоматически")
        detect_all_btn.setObjectName("detectAllProgramsButton")
        detect_all_btn.setMinimumWidth(340)
        detect_all_btn.setMaximumWidth(430)
        detect_all_btn.clicked.connect(self._autodetect_all)
        detect_row = QWidget(ext_box)
        detect_row_layout = QHBoxLayout(detect_row)
        detect_row_layout.setContentsMargins(0, 0, 0, 0)
        detect_row_layout.addStretch(1)
        detect_row_layout.addWidget(detect_all_btn)
        detect_row_layout.addStretch(1)
        ext_layout.addRow(detect_row)

        scroll_layout.addWidget(ext_box)
        scroll_layout.addSpacing(8)

        # --- Производительность ---
        perf_box, perf_layout = _section_form("Производительность")

        self.max_workers = QSpinBox()
        self.max_workers.setRange(1, MAX_WORKERS_LIMIT)
        self.max_workers.setValue(s.max_workers)
        self.max_workers.setToolTip(f"Число потоков параллельной обработки файлов (1–{MAX_WORKERS_LIMIT})")
        perf_layout.addRow("Число потоков обработки:", self.max_workers)

        self.per_file_timeout = QSpinBox()
        self.per_file_timeout.setRange(10, 600)
        self.per_file_timeout.setValue(s.per_file_timeout)
        self.per_file_timeout.setSuffix(" с")
        perf_layout.addRow("Таймаут на один файл:", self.per_file_timeout)
        perf_layout.addRow(QLabel("Защита от зависания на повреждённых файлах"))

        self.max_matches = QSpinBox()
        self.max_matches.setRange(1, 500)
        self.max_matches.setValue(s.max_matches_per_word)
        perf_layout.addRow("Макс. совпадений на слово/файл:", self.max_matches)

        scroll_layout.addWidget(perf_box)
        scroll_layout.addSpacing(8)

        # --- Безопасность ---
        sec_box, sec_layout = _section_form("Безопасное удаление")
        self.secure_passes = QSpinBox()
        self.secure_passes.setRange(1, 7)
        self.secure_passes.setValue(s.secure_passes)
        sec_layout.addRow("Число проходов перезаписи:", self.secure_passes)
        sec_layout.addRow(QLabel("Рекомендуется 3 прохода (DoD 5220.22-M)"))
        scroll_layout.addWidget(sec_box)
        scroll_layout.addSpacing(6)

        # --- Поведение при запуске --- #
        beh_box, beh_layout = _section_form("Поведение при запуске")
        self.reset_stats_on_start = QCheckBox(
            "Обнулять статистику при новом запуске"
        )
        self.reset_stats_on_start.setChecked(s.reset_stats_on_start)
        self.reset_stats_on_start.setToolTip(
            "Если включено, карточки статистики и результаты\n"
            "предыдущего сканирования очищаются при запуске приложения."
        )
        beh_layout.addRow(self.reset_stats_on_start)
        scroll_layout.addWidget(beh_box)
        scroll_layout.addStretch(1)
        # Нижний отступ внутри скролла, чтобы контент не упирался в край
        scroll_bottom_gap = QWidget()
        scroll_bottom_gap.setFixedHeight(12)
        scroll_layout.addWidget(scroll_bottom_gap)

        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)

        # --- Кнопки ---
        layout.addSpacing(4)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ #
    def _browse(self, line_edit: QLineEdit) -> None:
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, "Выберите исполняемый файл")
        if path:
            line_edit.setText(path)

    def _autodetect_libreoffice(self) -> None:
        lo, _ = autodetect_external_programs()
        if lo:
            self.libreoffice_path.setText(lo)
            QMessageBox.information(self, "LibreOffice найден", f"Найден путь:\n{lo}")
        else:
            QMessageBox.warning(
                self, "LibreOffice не найден",
                "Не удалось найти soffice в PATH или стандартных каталогах.\n"
                "Установите LibreOffice или укажите путь к soffice вручную.",
            )

    def _autodetect_tesseract(self) -> None:
        _, ts = autodetect_external_programs()
        if ts:
            self.tesseract_path.setText(ts)
            QMessageBox.information(self, "Tesseract найден", f"Найден путь:\n{ts}")
        else:
            QMessageBox.warning(
                self, "Tesseract не найден",
                "Не удалось найти tesseract в папке Tesseract-OCR рядом с программой, "
                "в PATH или стандартных каталогах.\n"
                "Установите Tesseract OCR или укажите путь к исполняемому файлу вручную.\n\n"
                "Скачать: https://github.com/tesseract-ocr/tesseract",
            )

    def _autodetect_all(self) -> None:
        lo, ts = autodetect_external_programs()
        found = []
        if lo:
            self.libreoffice_path.setText(lo)
            found.append(f"LibreOffice: {lo}")
        if ts:
            self.tesseract_path.setText(ts)
            found.append(f"Tesseract: {ts}")
        if found:
            QMessageBox.information(
                self, "Автоопределение завершено",
                "Найдено:\n\n" + "\n".join(found),
            )
        else:
            QMessageBox.warning(
                self, "Автоопределение завершено",
                "Ни LibreOffice, ни Tesseract не найдены в системе.\n"
                "Для Tesseract также можно поместить переносимые файлы в папку "
                "Tesseract-OCR рядом с программой.\n"
                "Установите программы или укажите пути вручную.",
            )

    def _accept(self) -> None:
        from pathlib import Path

        libreoffice = self.libreoffice_path.text().strip() or None
        tesseract = self.tesseract_path.text().strip() or None
        for label, value in (("LibreOffice", libreoffice), ("Tesseract", tesseract)):
            if value and not Path(value).is_file():
                QMessageBox.warning(self, "Некорректный путь", f"{label}: файл не найден:\n{value}")
                return

        settings = load_settings()
        settings.libreoffice_path = libreoffice
        settings.tesseract_path = tesseract
        settings.max_workers = self.max_workers.value()
        settings.per_file_timeout = self.per_file_timeout.value()
        settings.max_matches_per_word = self.max_matches.value()
        settings.secure_passes = self.secure_passes.value()
        settings.reset_stats_on_start = self.reset_stats_on_start.isChecked()
        if not save_settings(settings):
            QMessageBox.critical(self, "Ошибка", "Не удалось сохранить настройки на диск.")
            return
        self.accept()
