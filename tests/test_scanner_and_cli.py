"""Сквозные проверки: оркестратор сканирования и запуск CLI отдельным процессом."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from app.config import ScanSettings
from app.email_settings import EmailSettings, invalid_addresses, safe_header
from app.scanning.scanner import DocumentScanner

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def documents(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text(
        "Служебная записка о договоре № 42 и языке C++", encoding="utf-8"
    )
    (folder / "b.txt").write_text("Ничего интересного", encoding="utf-8")
    (folder / "договор_проект.txt").write_text("пусто", encoding="utf-8")
    return folder


def _run(folder, **overrides):
    settings = ScanSettings(
        paths=[str(folder)], file_types={".txt"}, max_workers=2, **overrides
    )
    return DocumentScanner().run(settings)


def test_scan_finds_content_matches(documents):
    report = _run(documents, words=["договор"])
    assert report.stats.processed == 3
    assert [r.file_name for r in report.results] == ["a.txt"]
    assert report.stats.errors == 0


def test_scan_finds_filename_matches_without_opening_files(documents):
    report = _run(documents, filename_words=["договор"])
    assert [r.file_name for r in report.results] == ["договор_проект.txt"]
    assert report.results[0].context.startswith("[Имя файла]")


def test_scan_reports_validation_errors_instead_of_crashing(tmp_path):
    report = DocumentScanner().run(ScanSettings(paths=[], words=[]))
    assert report.stats.errors > 0
    assert report.results == []


def test_cancelled_scan_is_marked_as_such(documents):
    cancel = Event()
    cancel.set()
    report = DocumentScanner().run(
        ScanSettings(paths=[str(documents)], words=["договор"], file_types={".txt"}),
        cancel_event=cancel,
    )
    assert report.cancelled is True


def test_progress_and_log_callbacks_fire(documents):
    seen_progress, seen_logs = [], []
    DocumentScanner().run(
        ScanSettings(paths=[str(documents)], words=["договор"], file_types={".txt"}),
        on_progress=lambda current, total, message: seen_progress.append(current),
        on_log=lambda level, message: seen_logs.append(level),
    )
    assert seen_progress


def test_cli_end_to_end_creates_report_and_summary(documents, tmp_path):
    """Главная гарантия для нового ПК: программа запускается и отдаёт отчёт."""
    report_path = tmp_path / "report.xlsx"
    summary_path = tmp_path / "summary.json"
    completed = subprocess.run(
        [
            sys.executable, str(ROOT / "main.py"), "--cli", "--no-config", "--quiet",
            "--path", str(documents),
            "--content-terms", "договор;C++;№ 42",
            "--types", "txt",
            "--format", "xlsx",
            "--output", str(report_path),
            "--summary-json", str(summary_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    assert report_path.is_file() and report_path.stat().st_size > 0

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert summary["processed"] == 3
    assert summary["matches"] == 3
    assert summary["email_sent"] is False


def test_cli_rejects_unsupported_file_type(documents):
    completed = subprocess.run(
        [
            sys.executable, str(ROOT / "main.py"), "--cli", "--no-config",
            "--path", str(documents), "--content-term", "x", "--types", "exe",
        ],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT),
    )
    assert completed.returncode == 2
    assert "Неподдерживаемые типы" in completed.stderr


def test_cli_version_does_not_need_qt():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--cli", "--version"],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT),
    )
    assert completed.returncode == 0
    assert "DSP Scanner" in completed.stdout


def test_smtp_header_injection_is_blocked():
    assert "\n" not in safe_header("Тема\nBcc: чужой@example.com")


def test_invalid_email_addresses_are_detected():
    assert invalid_addresses("не-адрес") == ["не-адрес"]
    assert invalid_addresses("user@example.com") == []


def test_email_password_is_not_saved_unless_allowed():
    settings = EmailSettings(password="секрет", save_password=False)
    assert settings.to_mapping()["password"] == ""
    assert EmailSettings(password="секрет", save_password=True).to_mapping()["password"] == "секрет"


def test_email_password_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("DSP_SCANNER_SMTP_PASSWORD", "из-окружения")
    assert EmailSettings().effective_password() == "из-окружения"
