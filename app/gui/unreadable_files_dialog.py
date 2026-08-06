"""Модальное окно управления файлами, которые не удалось прочитать."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.config import ErrorEntry


class UnreadableFilesDialog(QDialog):
    """Показывает окончательные ошибки чтения и возвращает выбранную операцию."""

    ACTION_COPY = "copy"
    ACTION_MOVE = "move"
    ACTION_DELETE = "delete"

    def __init__(self, entries: Sequence[ErrorEntry], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Файлы, которые не удалось прочитать")
        self.resize(980, 560)
        self.setMinimumSize(760, 420)
        self.entries = list(entries)
        self.selected_action: str | None = None
        self.destination = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        description = QLabel(
            f"Ни один доступный способ чтения не смог обработать файлов: "
            f"<b>{len(self.entries)}</b>. Проверьте список перед разрушительной операцией."
        )
        description.setWordWrap(True)
        root.addWidget(description)

        self.table = QTableWidget(len(self.entries), 3, self)
        self.table.setHorizontalHeaderLabels(["Файл", "Причина", "Полный путь"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.resizeSection(0, 210)
        header.resizeSection(1, 390)

        for row, entry in enumerate(self.entries):
            path = Path(entry.file_path)
            values = (path.name or entry.file_path, entry.message, entry.file_path)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                if column == 0:
                    item.setData(Qt.UserRole, entry.file_path)
                self.table.setItem(row, column, item)
        root.addWidget(self.table, 1)

        note = QLabel(
            "«Переместить» создаёт и проверяет копию, после чего безопасно "
            "перезаписывает и удаляет оригинал. «Удалить» необратимо выполняет "
            "безопасное многопроходное удаление."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#F2B84B;")
        root.addWidget(note)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 6, 0, 3)
        buttons.setSpacing(8)
        copy_button = QPushButton("Скопировать все в…")
        move_button = QPushButton("Безопасно переместить все в…")
        delete_button = QPushButton("Безопасно удалить все")
        delete_button.setObjectName("dangerButton")
        close_button = QPushButton("Закрыть")

        copy_button.clicked.connect(lambda: self._choose_destination(self.ACTION_COPY))
        move_button.clicked.connect(lambda: self._choose_destination(self.ACTION_MOVE))
        delete_button.clicked.connect(self._choose_delete)
        close_button.clicked.connect(self.reject)

        buttons.addWidget(copy_button)
        buttons.addWidget(move_button)
        buttons.addWidget(delete_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        root.addLayout(buttons)

    @property
    def paths(self) -> list[str]:
        """Возвращает уникальные существующие обычные файлы из списка."""
        result: list[str] = []
        seen: set[str] = set()
        for entry in self.entries:
            path_text = entry.file_path
            if not path_text or path_text in seen:
                continue
            seen.add(path_text)
            try:
                path = Path(path_text)
                if path.is_file() and not path.is_symlink():
                    result.append(path_text)
            except OSError:
                continue
        return result

    def _choose_destination(self, action: str) -> None:
        paths = self.paths
        if not paths:
            QMessageBox.warning(
                self,
                "Файлы недоступны",
                "В списке больше нет существующих обычных файлов, доступных для операции.",
            )
            return
        title = "Куда скопировать файлы" if action == self.ACTION_COPY else "Куда переместить файлы"
        destination = QFileDialog.getExistingDirectory(self, title)
        if not destination:
            return
        self.selected_action = action
        self.destination = destination
        self.accept()

    def _choose_delete(self) -> None:
        paths = self.paths
        if not paths:
            QMessageBox.warning(
                self,
                "Файлы недоступны",
                "В списке больше нет существующих обычных файлов, доступных для удаления.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Необратимое удаление",
            f"Безопасно удалить все доступные файлы из списка ({len(paths)})?\n\n"
            "Оригиналы будут многократно перезаписаны и удалены. Действие необратимо.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.selected_action = self.ACTION_DELETE
        self.accept()
