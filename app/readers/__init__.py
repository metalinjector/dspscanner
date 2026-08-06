"""
Пакет читателей документов.

Каждый модуль отвечает за извлечение текста из одного типа файлов и
реализует единый интерфейс `BaseReader.extract_text(path) -> ReaderOutcome`.
Диспетчеризация по расширению выполняется в `app.readers.registry`.
"""
from pathlib import Path

from app.config import ScanSettings
from app.readers.base import BaseReader, ReaderOutcome
from app.readers.docx_reader import DocxReader
from app.readers.doc_reader import DocReader
from app.readers.txt_reader import TxtReader
from app.readers.pdf_reader import PdfReader

__all__ = [
    "BaseReader",
    "ReaderOutcome",
    "DocxReader",
    "DocReader",
    "TxtReader",
    "PdfReader",
    "extract_text",
]

_docx_reader = DocxReader()
_doc_reader = DocReader()
_txt_reader = TxtReader()
_pdf_reader = PdfReader()


def extract_text(path: Path, extension: str, settings: ScanSettings) -> ReaderOutcome:
    """Единая точка входа для извлечения текста из файла произвольного типа,
    поддерживаемого приложением. Скрывает различия в сигнатурах читателей."""
    ext = extension.lower()
    if ext == ".docx":
        return _docx_reader.extract_text(path)
    if ext == ".doc":
        if settings.doc_external_only:
            return _doc_reader.extract_text_external(path)
        return _doc_reader.extract_text(path, method=settings.doc_read_method)
    if ext == ".txt":
        return _txt_reader.extract_text(path)
    if ext == ".pdf":
        return _pdf_reader.extract_text(
            path,
            use_ocr=settings.use_ocr_for_pdf,
            max_pages=(
                settings.pdf_page_limit
                if settings.use_ocr_for_pdf and settings.limit_pdf_pages
                else 0
            ),
        )
    return ReaderOutcome(error=f"Неподдерживаемое расширение: {extension}")
