"""Модальное окно просмотра всех контекстов объединённого результата."""
from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import SearchResult
from app.gui.results_model import (
    CONTENT_MATCH_COLOR,
    FILENAME_MATCH_COLOR,
    highlight_html,
    is_filename_match,
)


class ResultContextsDialog(QDialog):
    """Показывает каждое вхождение в отдельной прокручиваемой карточке."""

    def __init__(self, results: Sequence[SearchResult], parent=None):
        super().__init__(parent)
        self.results = list(results)
        primary = self.results[0] if self.results else None
        title = "Все контексты совпадений"
        if primary is not None:
            title = f"Совпадения «{primary.word}» — {primary.file_name}"
        self.setWindowTitle(title)
        self.resize(940, 700)
        self.setMinimumSize(700, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        summary = QLabel(
            f"Найдено совпадений: <b>{len(self.results)}</b>. "
            "Фрагменты здесь примерно на 50% шире всплывающей подсказки."
        )
        summary.setWordWrap(True)
        root.addWidget(summary)

        scroll = QScrollArea(self)
        scroll.setObjectName("contextsScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget(scroll)
        cards = QVBoxLayout(content)
        cards.setContentsMargins(4, 4, 10, 4)
        cards.setSpacing(12)

        for number, result in enumerate(self.results, start=1):
            filename_match = is_filename_match(result)
            color = FILENAME_MATCH_COLOR if filename_match else CONTENT_MATCH_COLOR
            source = (
                result.file_name
                if filename_match
                else result.detail_context or result.tooltip_context or result.context
            )
            matched = result.matched_text or result.word
            source_label = "название файла" if filename_match else "содержимое файла"

            card = QFrame(content)
            card.setObjectName("contextCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 12, 14, 12)
            card_layout.setSpacing(8)

            caption = QLabel(f"Совпадение {number} — {source_label}")
            caption.setObjectName("contextCardCaption")
            card_layout.addWidget(caption)

            fragment = QLabel()
            fragment.setTextFormat(Qt.RichText)
            fragment.setWordWrap(True)
            fragment.setTextInteractionFlags(Qt.TextSelectableByMouse)
            fragment.setText(
                '<div style="line-height:1.35;">'
                + highlight_html(source, matched, color)
                + "</div>"
            )
            card_layout.addWidget(fragment)
            cards.addWidget(card)

        cards.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_button = buttons.button(QDialogButtonBox.Close)
        if close_button is not None:
            close_button.setText("Закрыть")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)
