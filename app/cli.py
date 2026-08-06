"""Командный интерфейс DSP Scanner для автоматического запуска на сервере."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, Mapping, Sequence

from app.config import (
    APP_VERSION, DocReadMethod, MAX_WORKERS_LIMIT, ScanReport, ScanSettings, SUPPORTED_EXTENSIONS,
)
from app.default_paths import default_search_paths
from app.email_sender import send_email
from app.email_settings import EmailSettings
from app.reporting import build_search_info, export_report, normalize_report_format
from app.scan_config_store import existing_auto_config_path, load_scan_config
from app.scanning.scanner import DocumentScanner
from app.scanning.term_parser import parse_terms
from app.settings_store import invalidate_settings_cache

EXIT_OK = 0
EXIT_ARGUMENTS = 2
EXIT_EXPORT = 3
EXIT_EMAIL = 4
EXIT_INTERNAL = 5
EXIT_SCAN_ERRORS = 6
EXIT_CANCELLED = 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="DSP Scanner",
        description=(
            "Поиск ключевых слов в DOCX, DOC, TXT и PDF без запуска графического интерфейса. "
            "Параметры берутся из DSPScanner_Config/search_settings.json и могут быть "
            "переопределены ключами командной строки."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"DSP Scanner {APP_VERSION}")
    parser.add_argument("--config", metavar="FILE", help="JSON-конфигурация; по умолчанию переносимая автоконфигурация")
    parser.add_argument("--no-config", action="store_true", help="не загружать автоматическую JSON-конфигурацию")

    source = parser.add_argument_group("Область поиска")
    source.add_argument("--path", action="append", metavar="PATH", help="корневая папка/файл; можно повторять")
    source.add_argument(
        "--types", metavar="LIST",
        help="расширения через запятую: docx,doc,txt,pdf",
    )
    source.add_argument("--max-size-mb", type=float, help="максимальный размер файла для чтения содержимого")

    terms = parser.add_argument_group("Ключевые слова")
    terms.add_argument("--content-term", action="append", metavar="TEXT", help="термин для содержимого; можно повторять")
    terms.add_argument("--content-terms", metavar="TEXT", help="список терминов через запятую, точку с запятой или новую строку")
    terms.add_argument("--content-terms-file", metavar="FILE", help="UTF-8 файл со списком терминов для содержимого")
    terms.add_argument("--filename-term", action="append", metavar="TEXT", help="термин для названий файлов; можно повторять")
    terms.add_argument("--filename-terms", metavar="TEXT", help="список терминов для названий файлов")
    terms.add_argument("--filename-terms-file", metavar="FILE", help="UTF-8 файл со списком терминов для названий")
    terms.add_argument("--case-sensitive", action=argparse.BooleanOptionalAction, default=None, help="учитывать регистр")
    terms.add_argument("--whole-word", action=argparse.BooleanOptionalAction, default=None, help="искать только целые слова")
    terms.add_argument("--context", type=int, help="число символов контекста вокруг совпадения")
    terms.add_argument("--max-matches", type=int, help="максимум совпадений одного термина в одном файле")

    reading = parser.add_argument_group("Чтение документов")
    reading.add_argument(
        "--doc-method", choices=[item.value for item in DocReadMethod],
        help="метод чтения legacy DOC",
    )
    reading.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=None, help="OCR проблемных PDF")
    reading.add_argument(
        "--pdf-first-pages",
        type=int,
        metavar="N",
        help=(
            "при включённом OCR читать только первые N страниц каждого PDF; "
            "0 отключает ограничение"
        ),
    )
    reading.add_argument(
        "--external-after-heuristic-failure",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="после быстрой эвристики повторять ошибочные DOC внешними программами",
    )
    reading.add_argument("--workers", type=int, help="число параллельных потоков")
    reading.add_argument("--timeout", type=int, help="таймаут чтения одного файла, секунд")
    reading.add_argument("--libreoffice", metavar="FILE", help="путь к soffice/LibreOffice для этого запуска")
    reading.add_argument("--tesseract", metavar="FILE", help="путь к tesseract для этого запуска")

    output = parser.add_argument_group("Отчёт")
    output.add_argument("--format", choices=("xlsx", "csv", "md"), default="xlsx", help="формат локального отчёта")
    output.add_argument("--output", metavar="FILE_OR_DIR", help="путь отчёта или каталог; расширение добавляется автоматически")
    output.add_argument("--summary-json", metavar="FILE", help="записать краткий машинно-читаемый итог в JSON")
    output.add_argument("--fail-on-scan-errors", action="store_true", help="вернуть код 6, если при чтении были ошибки/предупреждения")

    mail = parser.add_argument_group("Отправка по электронной почте")
    mail.add_argument("--send-email", action="store_true", help="отправить отчёт после завершения сканирования")
    mail.add_argument("--email-format", choices=("xlsx", "csv", "md"), help="формат вложения; иначе значение из JSON")
    mail.add_argument("--smtp-host", help="переопределить SMTP-сервер")
    mail.add_argument("--smtp-port", type=int, help="переопределить SMTP-порт")
    mail.add_argument("--smtp-security", choices=("starttls", "ssl", "none"), help="режим защиты SMTP")
    mail.add_argument("--smtp-user", help="имя пользователя SMTP")
    mail.add_argument("--mail-from", dest="mail_from", help="адрес отправителя")
    mail.add_argument("--mail-to", help="получатели To через запятую/точку с запятой")
    mail.add_argument("--mail-cc", help="получатели Cc")
    mail.add_argument("--mail-bcc", help="получатели Bcc")
    mail.add_argument("--mail-subject", help="шаблон темы письма")
    mail.add_argument("--mail-body", help="шаблон текста письма")
    mail.add_argument("--smtp-timeout", type=int, help="таймаут SMTP, секунд")
    mail.add_argument(
        "--email-without-matches",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="отправлять письмо, даже если совпадений нет",
    )

    logging = parser.add_argument_group("Вывод")
    logging.add_argument("--quiet", action="store_true", help="показывать только итог и ошибки")
    logging.add_argument("--verbose", action="store_true", help="подробный журнал в консоли")
    return parser


def _read_utf8(path: str) -> str:
    target = Path(path).expanduser()
    if target.stat().st_size > 5_000_000:
        raise ValueError(f"Файл терминов слишком велик: {target}")
    return target.read_text(encoding="utf-8-sig")


def _terms_from_args(single: Sequence[str] | None, packed: str | None, file_path: str | None) -> list[str] | None:
    supplied = single is not None or packed is not None or file_path is not None
    if not supplied:
        return None
    values: list[str] = []
    if single:
        values.extend(str(item).strip() for item in single if str(item).strip())
    if packed is not None:
        values.extend(parse_terms(packed))
    if file_path is not None:
        values.extend(parse_terms(_read_utf8(file_path)))
    # Стабильное устранение дублей без изменения порядка.
    return list(dict.fromkeys(values))


def _config_or_empty(args: argparse.Namespace) -> tuple[dict[str, Any], Path | None]:
    if args.no_config:
        return {}, None
    if args.config:
        path = Path(args.config).expanduser()
        config = load_scan_config(path)
        if config is None:
            raise ValueError(f"Конфигурация не найдена: {path}")
        return config, path
    path = existing_auto_config_path()
    if path is None:
        return {}, None
    return load_scan_config(path) or {}, path


def _bool_value(cli_value: bool | None, config: Mapping[str, Any], key: str, default: bool) -> bool:
    if cli_value is not None:
        return bool(cli_value)
    return bool(config.get(key, default))


def _parse_types(value: str | None, config: Mapping[str, Any]) -> set[str]:
    if value is None:
        raw = config.get("file_types", list(SUPPORTED_EXTENSIONS))
        parts = raw if isinstance(raw, list) else list(SUPPORTED_EXTENSIONS)
    else:
        parts = value.replace(";", ",").split(",")
    normalized = {"." + str(item).strip().lower().lstrip(".") for item in parts if str(item).strip()}
    unknown = normalized - set(SUPPORTED_EXTENSIONS)
    if unknown:
        raise ValueError("Неподдерживаемые типы: " + ", ".join(sorted(unknown)))
    return normalized


def _number(cli_value: Any, config: Mapping[str, Any], key: str, default: Any, caster):
    value = cli_value if cli_value is not None else config.get(key, default)
    try:
        return caster(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Некорректное значение {key}: {value}") from exc


def build_scan_settings(args: argparse.Namespace, config: Mapping[str, Any]) -> ScanSettings:
    content_override = _terms_from_args(args.content_term, args.content_terms, args.content_terms_file)
    filename_override = _terms_from_args(args.filename_term, args.filename_terms, args.filename_terms_file)

    if args.path is not None:
        raw_paths = args.path
    elif config.get("paths_mode") == "default" or "paths" not in config:
        raw_paths = default_search_paths()
    else:
        raw_paths = config.get("paths", [])
    paths = [str(Path(item).expanduser()) for item in raw_paths if str(item).strip()] if isinstance(raw_paths, list) else []

    if content_override is None:
        raw_words = config.get("words", "")
        words = parse_terms(raw_words) if isinstance(raw_words, str) else [str(item) for item in raw_words or []]
    else:
        words = content_override
    if filename_override is None:
        raw_words = config.get("filename_words", "")
        filename_words = parse_terms(raw_words) if isinstance(raw_words, str) else [str(item) for item in raw_words or []]
    else:
        filename_words = filename_override

    method_value = args.doc_method or str(config.get("doc_method", DocReadMethod.AUTO.value))
    try:
        method = DocReadMethod(method_value)
    except ValueError as exc:
        raise ValueError(f"Неизвестный метод DOC: {method_value}") from exc

    if args.pdf_first_pages is not None:
        if args.pdf_first_pages < 0:
            raise ValueError("--pdf-first-pages должен быть 0 или положительным числом")
        limit_pdf_pages = args.pdf_first_pages > 0
        pdf_page_limit = max(1, args.pdf_first_pages) if limit_pdf_pages else 10
    else:
        limit_pdf_pages = bool(config.get("limit_pdf_pages", False))
        pdf_page_limit = _number(None, config, "pdf_page_limit", 10, int)

    settings = ScanSettings(
        paths=paths,
        words=words,
        filename_words=filename_words,
        file_types=_parse_types(args.types, config),
        doc_read_method=method,
        case_sensitive=_bool_value(args.case_sensitive, config, "case_sensitive", False),
        whole_word=_bool_value(args.whole_word, config, "whole_word", False),
        context_chars=_number(args.context, config, "context_chars", 20, int),
        max_file_size_mb=_number(args.max_size_mb, config, "max_size_mb", 25, float),
        max_workers=int(args.workers or 0),
        use_ocr_for_pdf=_bool_value(args.ocr, config, "use_ocr", True),
        limit_pdf_pages=limit_pdf_pages,
        pdf_page_limit=pdf_page_limit,
        use_external_programs_after_heuristic_failure=_bool_value(
            args.external_after_heuristic_failure,
            config,
            "external_after_heuristic_failure",
            True,
        ),
        max_matches_per_word=int(
            args.max_matches
            if args.max_matches is not None
            else (
                config.get("application", {}).get("max_matches_per_word", 50)
                if isinstance(config.get("application"), Mapping)
                else 50
            )
        ),
    )
    errors = settings.validate()
    if errors:
        raise ValueError(" ".join(errors))
    return settings


def _apply_runtime_application_settings(args: argparse.Namespace, config: Mapping[str, Any]) -> None:
    application = config.get("application") if isinstance(config.get("application"), Mapping) else {}

    for label, value in (("LibreOffice", args.libreoffice), ("Tesseract", args.tesseract)):
        if value is not None and not Path(value).expanduser().is_file():
            raise ValueError(f"{label}: исполняемый файл не найден: {value}")
    if args.workers is not None and not 1 <= args.workers <= MAX_WORKERS_LIMIT:
        raise ValueError(f"--workers должен быть от 1 до {MAX_WORKERS_LIMIT}")
    if args.timeout is not None and not 10 <= args.timeout <= 600:
        raise ValueError("--timeout должен быть от 10 до 600 секунд")
    if args.max_matches is not None and not 1 <= args.max_matches <= 500:
        raise ValueError("--max-matches должен быть от 1 до 500")

    config_lo = application.get("libreoffice_path")
    config_ts = application.get("tesseract_path")
    # Пути из переносимого JSON могли относиться к другому компьютеру.
    # Несуществующие значения пропускаются, чтобы load_settings() выполнил
    # обычное автоопределение.
    if config_lo and not Path(str(config_lo)).expanduser().is_file():
        config_lo = None
    if config_ts and not Path(str(config_ts)).expanduser().is_file():
        config_ts = None

    overrides = {
        "DSP_SCANNER_LIBREOFFICE_PATH": args.libreoffice if args.libreoffice is not None else config_lo,
        "DSP_SCANNER_TESSERACT_PATH": args.tesseract if args.tesseract is not None else config_ts,
        "DSP_SCANNER_PER_FILE_TIMEOUT": args.timeout if args.timeout is not None else application.get("per_file_timeout"),
        "DSP_SCANNER_MAX_WORKERS": args.workers if args.workers is not None else application.get("max_workers"),
        "DSP_SCANNER_MAX_MATCHES": args.max_matches if args.max_matches is not None else application.get("max_matches_per_word"),
    }
    for name, value in overrides.items():
        if value is not None and str(value).strip():
            os.environ[name] = str(value)
    invalidate_settings_cache()


def _email_settings(args: argparse.Namespace, config: Mapping[str, Any]) -> EmailSettings:
    section = config.get("email") if isinstance(config.get("email"), Mapping) else {}
    settings = EmailSettings.from_mapping(section)
    values = asdict(settings)
    mapping = {
        "smtp_host": args.smtp_host,
        "smtp_port": args.smtp_port,
        "security": args.smtp_security,
        "username": args.smtp_user,
        "from_address": args.mail_from,
        "to_addresses": args.mail_to,
        "cc_addresses": args.mail_cc,
        "bcc_addresses": args.mail_bcc,
        "subject_template": args.mail_subject,
        "body_template": args.mail_body,
        "timeout_seconds": args.smtp_timeout,
        "report_format": args.email_format,
        "send_when_no_matches": args.email_without_matches,
    }
    for key, value in mapping.items():
        if value is not None:
            values[key] = value
    return EmailSettings(**values).sanitized()


def _template_values(report: ScanReport) -> dict[str, object]:
    return {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "matches": len(report.results),
        "processed": report.stats.processed,
        "errors": len(report.errors),
        "elapsed": f"{report.elapsed_seconds:.1f}",
        "host": socket.gethostname(),
        "status": "отменено" if report.cancelled else "завершено",
        "report_format": "",
    }


def _force_utf8_streams() -> None:
    """Печатает вывод в UTF-8 независимо от кодировки локали.

    Когда stdout не является консолью (Планировщик заданий Windows, запись в
    лог-файл, конвейер, CI), Python кодирует вывод в кодировку локали. На
    Windows с не-кириллической локалью первая же строка с русским текстом
    завершала работу с UnicodeEncodeError, а в stderr сообщения превращались
    в escape-последовательности вида ``\\u0434``. Ошибка возникала до начала
    сканирования, поэтому отчёт не создавался вовсе.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # errors="replace" — на случай неразбираемых имён файлов с
            # суррогатными символами: испорченный символ лучше падения.
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(data), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def run_cli(argv: Sequence[str] | None = None) -> int:
    _force_utf8_streams()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config, config_path = _config_or_empty(args)
        _apply_runtime_application_settings(args, config)
        settings = build_scan_settings(args, config)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
        return EXIT_ARGUMENTS

    cancel_event = Event()

    def request_cancel(_signum, _frame) -> None:
        cancel_event.set()

    try:
        signal.signal(signal.SIGINT, request_cancel)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_cancel)
    except (ValueError, OSError):
        pass

    if not args.quiet:
        source = str(config_path) if config_path else "без JSON-конфигурации"
        print(f"DSP Scanner {APP_VERSION}; конфигурация: {source}", flush=True)

    def progress(current: int, total: int, message: str) -> None:
        if args.quiet:
            return
        print(f"[{current}/{total}] {message}", flush=True)

    def log(level: str, message: str) -> None:
        if args.verbose or level in {"WARNING", "ERROR"}:
            print(f"{level}: {message}", file=sys.stderr if level == "ERROR" else sys.stdout, flush=True)

    try:
        report = DocumentScanner().run(
            settings,
            cancel_event=cancel_event,
            on_progress=progress,
            on_log=log,
        )
    except KeyboardInterrupt:
        # Не маскируем Ctrl+C как внутреннюю ошибку. Обычно установленный выше
        # обработчик SIGINT лишь выставляет cancel_event, но явная ветка нужна
        # для платформ/окружений, где KeyboardInterrupt всё же пробрасывается.
        cancel_event.set()
        print("Сканирование отменено пользователем.", file=sys.stderr)
        return EXIT_CANCELLED
    except Exception as exc:
        print(f"Критическая ошибка сканирования: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL

    if report.cancelled:
        print("Сканирование отменено.", file=sys.stderr)
        return EXIT_CANCELLED

    search_info = build_search_info(settings)
    try:
        local_artifacts = export_report(report, search_info, args.format, args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Ошибка экспорта: {exc}", file=sys.stderr)
        return EXIT_EXPORT

    email_sent = False
    email_attachments: list[str] = []
    if args.send_email:
        mail_settings = _email_settings(args, config)
        if report.results or mail_settings.send_when_no_matches:
            try:
                email_format = normalize_report_format(mail_settings.report_format)
                if email_format == normalize_report_format(args.format):
                    mail_artifacts = local_artifacts
                else:
                    mail_artifacts = export_report(report, search_info, email_format)
                values = _template_values(report)
                values["report_format"] = email_format
                send_result = send_email(
                    mail_settings,
                    template_values=values,
                    attachments=mail_artifacts.attachments,
                )
                email_sent = True
                email_attachments = list(send_result.attachments)
                if not args.quiet:
                    print("Письмо отправлено: " + ", ".join(send_result.recipients), flush=True)
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"Ошибка отправки email: {exc}", file=sys.stderr)
                return EXIT_EMAIL
        elif not args.quiet:
            print("Письмо не отправлено: совпадений нет и это отключено настройкой.")

    summary = {
        "version": APP_VERSION,
        "status": "completed",
        "processed": report.stats.processed,
        "matches": len(report.results),
        "errors": len(report.errors),
        "elapsed_seconds": report.elapsed_seconds,
        "report": str(local_artifacts.primary),
        "email_sent": email_sent,
        "email_attachments": email_attachments,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    if args.summary_json:
        try:
            _atomic_json(Path(args.summary_json).expanduser(), summary)
        except OSError as exc:
            print(f"Не удалось записать summary JSON: {exc}", file=sys.stderr)
            return EXIT_EXPORT

    print(
        f"Готово: обработано={report.stats.processed}, совпадений={len(report.results)}, "
        f"ошибок/предупреждений={len(report.errors)}, отчёт={local_artifacts.primary}",
        flush=True,
    )
    if args.fail_on_scan_errors and report.errors:
        return EXIT_SCAN_ERRORS
    return EXIT_OK
