"""Единый экспорт отчётов для GUI, CLI и отправки по почте."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from app.config import REPORTS_DIR, ScanReport, ScanSettings
from app.export.excel_export import export_to_excel
from app.export.text_exports import export_to_csv, export_to_markdown

_FORMAT_EXTENSIONS = {"xlsx": ".xlsx", "csv": ".csv", "md": ".md"}


@dataclass(frozen=True)
class ReportArtifacts:
    primary: Path
    attachments: tuple[Path, ...]


def build_search_info(settings: ScanSettings, generated_at: datetime | None = None) -> dict:
    generated = generated_at or datetime.now()
    return {
        "generated_at": generated.strftime("%Y-%m-%d %H:%M:%S"),
        "paths": list(settings.paths),
        "words": list(settings.words),
        "filename_words": list(settings.filename_words),
        "doc_method": settings.doc_read_method.label,
        "external_after_heuristic_failure": settings.use_external_programs_after_heuristic_failure,
        "ocr": settings.use_ocr_for_pdf,
        "pdf_first_pages": (
            settings.pdf_page_limit if settings.limit_pdf_pages else None
        ),
    }


def default_report_path(
    report_format: str,
    *,
    directory: Path | str = REPORTS_DIR,
    now: datetime | None = None,
) -> Path:
    normalized = normalize_report_format(report_format)
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S_%f")
    return Path(directory) / f"search_results_{timestamp}{_FORMAT_EXTENSIONS[normalized]}"


def normalize_report_format(value: str) -> str:
    normalized = str(value or "xlsx").lower().lstrip(".")
    if normalized not in _FORMAT_EXTENSIONS:
        raise ValueError("Формат отчёта должен быть xlsx, csv или md")
    return normalized


def resolve_output_path(
    output: Path | str | None,
    report_format: str,
    *,
    directory: Path | str = REPORTS_DIR,
) -> Path:
    normalized = normalize_report_format(report_format)
    extension = _FORMAT_EXTENSIONS[normalized]
    if output is None:
        return default_report_path(normalized, directory=directory)
    target = Path(output).expanduser()
    if target.exists() and target.is_dir():
        return default_report_path(normalized, directory=target)
    # Завершающий слеш может указывать на ещё не созданную папку.
    if str(output).endswith(("/", "\\")):
        return default_report_path(normalized, directory=target)
    if target.suffix.lower() != extension:
        target = target.with_suffix(extension)
    return target


def export_report(
    report: ScanReport,
    search_info: Mapping[str, object],
    report_format: str,
    output: Path | str | None = None,
) -> ReportArtifacts:
    normalized = normalize_report_format(report_format)
    target = resolve_output_path(output, normalized)
    target.parent.mkdir(parents=True, exist_ok=True)

    if normalized == "xlsx":
        ok = export_to_excel(
            report.results,
            report.stats,
            report.errors,
            dict(search_info),
            str(target),
        )
        attachments: Sequence[Path] = (target,)
    elif normalized == "csv":
        ok = export_to_csv(report.results, str(target), report.errors)
        error_path = target.with_name(target.stem + "_errors.csv")
        attachments = (target, error_path) if error_path.is_file() else (target,)
    else:
        ok = export_to_markdown(
            report.results,
            dict(search_info),
            report.errors,
            str(target),
        )
        attachments = (target,)

    if not ok or not target.is_file():
        raise RuntimeError(f"Не удалось создать отчёт: {target}")
    return ReportArtifacts(primary=target, attachments=tuple(attachments))
