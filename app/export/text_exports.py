"""Экспорт в CSV и Markdown."""
from __future__ import annotations

import csv
import os
import re
import tempfile
from pathlib import Path
from typing import Sequence

from app.config import ErrorEntry, SearchResult
from app.export.safety import markdown_inline_code, markdown_text, spreadsheet_text


def _atomic_text_writer(path: Path, newline: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    return fd, Path(tmp_name), newline


def export_to_csv(results: Sequence[SearchResult], file_path: str, errors: Sequence[ErrorEntry] | None = None) -> bool:
    target = Path(file_path)
    temporary: list[Path] = []
    try:
        fd, tmp_path, _ = _atomic_text_writer(target)
        temporary.append(tmp_path)
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as file_handle:
            writer = csv.writer(file_handle, delimiter=";")
            writer.writerow(["Имя файла", "Полный путь", "Тип", "Найденное слово", "Контекст", "Изменён"])
            for result in results:
                writer.writerow([
                    spreadsheet_text(result.file_name), spreadsheet_text(result.full_path),
                    spreadsheet_text(result.file_type), spreadsheet_text(result.word),
                    spreadsheet_text(result.context), spreadsheet_text(result.modified),
                ])
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(tmp_path, target)
        temporary.remove(tmp_path)

        if errors:
            error_target = target.with_name(target.stem + "_errors.csv")
            fd, error_tmp, _ = _atomic_text_writer(error_target)
            temporary.append(error_tmp)
            with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as file_handle:
                writer = csv.writer(file_handle, delimiter=";")
                writer.writerow(["Время", "Уровень", "Файл", "Сообщение"])
                for error in errors:
                    writer.writerow([
                        spreadsheet_text(error.timestamp), spreadsheet_text(error.level),
                        spreadsheet_text(error.file_path), spreadsheet_text(error.message),
                    ])
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(error_tmp, error_target)
            temporary.remove(error_tmp)
        return True
    except OSError:
        return False
    finally:
        for tmp_path in temporary:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _highlight_context(context: str, matched_text: str) -> str:
    safe_context = markdown_text(context)
    if not matched_text:
        return safe_context
    safe_match = markdown_text(matched_text)
    return re.sub(re.escape(safe_match), lambda match: f"**{match.group(0)}**", safe_context, flags=re.IGNORECASE)


def export_to_markdown(
    results: Sequence[SearchResult],
    search_info: dict,
    errors: Sequence[ErrorEntry],
    file_path: str,
) -> bool:
    target = Path(file_path)
    tmp_path: Path | None = None
    try:
        lines = ["# Отчёт DSP Scanner", ""]
        lines.append(f"**Дата:** {markdown_text(search_info.get('generated_at', ''))}  ")
        lines.append(f"**Пути поиска:** {markdown_text(', '.join(map(str, search_info.get('paths', []))))}  ")
        lines.append(f"**В содержимом:** {markdown_text(', '.join(map(str, search_info.get('words', []))))}  ")
        lines.append(f"**В названиях файлов:** {markdown_text(', '.join(map(str, search_info.get('filename_words', []))))}  ")
        lines.append(f"**Метод чтения DOC:** {markdown_text(search_info.get('doc_method', ''))}  ")
        lines.extend(["", f"Найдено совпадений: **{len(results)}**", ""])

        by_file: dict[str, list[SearchResult]] = {}
        for result in results:
            by_file.setdefault(result.full_path, []).append(result)
        for full_path, items in by_file.items():
            lines.append(f"## {markdown_text(items[0].file_name)}")
            lines.append(markdown_inline_code(full_path))
            lines.append("")
            for item in items:
                context = _highlight_context(item.context, item.matched_text or item.word)
                lines.append(f"- **{markdown_text(item.word)}**: {context}")
            lines.append("")

        if errors:
            lines.append("## Ошибки")
            for error in errors:
                lines.append(
                    f"- {markdown_inline_code(error.timestamp)} [{markdown_text(error.level)}] "
                    f"{markdown_text(error.file_path)}: {markdown_text(error.message)}"
                )

        fd, tmp_name, _ = _atomic_text_writer(target)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file_handle:
            file_handle.write("\n".join(lines))
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(tmp_path, target)
        return True
    except OSError:
        return False
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
