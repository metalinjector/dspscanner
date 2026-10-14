"""Безопасное чтение DOCX (Office Open XML / ZIP)."""
from __future__ import annotations

import email
import io
import logging
from email.policy import default
import re
import zipfile
from pathlib import Path
from typing import List, Optional

from app.config import (
    DOCX_MAX_COMPRESSION_RATIO,
    DOCX_MAX_ENTRIES,
    DOCX_MAX_SINGLE_ENTRY_BYTES,
    DOCX_MAX_UNCOMPRESSED_BYTES,
    DOCX_MAX_XML_BYTES,
)
from app.readers.base import BaseReader, ReaderOutcome
from app.readers.doc_formats import read_html, read_rtf

# Дочерний логгер "dspscanner": пишет в тот же файл журнала и вкладку "Журнал"
# GUI, что и остальное приложение (см. app/logging_utils.py).
_logger = logging.getLogger("dspscanner.docx_reader")

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_STRICT_WORD_NS = "{http://purl.oclc.org/ooxml/wordprocessingml/main}"
_WORD_NAMESPACES = (_WORD_NS, _STRICT_WORD_NS)
_WORD_TAGS = {f"{ns}{tag}": tag for ns in _WORD_NAMESPACES
              for tag in ("p", "t", "br", "tab", "cr", "tc", "tr", "noBreakHyphen")}
_RELEVANT_XML_PREFIXES = (
    "word/document", "word/header", "word/footer", "word/footnotes",
    "word/endnotes", "word/comments",
)


def _validate_docx_archive(archive: zipfile.ZipFile) -> tuple[bool, Optional[str]]:
    """Проверяет центральный каталог до распаковки данных.

    Защищает python-docx и резервный XML-парсер от ZIP-bomb и аномально
    больших XML-частей. Архив не извлекается на диск.

    Принимает уже открытый ``ZipFile`` — вызывающий код открывает архив
    ровно один раз.
    """
    try:
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        return False, f"Некорректный ZIP-контейнер DOCX: {exc}"

    if len(infos) > DOCX_MAX_ENTRIES:
        return False, f"Слишком много элементов в DOCX: {len(infos)}"

    total_uncompressed = 0
    for info in infos:
        if info.is_dir():
            continue
        total_uncompressed += info.file_size
        if info.file_size > DOCX_MAX_SINGLE_ENTRY_BYTES:
            return False, f"Слишком крупная часть DOCX: {info.filename}"
        compressed = max(1, info.compress_size)
        if info.file_size > 1_000_000 and info.file_size / compressed > DOCX_MAX_COMPRESSION_RATIO:
            return False, f"Подозрительная степень сжатия DOCX: {info.filename}"
        normalized = info.filename.lower().replace("\\", "/")
        if normalized.endswith(".xml") and info.file_size > DOCX_MAX_XML_BYTES:
            return False, f"Слишком крупный XML внутри DOCX: {info.filename}"
    if total_uncompressed > DOCX_MAX_UNCOMPRESSED_BYTES:
        return False, "DOCX слишком велик после распаковки"
    return True, None


class DocxReader(BaseReader):
    name = "docx"

    def extract_text(self, path: Path) -> ReaderOutcome:
        # Открываем ZipFile ровно один раз — раньше архив открывался в
        # is_file_accessible, _validate_docx_archive и _read_manual_xml.
        try:
            archive = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            import errno
            if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM):
                return ReaderOutcome(locked=True, error="Файл заблокирован")
            return ReaderOutcome(error=f"Некорректный DOCX-контейнер: {exc}")

        with archive:
            safe, error = _validate_docx_archive(archive)
            if not safe:
                return ReaderOutcome(error=error or "Небезопасный DOCX-контейнер")

            # Прямой XML-парсер линейно обходит содержимое архива и не страдает
            # квадратичным пересозданием table.row.cells в python-docx на больших
            # табличных документах. python-docx остаётся совместимым fallback.
            warnings: list[str] = []
            text = self._read_manual_xml(archive, warnings)
            altchunk = self._read_altchunk_parts(archive, warnings)
            warning = "; ".join(warnings) or None
            if text and altchunk:
                return ReaderOutcome(text=f"{text}\n\n{altchunk}",
                                     method_used="manual-xml+altchunk", warning=warning)
            if text:
                return ReaderOutcome(text=text, method_used="manual-xml", warning=warning)
            if altchunk:
                return ReaderOutcome(text=altchunk, method_used="altchunk-mht-parser", warning=warning)

        # python-docx остаётся резервом; все MHTML-части уже проверены в первом архиве.
        text = self._read_with_python_docx(path)
        if text:
            return ReaderOutcome(text=text, method_used="python-docx-fallback", warning=warning)

        return ReaderOutcome(error="Не удалось извлечь текст из DOCX (файл повреждён?)")

    @staticmethod
    def _read_with_python_docx(path: Path) -> Optional[str]:
        try:
            import docx
        except ImportError:
            return None
        try:
            document = docx.Document(str(path))
            parts: List[str] = []
            parts.extend(para.text for para in document.paragraphs if para.text)
            for table in document.tables:
                for row in table.rows:
                    # python-docx возвращает один и тот же _Cell несколько раз
                    # подряд для горизонтально объединённых ячеек (gridSpan) в
                    # одной строке — без дедупликации текст объединённой ячейки
                    # попадает в вывод многократно и даёт фантомные лишние
                    # совпадения при поиске. Дедупликация намеренно ограничена
                    # одной строкой (не всей таблицей): id() у промежуточных
                    # _Cell-обёрток python-docx может быть переиспользован после
                    # сборки мусора между строками, что на практике приводило
                    # к ложному “дублированию” и потере текста несвязанных ячеек.
                    seen_cells: set[int] = set()
                    for cell in row.cells:
                        if id(cell._tc) in seen_cells:
                            continue
                        seen_cells.add(id(cell._tc))
                        if cell.text:
                            parts.append(cell.text)
            for section in document.sections:
                for header_footer in (section.header, section.footer):
                    try:
                        parts.extend(para.text for para in header_footer.paragraphs if para.text)
                    except Exception:
                        continue
            result = "\n".join(parts).strip()
            return result or None
        except Exception:
            return None

    @staticmethod
    def _read_manual_xml(archive: zipfile.ZipFile, warnings: Optional[list[str]] = None) -> Optional[str]:
        try:
            from defusedxml import ElementTree as ET

            names = sorted(
                name for name in archive.namelist()
                if name.lower().replace("\\", "/").endswith(".xml")
                and name.lower().replace("\\", "/").startswith(_RELEVANT_XML_PREFIXES)
            )
            if not names:
                return None

            document_parts: List[str] = []
            for name in names:
                try:
                    info = archive.getinfo(name)
                    if info.file_size > DOCX_MAX_XML_BYTES:
                        _logger.debug(
                            "DOCX: часть %s пропущена (превышен лимит размера XML)",
                            name,
                        )
                        continue
                    parts: List[str] = []
                    for event, elem in ET.iterparse(io.BytesIO(archive.read(name)), events=("start", "end")):
                        tag = _WORD_TAGS.get(elem.tag, "")
                        if event == "start" and tag == "p":
                            parts.append("\n")
                        elif event == "end":
                            if tag == "t" and elem.text:
                                parts.append(elem.text)
                            elif tag in {"p", "br", "tab", "cr", "tc", "tr"}:
                                parts.append("\n")
                            elif tag == "noBreakHyphen":
                                parts.append("-")
                            elem.clear()
                except Exception as exc:  # noqa: BLE001
                    # Одна повреждённая часть (например, comments.xml) не должна
                    # отменять извлечение текста из остальных исправных частей.
                    _logger.debug("DOCX: часть %s пропущена: %s", name, exc)
                    if warnings is not None:
                        warnings.append(f"DOCX прочитан частично: не удалось прочитать {name}")
                    continue
                text = "".join(parts).strip()
                if text:
                    document_parts.append(text)

            result = "\n\n".join(document_parts)
            result = re.sub(r"[ \t]+", " ", result)
            result = re.sub(r"\n{3,}", "\n\n", result).strip()
            return result or None
        except Exception:
            return None

    @staticmethod
    def _read_altchunk(path: Path) -> Optional[str]:
        """Fallback-путь: открывает архив самостоятельно."""
        try:
            with zipfile.ZipFile(path) as archive:
                return DocxReader._read_altchunk_parts(archive)
        except Exception:
            return None

    @staticmethod
    def _read_altchunk_parts(archive: zipfile.ZipFile, warnings: Optional[list[str]] = None) -> Optional[str]:
        # All imported MHTML sections, not just names[0]. Do not deduplicate
        # equal sections: two real occurrences must remain two search matches.
        results = []
        for name in archive.namelist():
            if not (name.lower().startswith("word/") and name.lower().endswith((".mht", ".mhtml"))):
                continue
            try:
                if archive.getinfo(name).file_size > DOCX_MAX_SINGLE_ENTRY_BYTES:
                    raise ValueError("MHTML part too large")
                message = email.message_from_bytes(archive.read(name), policy=default)
                # multipart/alternative is one body, not duplicate HTML+plain.
                part = message.get_body(preferencelist=("html", "plain"))
                if part is None:
                    part = next((p for p in message.walk() if p.get_content_type() == "application/rtf"), None)
                if part is None:
                    raise ValueError("No supported MHTML body")
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                if part.get_content_type() == "text/html":
                    text = read_html(payload, encoding=part.get_content_charset())
                elif part.get_content_type() == "application/rtf":
                    text = read_rtf(payload)
                else:
                    text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                if text and text.strip():
                    results.append(text.strip())
            except Exception as exc:  # malformed section must not hide later sections
                _logger.debug("DOCX: altChunk %s пропущен: %s", name, exc)
                if warnings is not None:
                    warnings.append(f"DOCX прочитан частично: не удалось прочитать {name}")
        return "\n\n".join(results) or None
