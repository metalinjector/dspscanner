"""Табличная модель результатов поиска и делегаты подсветки.

Одинаковые совпадения одного термина в одном файле объединяются в одну строку.
Полный список отдельных вхождений сохраняется внутри модели для экспорта,
вкладки «Файлы» и модального просмотра контекстов.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any, Iterable, List

import re

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from app.config import SearchResult

_HEADERS = [
    "✓",
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
# Разделитель между именем файла и уточняющей папкой. Имя идёт первым, чтобы
# сортировка по столбцу «Файл» оставалась сортировкой по имени, а копии с
# одинаковым именем оказывались рядом.
_NAME_FOLDER_SEPARATOR = " › "
# Глубже обычно не требуется: если различие лежит выше, показываем весь путь.
_MAX_FOLDER_PARTS = 4

# Пользовательские роли.
MatchRole = Qt.UserRole + 1
PathRole = Qt.UserRole + 2
FilenameMatchRole = Qt.UserRole + 3
OccurrencesRole = Qt.UserRole + 4
# Полный путь независимо от колонки (нужен делегату чекбокса).
FileIdRole = Qt.UserRole + 5
# Индекс столбца чекбоксов.
_CHECK_COLUMN = 0


class CheckboxDelegate(QStyledItemDelegate):
    """Центрированный чекбокс для колонки ✓.

    Индикатор рисуется вручную: стандартный QTableView рисует его только у
    редактируемых ячеек. Клик внутри колонки переключает состояние через модель.

    Рисование целиком своё, без QStyle. Причины две:

    * ``CE_ItemViewItem`` сам рисует индикатор, если модель отдаёт
      ``CheckStateRole`` (``initStyleOption`` выставляет ``HasCheckIndicator``).
      Вместе с нашим он давал два наложенных чекбокса — «двоение» при клике,
      когда индикатор стиля перерисовывался в нажатом состоянии.
    * Индикатор системного стиля прижимался к левому краю ячейки и при узкой
      колонке ✓ обрезался пополам.

    Собственная отрисовка убирает обе проблемы и заодно приводит галочку в
    таблице к тому же виду, что у обычных QCheckBox из темы.
    """

    #: Сторона квадрата индикатора. Умещается в колонку ✓ и в строку 24 px.
    _BOX_SIDE = 16
    _RADIUS = 4.0

    _BORDER = QColor("#7A8394")
    _BORDER_HOVER = QColor("#9BA5B7")
    _BG = QColor("#11151C")
    _BG_HOVER = QColor("#171C24")
    _ACCENT = QColor("#4C8DFF")
    _ACCENT_HOVER = QColor("#6AA1FF")
    _CHECK = QColor("#0A0C10")

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()

        # Фон, выделение и рамку строки рисует стиль, но без текста и без
        # своего индикатора — иначе он наложится на нарисованный ниже.
        opt.text = ""
        opt.features &= ~QStyleOptionViewItem.HasCheckIndicator
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)

        checked = index.data(Qt.CheckStateRole) == Qt.Checked
        hovered = bool(opt.state & QStyle.State_MouseOver)

        side = self._BOX_SIDE
        rect = QRect(0, 0, side, side)
        rect.moveCenter(opt.rect.center())
        # Полупиксельный сдвиг: рамка шириной 1 px ложится ровно на пиксель,
        # иначе сглаживание размывает её в две полупрозрачные линии.
        box = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if checked:
            fill = self._ACCENT_HOVER if hovered else self._ACCENT
            border = fill
        else:
            fill = self._BG_HOVER if hovered else self._BG
            border = self._BORDER_HOVER if hovered else self._BORDER
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(box, self._RADIUS, self._RADIUS)

        if checked:
            path = QPainterPath()
            # Галочка в долях стороны — масштабируется вместе с _BOX_SIDE.
            path.moveTo(box.left() + side * 0.24, box.top() + side * 0.52)
            path.lineTo(box.left() + side * 0.43, box.top() + side * 0.71)
            path.lineTo(box.left() + side * 0.77, box.top() + side * 0.30)
            pen = QPen(self._CHECK, 2.0)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        from PySide6.QtCore import QEvent

        # Переключает только левая кнопка: правая открывает контекстное меню
        # операций над отмеченными, и менять отметку под курсором при этом
        # нельзя — пользователь целился в меню, а не в галочку.
        if (
            event.type() == QEvent.MouseButtonRelease
            and event.button() == Qt.LeftButton
        ):
            current = index.data(Qt.CheckStateRole)
            new_state = Qt.Unchecked if current == Qt.Checked else Qt.Checked
            model.setData(index, new_state, Qt.CheckStateRole)
            return True
        return False


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


def _parent_tail(parent: Path, depth: int) -> str:
    """Последние ``depth`` частей родительского пути."""
    parts = parent.parts
    if depth >= len(parts):
        return str(parent)
    return os.sep.join(parts[-depth:])


def disambiguating_labels(file_name: str, paths: Iterable[str]) -> dict[str, str]:
    """Подписи столбца «Файл» для одного имени файла.

    Пока имя встречается в одном каталоге, подпись — само имя: обычный случай
    ничего не теряет. Как только то же имя найдено во втором каталоге, строки
    становятся неразличимы на вид, поэтому к имени добавляется минимальный
    хвост родительского пути: сначала одна папка, при совпадении — две и так
    далее. Если различие лежит выше ``_MAX_FOLDER_PARTS``, показывается весь
    родительский путь.
    """
    unique = sorted(set(paths))
    if len(unique) <= 1:
        return {path: file_name for path in unique}

    parents = {path: Path(path).parent for path in unique}
    tails: dict[str, str] = {}
    for depth in range(1, _MAX_FOLDER_PARTS + 1):
        tails = {path: _parent_tail(parent, depth) for path, parent in parents.items()}
        if len(set(tails.values())) == len(unique):
            break
    else:
        tails = {path: str(parent) for path, parent in parents.items()}

    return {
        path: f"{file_name}{_NAME_FOLDER_SEPARATOR}{tail}"
        for path, tail in tails.items()
    }


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
        # Отмеченные галочкой строки по (путь, слово).
        self._checked_keys: set[tuple[str, str]] = set()
        # Индексы для уточнения одинаковых имён файлов. Поддерживаются на
        # лету: имя может стать неоднозначным уже после того, как его строки
        # показаны, и тогда подписи нужно обновить, а не пересобирать таблицу.
        self._paths_by_name: dict[str, set[str]] = {}
        self._rows_by_name: dict[str, list[int]] = {}
        self._label_by_path: dict[str, str] = {}
        # Самый длинный контекст нужен для подгонки ширины столбца. Копить его
        # по мере поступления дешевле, чем измерять все строки при каждой
        # перекомпоновке заголовка.
        self._longest_context = ""

    def longest_context(self) -> str:
        """Самая длинная строка столбца «Контекст» среди загруженных."""
        return self._longest_context

    def _note_context(self, result: SearchResult) -> None:
        if len(result.context) > len(self._longest_context):
            self._longest_context = result.context

    @staticmethod
    def _key(result: SearchResult) -> tuple[str, str]:
        return result.full_path, result.word

    def _register_row(self, row: int, result: SearchResult) -> str:
        """Привязывает строку к имени файла и возвращает это имя."""
        name = result.file_name
        self._paths_by_name.setdefault(name, set()).add(result.full_path)
        self._rows_by_name.setdefault(name, []).append(row)
        return name

    def _refresh_labels(self, names: Iterable[str]) -> set[str]:
        """Пересчитывает подписи и возвращает имена, у которых они изменились."""
        changed: set[str] = set()
        for name in names:
            labels = disambiguating_labels(name, self._paths_by_name.get(name, ()))
            for path, label in labels.items():
                if self._label_by_path.get(path) != label:
                    self._label_by_path[path] = label
                    changed.add(name)
        return changed

    def display_name(self, full_path: str, fallback: str) -> str:
        return self._label_by_path.get(full_path, fallback)

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
                "",
                self.display_name(primary.full_path, primary.file_name),
                primary.word,
                len(group.results),
                primary.context,
                primary.file_type,
                primary.modified,
                primary.full_path,
            ]
            return values[col]
        if role == Qt.CheckStateRole and col == _CHECK_COLUMN:
            key = self._key(primary)
            return Qt.Checked if key in self._checked_keys else Qt.Unchecked
        if role == Qt.ToolTipRole:
            return group_tooltip_html(group.results)
        if role == Qt.TextAlignmentRole and col == 3:
            return int(Qt.AlignCenter)
        if role == Qt.ForegroundRole and col == 3:
            return QColor(CONTENT_MATCH_COLOR)
        if role == Qt.FontRole and col == 3:
            font = QFont()
            font.setBold(True)
            font.setUnderline(True)
            return font
        if role == MatchRole:
            if col == 1 and group.filename_result is not None:
                item = group.filename_result
                return item.matched_text or item.word
            return primary.matched_text or primary.word
        if role == PathRole:
            return primary.full_path
        if role == FileIdRole:
            return primary.full_path
        if role == FilenameMatchRole:
            if col == 1:
                return group.filename_result is not None
            return is_filename_match(primary)
        if role == OccurrencesRole:
            return tuple(group.results)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        flags = super().flags(index)
        if index.isValid() and index.column() == _CHECK_COLUMN:
            flags |= Qt.ItemIsUserCheckable
            flags &= ~Qt.ItemIsEditable
        return flags

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:
        if not index.isValid() or index.column() != _CHECK_COLUMN:
            return False
        if role != Qt.CheckStateRole:
            return False
        group = self._groups[index.row()]
        key = self._key(group.primary)
        if value == Qt.Checked:
            changed = key not in self._checked_keys
            self._checked_keys.add(key)
        else:
            changed = key in self._checked_keys
            self._checked_keys.discard(key)
        if changed:
            self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        return True

    def set_all_checked(self, checked: bool) -> None:
        """Проставляет/снимает галочки на всех строках одним сигналом."""
        if not self._groups:
            return
        if checked:
            self._checked_keys = set(self._group_rows)
        else:
            if not self._checked_keys:
                return
            self._checked_keys.clear()
        self.dataChanged.emit(
            self.index(0, _CHECK_COLUMN),
            self.index(self.rowCount() - 1, _CHECK_COLUMN),
            [Qt.CheckStateRole],
        )

    def checked_paths(self) -> list[str]:
        """Уникальные полные пути, отмеченные галочками."""
        seen: list[str] = []
        seen_set: set[str] = set()
        for key in self._checked_keys:
            path = key[0]
            if path not in seen_set and key in self._group_rows:
                seen_set.add(path)
                seen.append(path)
        return seen

    def has_checked(self) -> bool:
        return bool(
            set(key[0] for key in self._checked_keys)
            & set(key[0] for key in self._group_rows)
        )

    def checked_count(self) -> int:
        """Число отмеченных файлов (не строк): операции работают по файлам."""
        return len(self.checked_paths())

    def set_checked_paths(self, paths: Iterable[str]) -> None:
        """Отмечает все строки перечисленных файлов, остальные снимает.

        Нужно при смене группировки: пользователь отметил файлы в одном
        режиме и вправе увидеть их отмеченными в другом.
        """
        wanted = set(paths)
        fresh = {key for key in self._group_rows if key[0] in wanted}
        if fresh == self._checked_keys:
            return
        self._checked_keys = fresh
        if self._groups:
            self.dataChanged.emit(
                self.index(0, _CHECK_COLUMN),
                self.index(self.rowCount() - 1, _CHECK_COLUMN),
                [Qt.CheckStateRole],
            )

    def remove_checked(self) -> set[str]:
        """Сбрасывает модель без отмеченных строк; возвращает их пути."""
        checked_keys = set(self._checked_keys)
        if not checked_keys:
            return set()
        removed_paths = {key[0] for key in checked_keys}
        remaining = [
            row
            for row in self._all_results
            if (row.full_path, row.word) not in checked_keys
        ]
        self.beginResetModel()
        self._reset_state()
        for result in remaining:
            key = self._key(result)
            row = self._group_rows.get(key)
            if row is None:
                row = len(self._groups)
                self._group_rows[key] = row
                self._groups.append(_ResultGroup(result.full_path, key[1], [result]))
                self._register_row(row, result)
            else:
                self._groups[row].results.append(result)
            self._all_results.append(result)
            self._note_context(result)
        self._refresh_labels(self._paths_by_name)
        self.endResetModel()
        return removed_paths

    def add_result(self, result: SearchResult) -> None:
        self.add_results([result])

    def add_results(self, results: List[SearchResult]) -> None:
        if not results:
            return

        incoming: dict[tuple[str, str], list[SearchResult]] = {}
        order: list[tuple[str, str]] = []
        for result in results:
            self._all_results.append(result)
            self._note_context(result)
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

        touched_names: set[str] = set()
        if new_groups:
            start = len(self._groups)
            self.beginInsertRows(QModelIndex(), start, start + len(new_groups) - 1)
            for key, group in new_groups:
                row = len(self._groups)
                self._group_rows[key] = row
                self._groups.append(group)
                touched_names.add(self._register_row(row, group.results[0]))
            self.endInsertRows()

        # Второй каталог для уже показанного имени делает прежние строки
        # неразличимыми, поэтому подписи обновляются и у ранее вставленных.
        for name in self._refresh_labels(touched_names):
            for row in self._rows_by_name.get(name, ()):
                changed_rows.add(row)

        for row in sorted(changed_rows):
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, self.columnCount() - 1),
                [Qt.DisplayRole, Qt.ToolTipRole, OccurrencesRole],
            )

    def clear(self) -> None:
        self.beginResetModel()
        self._reset_state()
        self.endResetModel()

    def _reset_state(self) -> None:
        self._groups = []
        self._group_rows = {}
        self._all_results = []
        self._paths_by_name = {}
        self._rows_by_name = {}
        self._label_by_path = {}
        self._longest_context = ""
        self._checked_keys = set()

    def result_at(self, row: int) -> SearchResult:
        return self._groups[row].primary

    def group_results_at(self, row: int) -> List[SearchResult]:
        return list(self._groups[row].results)

    def occurrence_count(self) -> int:
        return len(self._all_results)

    def count(self) -> int:
        """Число строк — симметрично FilesTableModel.count()."""
        return len(self._groups)

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
        self._reset_state()
        for result in remaining:
            key = self._key(result)
            row = self._group_rows.get(key)
            if row is None:
                row = len(self._groups)
                self._group_rows[key] = row
                self._groups.append(_ResultGroup(result.full_path, key[1], [result]))
                self._register_row(row, result)
            else:
                self._groups[row].results.append(result)
            self._all_results.append(result)
            self._note_context(result)
        # Удаление файла может вернуть имени однозначность: уточнение папкой
        # больше не нужно, и подпись снова становится просто именем.
        self._refresh_labels(self._paths_by_name)
        self.endResetModel()


@dataclass
class _FileRow:
    """Одна строка вкладки «Файлы»: файл + агрегированные совпадения."""

    full_path: str
    results: list[SearchResult] = field(default_factory=list)

    @property
    def primary(self) -> SearchResult:
        return next(
            (item for item in self.results if not is_filename_match(item)),
            self.results[0],
        )

    @property
    def filename_result(self) -> SearchResult | None:
        return next((item for item in self.results if is_filename_match(item)), None)

    @property
    def occurrence_count(self) -> int:
        return len(self.results)

    @property
    def words(self) -> list[str]:
        """Уникальные термины файла в порядке первого появления."""
        seen: set[str] = set()
        ordered: list[str] = []
        for result in self.results:
            key = result.word.casefold()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(result.word)
        return ordered

    @property
    def words_text(self) -> str:
        return WORDS_SEPARATOR.join(self.words)


# Раскладка совпадает с «Результатами» по номерам колонок: обе вкладки
# используют одни и те же делегаты подсветки и индексы в главном окне.
_FILE_HEADERS = [
    "✓",
    "Файл",
    "Слово",
    "Кол-во совпадений",
    "Контекст",
    "Тип",
    "Изменён",
    "Путь",
]
# Разделитель списка терминов в колонке «Слово» вкладки «Файлы».
WORDS_SEPARATOR = ", "
# Индекс колонки с числом совпадений (кликабельной) во вкладке «Файлы».
_FILE_OCCURRENCES_COLUMN = 3


class FilesTableModel(QAbstractTableModel):
    """Подробная таблица файлов, как в разделе «Результаты».

    Одна строка на уникальный путь; наполняется синхронно с ResultsTableModel.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[_FileRow] = []
        self._row_by_path: dict[str, int] = {}
        self._checked_paths: set[str] = set()
        self._labels_dict: dict[str, str] = {}
        # Как и в «Результатах», самый длинный контекст копится по мере
        # поступления строк: ширина колонки подгоняется по нему без обхода
        # всей модели.
        self._longest_context = ""

    def longest_context(self) -> str:
        """Самая длинная строка столбца «Контекст» среди загруженных."""
        return self._longest_context

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return len(_FILE_HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return _FILE_HEADERS[section]

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        primary = row.primary
        col = index.column()
        if role == Qt.DisplayRole:
            values = [
                "",
                self._labels_dict.get(primary.full_path, Path(primary.full_path).name),
                row.words_text,
                row.occurrence_count,
                primary.context,
                primary.file_type,
                primary.modified,
                primary.full_path,
            ]
            return values[col]
        if role == Qt.CheckStateRole and col == _CHECK_COLUMN:
            return (
                Qt.Checked if row.full_path in self._checked_paths else Qt.Unchecked
            )
        if role == Qt.ToolTipRole:
            return file_tooltip_html(row.full_path, row.results)
        if role == Qt.TextAlignmentRole and col == _FILE_OCCURRENCES_COLUMN:
            return int(Qt.AlignCenter)
        if role == Qt.ForegroundRole and col == _FILE_OCCURRENCES_COLUMN:
            return QColor(CONTENT_MATCH_COLOR)
        if role == Qt.FontRole and col == _FILE_OCCURRENCES_COLUMN:
            font = QFont()
            font.setBold(True)
            font.setUnderline(True)
            return font
        if role == MatchRole:
            # Колонка «Файл» подсвечивается совпадением в имени, «Контекст» —
            # совпадением внутри текста, ровно как во вкладке «Результаты».
            if col == 1:
                filename_result = row.filename_result
                if filename_result is not None:
                    return filename_result.matched_text or filename_result.word
                return None
            return primary.matched_text or primary.word
        if role == FilenameMatchRole:
            if col == 1:
                return row.filename_result is not None
            return is_filename_match(primary)
        if role in (PathRole, FileIdRole):
            return row.full_path
        if role == OccurrencesRole:
            return tuple(row.results)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        flags = super().flags(index)
        if index.isValid() and index.column() == _CHECK_COLUMN:
            flags |= Qt.ItemIsUserCheckable
            flags &= ~Qt.ItemIsEditable
        return flags

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:
        if not index.isValid() or index.column() != _CHECK_COLUMN:
            return False
        if role != Qt.CheckStateRole:
            return False
        row = self._rows[index.row()]
        if value == Qt.Checked:
            changed = row.full_path not in self._checked_paths
            self._checked_paths.add(row.full_path)
        else:
            changed = row.full_path in self._checked_paths
            self._checked_paths.discard(row.full_path)
        if changed:
            self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        return True

    def sync_with_results(self, results_model: ResultsTableModel) -> None:
        """Синхронизирует с моделью результатов: добавляет/обновляет строки.

        Удаление происходит отдельно через :meth:`remove_paths` — в момент,
        когда результаты уже отфильтрованы.
        """
        by_path: dict[str, list[SearchResult]] = {}
        order: list[str] = []
        for result in results_model.all_results():
            if result.full_path not in by_path:
                by_path[result.full_path] = []
                order.append(result.full_path)
            by_path[result.full_path].append(result)
            if len(result.context) > len(self._longest_context):
                self._longest_context = result.context

        paths_by_name: dict[str, set[str]] = {}
        for path in order:
            paths_by_name.setdefault(Path(path).name, set()).add(path)
        labels: dict[str, str] = {}
        for name, paths in paths_by_name.items():
            labels.update(disambiguating_labels(name, paths))
        if labels != self._labels_dict:
            self._labels_dict = labels

        new_items = [path for path in order if path not in self._row_by_path]
        if new_items:
            start = len(self._rows)
            self.beginInsertRows(QModelIndex(), start, start + len(new_items) - 1)
            for path in new_items:
                row_index = len(self._rows)
                self._row_by_path[path] = row_index
                self._rows.append(_FileRow(path, by_path[path]))
            self.endInsertRows()

        changed_rows: set[int] = set()
        for path, row_index in self._row_by_path.items():
            fresh = by_path.get(path, [])
            row = self._rows[row_index]
            if row.results != fresh:
                row.results = fresh
                changed_rows.add(row_index)
        for row_index in sorted(changed_rows):
            self.dataChanged.emit(
                self.index(row_index, 1),
                self.index(row_index, self.columnCount() - 1),
                [
                    Qt.DisplayRole,
                    Qt.ToolTipRole,
                    OccurrencesRole,
                    MatchRole,
                    FilenameMatchRole,
                ],
            )

    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self._row_by_path = {}
        self._checked_paths = set()
        self._labels_dict = {}
        self._longest_context = ""
        self.endResetModel()

    def set_all_checked(self, checked: bool) -> None:
        if not self._rows:
            return
        if checked:
            self._checked_paths = set(self._row_by_path)
        else:
            if not self._checked_paths:
                return
            self._checked_paths.clear()
        self.dataChanged.emit(
            self.index(0, _CHECK_COLUMN),
            self.index(self.rowCount() - 1, _CHECK_COLUMN),
            [Qt.CheckStateRole],
        )

    def checked_paths(self) -> list[str]:
        return [
            path
            for path, row_index in self._row_by_path.items()
            if path in self._checked_paths
        ]

    def has_checked(self) -> bool:
        return bool(self._checked_paths & set(self._row_by_path))

    def checked_count(self) -> int:
        return len(self.checked_paths())

    def set_checked_paths(self, paths: Iterable[str]) -> None:
        """Симметрично ResultsTableModel: перенос отметок между режимами."""
        fresh = {path for path in paths if path in self._row_by_path}
        if fresh == self._checked_paths:
            return
        self._checked_paths = fresh
        if self._rows:
            self.dataChanged.emit(
                self.index(0, _CHECK_COLUMN),
                self.index(self.rowCount() - 1, _CHECK_COLUMN),
                [Qt.CheckStateRole],
            )

    def remove_paths(self, paths: set[str]) -> None:
        if not paths:
            return
        remaining = [row for row in self._rows if row.full_path not in paths]
        if len(remaining) == len(self._rows):
            return
        self.beginResetModel()
        self._rows = []
        self._row_by_path = {}
        self._checked_paths -= paths
        for row_index, row in enumerate(remaining):
            self._row_by_path[row.full_path] = row_index
            self._rows.append(row)
        self.endResetModel()

    def path_at_source(self, row: int) -> str:
        return self._rows[row].full_path

    def primary_at_source(self, row: int) -> SearchResult:
        return self._rows[row].primary

    def group_results_at(self, row: int) -> List[SearchResult]:
        return list(self._rows[row].results)

    def count(self) -> int:
        return len(self._rows)


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
