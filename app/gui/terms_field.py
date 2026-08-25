"""Поле ключевых слов в виде кнопки со списком-модалкой.

Раньше термины вводились в два многострочных QTextEdit. Они занимали
заметную часть левой панели, но большую часть времени показывали
неотредактированный текст, а при длинном списке всё равно прокручивались.
Здесь то же самое хранится за кнопкой: на ней видно количество терминов,
редактирование — в модальном окне со списком, наведение показывает полный
перечень.

`TermsField` намеренно повторяет тот минимум API QTextEdit, которым
пользовалось главное окно (`toPlainText`, `setPlainText`, `textChanged`):
сохранение настроек, автосохранение и запуск поиска продолжают работать с
полем как с текстом, не зная про новое представление.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.scanning.term_parser import parse_terms

#: Пауза перед началом прокрутки подсказки и после её конца, мс.
_SCROLL_IDLE_MS = 900
#: Шаг автопрокрутки: 1 px каждые 30 мс — читаемо и не дёргается.
_SCROLL_STEP_MS = 30
_SCROLL_STEP_PX = 1
#: Дальше подсказка не растёт: длинный список всё равно прокручивается.
_TOOLTIP_MAX_HEIGHT = 260
_TOOLTIP_MIN_WIDTH = 260
#: Цвета разлиновки пустой части списка — те же, что у рамок и подписей темы.
_RULE_COLOR = "#3A4150"
_HINT_COLOR = "#6B7385"


class TermsTooltip(QFrame):
    """Всплывающий перечень терминов с автопрокруткой вверх.

    Штатный QToolTip не прокручивается и обрезает длинные списки, поэтому
    подсказка собрана из QScrollArea: пока курсор над кнопкой, содержимое
    равномерно уезжает вверх, дойдя до конца — возвращается к началу.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        # ToolTip-окно не забирает фокус и не появляется в списке окон.
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setObjectName("termsTooltip")
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self._caption = QLabel()
        self._caption.setObjectName("termsTooltipCaption")
        layout.addWidget(self._caption)

        self._area = QScrollArea(self)
        self._area.setObjectName("termsTooltipArea")
        self._area.setWidgetResizable(True)
        self._area.setFrameShape(QFrame.NoFrame)
        self._area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body = QLabel()
        self._body.setObjectName("termsTooltipBody")
        self._body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._body.setTextFormat(Qt.PlainText)
        self._area.setWidget(self._body)
        layout.addWidget(self._area)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._scroll_once)

    def set_terms(self, caption: str, terms: list[str]) -> None:
        self._caption.setText(caption)
        if terms:
            # Нумерация помогает понять, что список прокручивается, а не
            # повторяется: видно, где начало и где конец.
            self._body.setText(
                "\n".join(f"{i}. {term}" for i, term in enumerate(terms, start=1))
            )
        else:
            self._body.setText("Список пуст — нажмите кнопку, чтобы добавить.")
        self._body.adjustSize()
        self.adjustSize()

        width = max(_TOOLTIP_MIN_WIDTH, self.sizeHint().width())
        height = min(_TOOLTIP_MAX_HEIGHT, self.sizeHint().height())
        self.resize(width, height)

    def start(self) -> None:
        """Показывает подсказку и запускает прокрутку после паузы."""
        self._area.verticalScrollBar().setValue(0)
        self.show()
        if self._scrollable():
            self._timer.start(_SCROLL_IDLE_MS)

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _scrollable(self) -> bool:
        return self._area.verticalScrollBar().maximum() > 0

    def _scroll_once(self) -> None:
        bar = self._area.verticalScrollBar()
        if bar.value() >= bar.maximum():
            # Дойдя до конца, список замирает и начинается заново — иначе
            # непонятно, кончился он или просто дёрнулся.
            bar.setValue(0)
            self._timer.start(_SCROLL_IDLE_MS)
            return
        bar.setValue(bar.value() + _SCROLL_STEP_PX)
        self._timer.start(_SCROLL_STEP_MS)


class RuledList(QListWidget):
    """Список-«тетрадный лист»: линии видны и там, где значений ещё нет.

    Разлиновку строк даёт QSS, но у пустого списка строк нет — оставалась
    голая рамка, по которой непонятно, что сюда добавляют по одному значению
    в строку. Свободная часть дорисовывается теми же линиями.
    """

    #: Совпадает с min-height строки в QSS: линии продолжают сетку списка.
    ROW_HEIGHT = 30

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._empty_hint = ""

    def set_empty_hint(self, text: str) -> None:
        self._empty_hint = text
        self.viewport().update()

    def paintEvent(self, event):  # noqa: N802 - имя задано Qt
        super().paintEvent(event)

        viewport = self.viewport()
        used = self.count() * self.ROW_HEIGHT - self.verticalScrollBar().value()
        top = max(0, used)
        if top >= viewport.height():
            return

        painter = QPainter(viewport)
        painter.setPen(QPen(QColor(_RULE_COLOR), 1))
        y = top + self.ROW_HEIGHT
        while y < viewport.height():
            painter.drawLine(0, y, viewport.width(), y)
            y += self.ROW_HEIGHT

        if not self.count() and self._empty_hint:
            painter.setPen(QColor(_HINT_COLOR))
            # Ровно в первую строку сетки: без переноса подсказка не
            # пересекает разлиновку и читается как пример записи.
            rect = QRect(10, 0, max(0, viewport.width() - 20), self.ROW_HEIGHT)
            painter.drawText(
                rect,
                int(Qt.AlignLeft | Qt.AlignVCenter),
                painter.fontMetrics().elidedText(
                    self._empty_hint, Qt.ElideRight, rect.width()
                ),
            )
        painter.end()


class TermsDialog(QDialog):
    """Список ключевых слов: одна строка — один термин.

    Удаление через галочки, а не через выделение: выделение в списке легко
    сбить случайным кликом, а отметки переживают прокрутку и видны глазом
    до нажатия «Удалить».
    """

    def __init__(self, title: str, terms: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName("termsDialog")
        self.setModal(True)
        self.resize(430, 460)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        hint = QLabel(
            "По одному значению в строке. Можно вставить сразу несколько — "
            "через запятую, точку с запятой или с новой строки."
        )
        hint.setObjectName("termsDialogHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("новое слово или словосочетание")
        # Enter — самый быстрый способ добавить несколько терминов подряд.
        self.input.returnPressed.connect(self._add_from_input)
        self.add_btn = QPushButton("Добавить")
        self.add_btn.setObjectName("primaryButton")
        self.add_btn.clicked.connect(self._add_from_input)
        add_row.addWidget(self.input, 1)
        add_row.addWidget(self.add_btn)
        root.addLayout(add_row)

        self.list = RuledList()
        self.list.setObjectName("termsList")
        self.list.set_empty_hint(
            "Пусто. Введите значение выше и нажмите «Добавить»."
        )
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setAlternatingRowColors(False)
        self.list.setUniformItemSizes(True)
        self.list.itemChanged.connect(lambda _item: self._update_controls())
        root.addWidget(self.list, 1)

        for term in terms:
            self._append_item(term)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.check_all_btn = QPushButton("Отметить все")
        self.check_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        self.uncheck_all_btn = QPushButton("Снять все")
        self.uncheck_all_btn.clicked.connect(lambda: self._set_all_checked(False))
        self.remove_btn = QPushButton("Удалить отмеченные")
        self.remove_btn.setObjectName("dangerButton")
        self.remove_btn.clicked.connect(self._remove_checked)
        controls.addWidget(self.check_all_btn)
        controls.addWidget(self.uncheck_all_btn)
        controls.addStretch(1)
        controls.addWidget(self.remove_btn)
        root.addLayout(controls)

        self.summary = QLabel()
        self.summary.setObjectName("termsDialogSummary")
        root.addWidget(self.summary)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._update_controls()
        self.input.setFocus()

    # -- содержимое ---------------------------------------------------- #

    def _append_item(self, term: str) -> QListWidgetItem:
        item = QListWidgetItem(term)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Unchecked)
        self.list.addItem(item)
        return item

    def terms(self) -> list[str]:
        return [self.list.item(row).text() for row in range(self.list.count())]

    def _existing_keys(self) -> set[str]:
        # Ключ дедупликации совпадает с parse_terms: регистр и «ё» не
        # различаются, иначе один и тот же термин попадёт в список дважды.
        return {term.casefold().replace("ё", "е") for term in self.terms()}

    def _add_from_input(self) -> None:
        raw = self.input.text()
        added = 0
        keys = self._existing_keys()
        for term in parse_terms(raw):
            key = term.casefold().replace("ё", "е")
            if key in keys:
                continue
            keys.add(key)
            self._append_item(term)
            added += 1
        self.input.clear()
        if added:
            self.list.scrollToBottom()
        self._update_controls(added=added, ignored=bool(raw.strip()) and not added)

    def _checked_rows(self) -> list[int]:
        return [
            row
            for row in range(self.list.count())
            if self.list.item(row).checkState() == Qt.Checked
        ]

    def _remove_checked(self) -> None:
        for row in reversed(self._checked_rows()):
            self.list.takeItem(row)
        self._update_controls()

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.list.count()):
            self.list.item(row).setCheckState(state)
        self._update_controls()

    def _update_controls(self, added: int = 0, ignored: bool = False) -> None:
        total = self.list.count()
        checked = len(self._checked_rows())
        self.remove_btn.setEnabled(checked > 0)
        self.check_all_btn.setEnabled(total > 0)
        self.uncheck_all_btn.setEnabled(checked > 0)

        if ignored:
            self.summary.setText("Такое значение уже есть в списке.")
        elif added:
            self.summary.setText(f"Добавлено значений: {added}. Всего: {total}.")
        elif checked:
            self.summary.setText(f"Всего: {total}. Отмечено к удалению: {checked}.")
        else:
            self.summary.setText(f"Всего значений: {total}.")


class TermsField(QPushButton):
    """Кнопка-хранилище списка терминов.

    Наружу выглядит как QTextEdit (`toPlainText`/`setPlainText`/`textChanged`),
    чтобы сохранение настроек и запуск поиска не зависели от того, чем
    редактируется список.
    """

    textChanged = Signal()

    def __init__(
        self,
        title: str,
        dialog_title: str,
        empty_hint: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("termsButton")
        self._title = title
        self._dialog_title = dialog_title
        self._empty_hint = empty_hint
        self._terms: list[str] = []
        self._tooltip: TermsTooltip | None = None

        self.setMinimumHeight(56)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(self._open_dialog)
        self._refresh_text()

    # -- совместимость с QTextEdit -------------------------------------- #

    def toPlainText(self) -> str:
        return "\n".join(self._terms)

    def setPlainText(self, text: str) -> None:
        self.set_terms(parse_terms(text))

    # -- список --------------------------------------------------------- #

    def terms(self) -> list[str]:
        return list(self._terms)

    def set_terms(self, terms: list[str]) -> None:
        new_terms = list(terms)
        if new_terms == self._terms:
            return
        self._terms = new_terms
        self._refresh_text()
        self.textChanged.emit()

    def _refresh_text(self) -> None:
        count = len(self._terms)
        if count:
            self.setText(f"{self._title}   ·   {count}\n{self._preview()}")
        else:
            self.setText(f"{self._title}\n{self._empty_hint}")
        # Штатная подсказка не нужна: перечень показывает своя, с прокруткой.
        self.setToolTip("")

    def _preview(self) -> str:
        """Первые термины, помещающиеся в ширину кнопки.

        Обрезка по фиксированному числу элементов рвала строку посреди слова
        («договор» превращался в «дог»), поэтому список набирается по целым
        терминам, пока хватает места, а остаток сворачивается в счётчик.
        """
        metrics = self.fontMetrics()
        # Поля кнопки плюс запас под «… и ещё N».
        available = max(80, self.width() - 24)
        shown: list[str] = []
        for term in self._terms:
            candidate = ", ".join([*shown, term])
            rest = len(self._terms) - len(shown) - 1
            suffix = f" и ещё {rest}" if rest > 0 else ""
            if shown and metrics.horizontalAdvance(candidate + suffix) > available:
                break
            shown.append(term)

        rest = len(self._terms) - len(shown)
        preview = ", ".join(shown)
        if rest > 0:
            preview += f" и ещё {rest}"
        # Единственный термин шире кнопки — многоточие вместо разрыва по букве.
        return metrics.elidedText(preview, Qt.ElideRight, available)

    def resizeEvent(self, event):  # noqa: N802 - имя задано Qt
        # Ширина кнопки зависит от размера окна: превью пересобирается,
        # иначе после растягивания панели строка остаётся коротким огрызком.
        super().resizeEvent(event)
        if self._terms:
            self.setText(f"{self._title}   ·   {len(self._terms)}\n{self._preview()}")

    def _open_dialog(self) -> None:
        self._hide_tooltip()
        dialog = TermsDialog(self._dialog_title, self._terms, self)
        if dialog.exec() == QDialog.Accepted:
            self.set_terms(dialog.terms())

    # -- подсказка с автопрокруткой -------------------------------------- #

    def enterEvent(self, event):  # noqa: N802 - имя задано Qt
        self._show_tooltip()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - имя задано Qt
        self._hide_tooltip()
        super().leaveEvent(event)

    def hideEvent(self, event):  # noqa: N802 - имя задано Qt
        # Иначе подсказка переживёт скрытие окна и повиснет поверх экрана.
        self._hide_tooltip()
        super().hideEvent(event)

    def _show_tooltip(self) -> None:
        if self._tooltip is None:
            self._tooltip = TermsTooltip(self)
        caption = f"{self._title}: {len(self._terms)}" if self._terms else self._title
        self._tooltip.set_terms(caption, self._terms)
        # Под кнопкой по левому краю: так подсказка не накрывает саму кнопку
        # и не уезжает за границу узкой левой панели.
        self._tooltip.move(self.mapToGlobal(self.rect().bottomLeft()))
        self._tooltip.start()

    def _hide_tooltip(self) -> None:
        if self._tooltip is not None:
            self._tooltip.stop()
