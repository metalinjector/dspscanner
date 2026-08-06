"""
Глобальные константы и конфигурационные модели приложения DSP Scanner.
"""
from __future__ import annotations

import math
import platform
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Set

APP_NAME = "DSP Scanner"
APP_VERSION = "2.9.9"


def _default_log_dir() -> Path:
    if platform.system() == "Windows" and Path("D:\\").exists():
        return Path("D:/PS")
    return Path.home() / "DSPScanner" / "logs"


def _default_copy_destination() -> Path:
    if platform.system() == "Windows" and Path("D:\\").exists():
        return Path("D:/EX")
    return Path.home() / "DSPScanner" / "EX"


LOG_DIR = _default_log_dir()
LOG_FILE = LOG_DIR / "search_errors.log"
DEFAULT_COPY_DESTINATION = _default_copy_destination()
REPORTS_DIR = Path.home() / "DSPScanner" / "reports"
SETTINGS_FILE = Path.home() / "DSPScanner" / "settings.json"

SUPPORTED_EXTENSIONS = (".docx", ".doc", ".txt", ".pdf")

IGNORED_FOLDER_NAMES = {
    "appdata", "application data", "$recycle.bin", "system volume information",
    "programdata", "windows", "temp", "temporary internet files",
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    "$windows.~ws", "$windows.~bt",
}

TEMP_FILE_PREFIXES = ("~$", "~")
CONTEXT_CHARS_DEFAULT = 40
MAX_FILE_SIZE_MB_DEFAULT = 25
MAX_MATCHES_PER_WORD_PER_FILE = 50
PDF_PAGE_LIMIT_DEFAULT = 10
# Единая верхняя граница числа потоков. GUI (settings_dialog) и CLI уже
# ограничивают ввод этим значением; validate() ниже должен использовать то же
# число, иначе JSON с недостижимым в интерфейсе значением проходил бы проверку.
MAX_WORKERS_LIMIT = 128

# Защита от ZIP-bomb / XML-bomb в DOCX. Пределы достаточно велики для
# обычных документов, но не позволяют маленькому архиву развернуться в гигабайты.
DOCX_MAX_ENTRIES = 20_000
DOCX_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
DOCX_MAX_SINGLE_ENTRY_BYTES = 128 * 1024 * 1024
DOCX_MAX_COMPRESSION_RATIO = 1_000
DOCX_MAX_XML_BYTES = 64 * 1024 * 1024


class DocReadMethod(str, Enum):
    AUTO = "auto"
    LIBREOFFICE = "libreoffice"
    COM = "com"
    HEURISTIC = "heuristic"

    @property
    def label(self) -> str:
        return {
            DocReadMethod.AUTO: "Авто: эвристика → внешние программы",
            DocReadMethod.LIBREOFFICE: "LibreOffice (наиболее точный)",
            DocReadMethod.COM: "MS Word COM (только Windows)",
            DocReadMethod.HEURISTIC: "Быстрая эвристика (без внешних программ)",
        }[self]


class ExportFormat(str, Enum):
    EXCEL = "xlsx"
    CSV = "csv"
    MARKDOWN = "md"


@dataclass
class ScanSettings:
    paths: List[str] = field(default_factory=list)
    words: List[str] = field(default_factory=list)
    filename_words: List[str] = field(default_factory=list)
    file_types: Set[str] = field(default_factory=lambda: set(SUPPORTED_EXTENSIONS))
    doc_read_method: DocReadMethod = DocReadMethod.AUTO
    case_sensitive: bool = False
    whole_word: bool = False
    context_chars: int = CONTEXT_CHARS_DEFAULT
    max_file_size_mb: float = MAX_FILE_SIZE_MB_DEFAULT
    max_workers: int = 0
    use_ocr_for_pdf: bool = True
    limit_pdf_pages: bool = False
    pdf_page_limit: int = PDF_PAGE_LIMIT_DEFAULT
    # Видимая пользователю настройка: после быстрого эвристического прохода
    # повторно обрабатывать только нераспознанные legacy .doc через доступные
    # внешние программы (LibreOffice / MS Word).
    use_external_programs_after_heuristic_failure: bool = True
    # Внутренний флаг второго прохода. В JSON интерфейса не сохраняется и
    # запрещает reader-у снова откатываться к эвристике после внешних программ.
    doc_external_only: bool = False
    max_matches_per_word: int = MAX_MATCHES_PER_WORD_PER_FILE

    @property
    def max_file_size_bytes(self) -> int:
        return int(self.max_file_size_mb * 1024 * 1024)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.paths:
            errors.append("Не указан ни один путь для поиска.")
        all_words = [*self.words, *self.filename_words]
        if not all_words:
            errors.append("Не указано ни одного ключевого слова ни для содержимого, ни для названий файлов.")
        elif any(not word.strip().replace("*", "") for word in all_words):
            errors.append("Поисковый термин не может состоять только из символов '*'.")
        if len(self.words) > 1_000 or len(self.filename_words) > 1_000:
            errors.append("Слишком много поисковых терминов (максимум 1000 в каждом поле).")
        if any(len(str(word)) > 500 for word in all_words):
            errors.append("Длина одного поискового термина не должна превышать 500 символов.")
        if not self.file_types:
            errors.append("Не выбран ни один тип файлов.")
        unsupported = {ext.lower() for ext in self.file_types} - set(SUPPORTED_EXTENSIONS)
        if unsupported:
            errors.append("Неподдерживаемые типы файлов: " + ", ".join(sorted(unsupported)))
        if self.max_workers < 0 or self.max_workers > MAX_WORKERS_LIMIT:
            errors.append(f"Число потоков должно быть от 0 (авто) до {MAX_WORKERS_LIMIT}.")
        if self.context_chars < 20 or self.context_chars > 10_000:
            errors.append("Число символов контекста должно быть от 20 до 10000.")
        if not math.isfinite(self.max_file_size_mb) or self.max_file_size_mb <= 0:
            errors.append("Максимальный размер файла должен быть положительным числом.")
        if not 1 <= self.max_matches_per_word <= 10_000:
            errors.append("Лимит совпадений на слово/файл должен быть от 1 до 10000.")
        if self.limit_pdf_pages and not 1 <= self.pdf_page_limit <= 100_000:
            errors.append("Число первых страниц PDF должно быть от 1 до 100000.")
        return errors


@dataclass
class ScanStats:
    processed: int = 0
    found: int = 0
    skipped: int = 0
    errors: int = 0
    locked: int = 0
    corrupted: int = 0
    temp_files: int = 0
    docx_count: int = 0
    doc_count: int = 0
    txt_count: int = 0
    pdf_count: int = 0
    cache_hits: int = 0
    cache_misses: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class FileEntry:
    path: Path
    size: int
    modified: float
    extension: str

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class SearchResult:
    file_name: str
    full_path: str
    word: str
    context: str
    file_type: str
    modified: str
    occurrence_index: int = 1
    matched_text: str = ""
    # Расширенный фрагмент только для всплывающей подсказки. Табличный
    # контекст остаётся компактным и управляется параметром «Контекст».
    tooltip_context: str = ""
    # Ещё более широкий фрагмент для модального просмотра всех совпадений.
    detail_context: str = ""


@dataclass
class ErrorEntry:
    file_path: str
    level: str
    message: str
    timestamp: str


@dataclass
class ScanReport:
    results: List[SearchResult] = field(default_factory=list)
    errors: List[ErrorEntry] = field(default_factory=list)
    # Финальный список файлов, которые не удалось прочитать ни основным,
    # ни резервным способом. В отличие от общего журнала сюда не попадают
    # предупреждения успешного чтения и ошибки конфигурации без пути к файлу.
    unreadable_files: List[ErrorEntry] = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)
    elapsed_seconds: float = 0.0
    doc_read_method: str = ""
    cancelled: bool = False
