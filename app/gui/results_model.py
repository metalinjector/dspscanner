"""Табличная модель результатов поиска и делегаты подсветки.

Одинаковые совпадения одного термина в одном файле объединяются в одну строку.
Полный список отдельных вхождений сохраняется внутри модели для экспорта,
вкладки «Файлы» и модального просмотра контекстов.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import Any, Iterable, List

import re

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from app.config import SearchResult

_HEADERS = [
    "Файл",
    "Слово",
    "Кол-во совпадений",
    "Контекст",
    "Тип",
    "Изменён",
    "Путь",
]
_FILENAME_CONTEXT_PREFIX = "[Имя файла]"
CONTENT_MATCH_COLOR = "#6AA1FF"
FILENAME_MATCH_COLOR = "#F2A65A"
_TOOLTIP_RESULT_LIMIT = 12

# Пользовательские роли.
MatchRole = Qt.UserRole + 1
PathRole = Qt.UserRole + 2
FilenameMatchRole = Qt.UserRole + 3
OccurrencesRole = Qt.UserRole + 4


@dataclass
class _ResultGroup:
    """Все вхождения одного поискового термина в одном файле."""

    full_path: str
    word_key: str
    results: list[SearchResult] = field(default_factory=list)

    @property
    def primary(self) -> SearchResult:
        # Для компактной строки предпочтительнее реальный текст документа.
        return next((item for item in self.results if not is_filename_match(item)), self.results[0])

    @property
    def filename_result(self) -> SearchResult | None:
        return next((item for item in self.results if is_filename_match(item)), None)


def is_filename_match(result: SearchResult) -> bool:
    """Возвращает True для результатов поиска только по названию файла."""
    return result.context.startswith(_FILENAME_CONTEXT_PREFIX)


def _html_text(text: str) -> str:
    return escape(str(text), quote=True).replace("\n", "<br>")


def highlight_html(text: str, matched_text: str, color: str) -> str:
    """Экранирует текст и выделяет все совпадения безопасным HTML."""
    if not text or not matched_text:
        return _html_text(text)

    pattern = re.compile(re.escape(matched_text), re.IGNORECASE)
    parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        parts.append(_html_text(text[cursor:match.start()]))
        parts.append(
            f'<span style="color:{color}; font-weight:700;">'
            f'{_html_text(match.group(0))}</span>'
        )
        cursor = match.end()
    parts.append(_html_text(text[cursor:]))
    return "".join(parts)


def result_tooltip_html(result: SearchResult) -> str:
    """Формирует расширенную подсказку для одного вхождения."""
    filename_match = is_filename_match(result)
    color = FILENAME_MATCH_COLOR if filename_match else CONTENT_MATCH_COLOR
    matched = result.matched_text or result.word
    if filename_match:
        fragment = highlight_html(result.file_name, matched, color)
        caption = "Совпадение в названии файла"
    else:
        source = result.tooltip_context or result.context
        fragment = highlight_html(source, matched, color)
        caption = "Расширенный фрагмент текста файла"
    return (
        '<div style="white-space:pre-wrap; min-width:460px; max-width:920px;">'
        f'<span style="color:#9AA3B2; font-weight:600;">{caption}</span><br>'
        f'{fragment}'
        '</div>'
    )


def group_tooltip_html(results: Iterable[SearchResult]) -> str:
    """Показывает первый содержательный контекст объединённой строки."""
    items = list(results)
    if not items:
        return ""
    primary = next((item for item in items if not is_filename_match(item)), items[0])
    html = result_tooltip_html(primary)
    if len(items) <= 1:
        return html
    return html.replace(
        "</div>",
        f'<br><br><span style="color:#9AA3B2;">Всего совпадений: {len(items)}. '
        'Нажмите на число в столбце «Кол-во совпадений», чтобы просмотреть все.</span></div>',
    )


def file_tooltip_html(full_path: str, results: Iterable[SearchResult]) -> str:
    """Формирует подсказку вкладки «Файлы» из реального текста документа."""
    del full_path
    content_results: list[SearchResult] = []
    filename_results: list[SearchResult] = []
    seen: set[tuple[str, str, bool]] = set()
    for result in results:
        filename_match = is_filename_match(result)
        matched = result.matched_text or result.word
        source = result.tooltip_context or result.context
        key = (source, matched.casefold(), filename_match)
        if key in seen:
            continue
        seen.add(key)
        (filename_results if filename_match else content_results).append(result)

    blocks: list[str] = [
        '<div style="white-space:pre-wrap; min-width:480px; max-width:940px;">',
    ]
    shown = 0
    if content_results:
        blocks.append('<span style="color:#9AA3B2; font-weight:600;">Фрагменты текста файла</span>')
        for result in content_results[:_TOOLTIP_RESULT_LIMIT]:
            matched = result.matched_text or result.word
            blocks.extend([
                '<br><br>',
                highlight_html(
                    result.tooltip_context or result.context,
                    matched,
                    CONTENT_MATCH_COLOR,
                ),
            ])
            shown += 1

    remaining_slots = max(0, _TOOLTIP_RESULT_LIMIT - shown)
    if filename_results and remaining_slots:
        if content_results:
            blocks.append('<br><br>')
        blocks.append(
            '<span style="color:#9AA3B2; font-weight:600;">'
            'Совпадения только в названии файла</span>'
        )
        for result in filename_results[:remaining_slots]:
            matched = result.matched_text or result.word
            blocks.extend([
                '<br><br>',
                highlight_html(result.file_name, matched, FILENAME_MATCH_COLOR),
            ])
            shown += 1

    if not content_results and not filename_results:
        blocks.append('<span style="color:#9AA3B2;">Контекст совпадений пока недоступен.</span>')

    remaining = len(content_results) + len(filename_results) - shown
    if remaining > 0:
        blocks.extend([
            '<br><br>',
            f'<span style="color:#9AA3B2;">Ещё фрагментов: {remaining}. '
            'Все контексты доступны по нажатию на число совпадений во вкладке «Результаты».</span>',
        ])
    blocks.append('</div>')
    return "".join(blocks)


class ResultsTableModel(QAbstractTableModel):
    """Агрегированная таблица поверх полного списка отдельных совпадений."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._groups: list[_ResultGroup] = []
        self._group_rows: dict[tuple[str, str], int] = {}
        self._all_results: list[SearchResult] = []

    @staticmethod
    def _key(result: SearchResult) -> tuple[str, str]:
        return result.full_path, result.word

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._groups)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return _HEADERS[section]

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        group = self._groups[index.row()]
        primary = group.primary
        col = index.column()
        if role == Qt.DisplayRole:
            values = [
                primary.file_name,
                primary.word,
                len(group.results),
                primary.context,
                primary.file_type,
                primary.modified,
                primary.full_path,
            ]
            return values[col]
        if role == Qt.ToolTipRole:
            return group_tooltip_html(group.results)
        if role == Qt.TextAlignmentRole and col == 2:
            return int(Qt.AlignCenter)
        if role == Qt.ForegroundRole and col == 2:
            return QColor(CONTENT_MATCH_COLOR)
        if role == Qt.FontRole and col == 2:
            font = QFont()
            font.setBold(True)
            font.setUnderline(True)
            return font
        if role == MatchRole:
            if col == 0 and group.filename_result is not None:
                item = group.filename_result
                return item.matched_text or item.word
            return primary.matched_text or primary.word
        if role == PathRole:
            return primary.full_path
        if role == FilenameMatchRole:
            if col == 0:
                return group.filename_result is not None
            return is_filename_match(primary)
        if role == OccurrencesRole:
            return tuple(group.results)
        return None

    def add_result(self, result: SearchResult) -> None:
        self.add_results([result])

    def add_results(self, results: List[SearchResult]) -> None:
        if not results:
            return

        incoming: dict[tuple[str, str], list[SearchResult]] = {}
        order: list[tuple[str, str]] = []
        for result in results:
            self._all_results.append(result)
            key = self._key(result)
            if key not in incoming:
                incoming[key] = []
                order.append(key)
            incoming[key].append(result)

        changed_rows: set[int] = set()
        new_groups: list[tuple[tuple[str, str], _ResultGroup]] = []
        for key in order:
            row = self._group_rows.get(key)
            if row is None:
                first = incoming[key][0]
                new_groups.append((key, _ResultGroup(first.full_path, key[1], list(incoming[key]))))
            else:
                self._groups[row].results.extend(incoming[key])
                changed_rows.add(row)

        if new_groups:
            start = len(self._groups)
            self.beginInsertRows(QModelIndex(), start, start + len(new_groups) - 1)
            for key, group in new_groups:
                self._group_rows[key] = len(self._groups)
                self._groups.append(group)
            self.endInsertRows()

        for row in sorted(changed_rows):
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, self.columnCount() - 1),
                [Qt.DisplayRole, Qt.ToolTipRole, OccurrencesRole],
            )

    def clear(self) -> None:
        self.beginResetModel()
        self._groups = []
        self._group_rows = {}
        self._all_results = []
        self.endResetModel()

    def result_at(self, row: int) -> SearchResult:
        return self._groups[row].primary

    def group_results_at(self, row: int) -> List[SearchResult]:
        return list(self._groups[row].results)

    def occurrence_count(self) -> int:
        return len(self._all_results)

    def all_results(self) -> List[SearchResult]:
        return list(self._all_results)

    def results_for_path(self, full_path: str) -> List[SearchResult]:
        return [row for row in self._all_results if row.full_path == full_path]

    def unique_paths(self) -> List[str]:
        seen: List[str] = []
        seen_set = set()
        for result in self._all_results:
            if result.full_path not in seen_set:
                seen_set.add(result.full_path)
                seen.append(result.full_path)
        return seen

    def remove_paths(self, paths: set[str]) -> None:
        if not paths:
            return
        remaining = [row for row in self._all_results if row.full_path not in paths]
        if len(remaining) == len(self._all_results):
            return
        self.beginResetModel()
        self._groups = []
        self._group_rows = {}
        self._all_results = []
        for result in remaining:
            key = self._key(result)
            row = self._group_rows.get(key)
            if row is None:
                self._group_rows[key] = len(self._groups)
                self._groups.append(_ResultGroup(result.full_path, key[1], [result]))
            else:
                self._groups[row].results.append(result)
            self._all_results.append(result)
        self.endResetModel()


class HighlightDelegate(QStyledItemDelegate):
    """Подсветка совпадений в колонках «Файл» и «Контекст»."""

    def __init__(
        self,
        content_color: str = CONTENT_MATCH_COLOR,
        filename_color: str = FILENAME_MATCH_COLOR,
        *,
        filename_column: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._content_color = QColor(content_color)
        self._filename_color = QColor(filename_color)
        self._filename_column = filename_column

    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex):
        filename_match = bool(index.data(FilenameMatchRole))
        if self._filename_column and not filename_match:
            super().paint(painter, option, index)
            return

        word = index.data(MatchRole)
        text = index.data(Qt.DisplayRole)
        if not word or not text:
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)

        painter.save()
        painter.setClipRect(option.rect)

        plain_font = QFont(painter.font())
        bold_font = QFont(plain_font)
        bold_font.setBold(True)
        metrics = QFontMetrics(plain_font)
        rect = option.rect.adjusted(6, 0, -6, 0)
        display = metrics.elidedText(str(text), Qt.ElideRight, rect.width())

        selected = bool(option.state & QStyle.State_Selected)
        normal_color = (
            option.palette.highlightedText().color()
            if selected else option.palette.text().color()
        )
        base_match_color = self._filename_color if filename_match else self._content_color
        match_color = option.palette.highlightedText().color() if selected else base_match_color

        pattern = re.compile(re.escape(str(word)), re.IGNORECASE)
        parts = pattern.split(display)
        matches = pattern.findall(display)

        x = rect.x()
        y = rect.y() + metrics.ascent() + (rect.height() - metrics.height()) // 2
        right = rect.right()
        for i, part in enumerate(parts):
            if part and x <= right:
                painter.setFont(plain_font)
                painter.setPen(normal_color)
                painter.drawText(x, y, part)
                x += metrics.horizontalAdvance(part)
            if i < len(matches) and x <= right:
                painter.setFont(bold_font)
                painter.setPen(match_color)
                painter.drawText(x, y, matches[i])
                x += QFontMetrics(bold_font).horizontalAdvance(matches[i])
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex):
        size = super().sizeHint(option, index)
        size.setHeight(max(24, size.height()))
        return size
