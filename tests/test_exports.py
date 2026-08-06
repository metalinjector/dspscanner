"""Экспорт отчётов: состав вложений, санитизация, атомарность."""
from __future__ import annotations

import csv

import pytest

from app.config import ErrorEntry, ScanReport, ScanStats, SearchResult
from app.export.safety import markdown_text, spreadsheet_text
from app.export.text_exports import export_to_csv, export_to_markdown
from app.reporting import export_report, normalize_report_format, resolve_output_path

SEARCH_INFO = {
    "generated_at": "2026-01-01 00:00:00",
    "paths": ["/x"],
    "words": ["договор"],
    "filename_words": [],
    "doc_method": "auto",
}


def _result(name="a.txt", context="контекст"):
    return SearchResult(name, f"/x/{name}", "договор", context, "txt", "2026-01-01")


def _report(errors=()):
    return ScanReport(results=[_result()], errors=list(errors), stats=ScanStats())


@pytest.mark.parametrize("report_format", ["xlsx", "csv", "md"])
def test_every_format_produces_a_file(tmp_path, report_format):
    target = tmp_path / f"report.{report_format}"
    artifacts = export_report(_report(), SEARCH_INFO, report_format, str(target))
    assert artifacts.primary.is_file()
    assert artifacts.primary.stat().st_size > 0


def test_csv_errors_file_appears_only_with_errors(tmp_path):
    target = tmp_path / "report.csv"
    errors = [ErrorEntry("/x/b.txt", "ERROR", "не прочитан", "2026-01-01")]
    artifacts = export_report(_report(errors), SEARCH_INFO, "csv", str(target))
    assert [p.name for p in artifacts.attachments] == ["report.csv", "report_errors.csv"]


def test_csv_rerun_without_errors_drops_previous_errors_file(tmp_path):
    """Иначе к письму цеплялся список ошибок предыдущего прогона."""
    target = tmp_path / "report.csv"
    errors = [ErrorEntry("/x/b.txt", "ERROR", "СТАРАЯ ОШИБКА", "2026-01-01")]
    export_report(_report(errors), SEARCH_INFO, "csv", str(target))
    assert (tmp_path / "report_errors.csv").is_file()

    artifacts = export_report(_report(), SEARCH_INFO, "csv", str(target))
    assert [p.name for p in artifacts.attachments] == ["report.csv"]
    assert not (tmp_path / "report_errors.csv").exists()


def test_csv_contains_the_match(tmp_path):
    target = tmp_path / "report.csv"
    assert export_to_csv([_result()], str(target))
    with open(target, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle, delimiter=";"))
    assert rows[0][0] == "Имя файла"
    assert rows[1][3] == "договор"


def test_markdown_highlights_the_match(tmp_path):
    target = tmp_path / "report.md"
    assert export_to_markdown([_result()], SEARCH_INFO, [], str(target))
    text = target.read_text(encoding="utf-8")
    assert "# Отчёт DSP Scanner" in text
    assert "договор" in text


@pytest.mark.parametrize("payload", ["=SUM(1+1)", "+1", "-1", "@cmd", "\t=HYPERLINK()"])
def test_formula_injection_is_neutralised(payload):
    """Значение из чужого документа не должно исполняться в Excel."""
    assert spreadsheet_text(payload).startswith("'")


def test_control_characters_are_stripped_for_xml():
    assert "\x00" not in spreadsheet_text("до\x00после")


def test_markdown_escaping_neutralises_html():
    assert "<script>" not in markdown_text("<script>alert(1)</script>")


def test_partial_export_does_not_leave_temp_files(tmp_path):
    target = tmp_path / "report.csv"
    export_to_csv([_result()], str(target))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_output_path_gets_the_right_extension(tmp_path):
    assert resolve_output_path(tmp_path / "report", "csv").suffix == ".csv"
    assert resolve_output_path(tmp_path / "report.txt", "md").suffix == ".md"


def test_directory_output_gets_a_generated_name(tmp_path):
    resolved = resolve_output_path(tmp_path, "xlsx")
    assert resolved.parent == tmp_path
    assert resolved.name.startswith("search_results_")


def test_unknown_format_is_rejected():
    with pytest.raises(ValueError):
        normalize_report_format("exe")
