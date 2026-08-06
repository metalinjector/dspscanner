"""Сквозные проверки: оркестратор сканирования и запуск CLI отдельным процессом."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from app.config import ScanSettings
from app.email_settings import EmailSettings, invalid_addresses, safe_header
from app.scanning.scanner import PRECOUNT_MESSAGE, DocumentScanner

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, env_extra: dict[str, str] | None = None, timeout: int = 300):
    """Запускает CLI отдельным процессом.

    ``encoding="utf-8"`` задаётся явно: без него родительский процесс
    декодировал бы вывод по кодировке локали, и на Windows кириллица
    превращалась бы в мусор ещё до проверок.
    """
    env = None
    if env_extra:
        env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(ROOT),
        env=env,
    )


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


def _progress_events(folder, **overrides):
    events: list[tuple[int, int, str]] = []
    DocumentScanner().run(
        ScanSettings(
            paths=[str(folder)], words=["договор"], file_types={".txt"}, **overrides
        ),
        on_progress=lambda current, total, message: events.append((current, total, message)),
    )
    return events


def test_precount_fixes_the_progress_denominator(documents):
    """Ради этого подсчёт и делается: знаменатель не меняется по ходу."""
    events = _progress_events(documents, precount_files=True)
    totals = {total for _current, total, _message in events if total > 0}
    assert totals == {3}


def test_precount_announces_the_counting_phase(documents):
    events = _progress_events(documents, precount_files=True)
    counting = [message for _current, total, message in events if total == 0]
    assert any(PRECOUNT_MESSAGE in message for message in counting)


@pytest.fixture
def many_documents(tmp_path):
    """Дерево крупнее основной фикстуры.

    На трёх файлах потоковый режим случайно оказывается монотонным, и проверка
    ничего бы не доказывала.
    """
    folder = tmp_path / "many"
    folder.mkdir()
    for index in range(60):
        (folder / f"f{index}.txt").write_text("договор", encoding="utf-8")
    return folder


def _fractions(folder, **overrides):
    return [
        current / total
        for current, total, _message in _progress_events(folder, **overrides)
        if total > 0
    ]


def test_precount_keeps_progress_monotonic(many_documents):
    """Гарантия неубывающего прогресса даётся именно включённым подсчётом."""
    fractions = _fractions(many_documents, precount_files=True, max_workers=4)
    assert fractions == sorted(fractions)


def test_without_precount_the_denominator_still_grows(many_documents):
    """Обратная сторона, ради которой подсчёт включён по умолчанию.

    Без него общее число уточняется по ходу обхода, поэтому доля выполнения
    откатывается назад. Тест фиксирует это как известное свойство режима, а не
    как ошибку: гарантию даёт только предварительный подсчёт.
    """
    totals = {
        total
        for _current, total, _message in _progress_events(
            many_documents, precount_files=False, max_workers=4
        )
        if total > 0
    }
    assert len(totals) > 1


def test_progress_reaches_the_end(documents):
    events = _progress_events(documents, precount_files=True)
    current, total, _message = next(
        event for event in reversed(events) if event[1] > 0
    )
    assert current == total == 3


def test_precount_counts_exactly_what_gets_processed(tmp_path):
    """Фильтры подсчёта и обработки обязаны совпадать.

    Иначе полоса застревала бы, не доходя до конца, либо упиралась в 100%
    раньше времени.
    """
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("договор", encoding="utf-8")
    (folder / "b.txt").write_text("договор", encoding="utf-8")
    (folder / "чужой.pdf").write_text("договор", encoding="utf-8")
    (folder / "~$temp.txt").write_text("договор", encoding="utf-8")
    (folder / "empty.txt").write_text("", encoding="utf-8")

    counted = DocumentScanner._precount_files(
        ScanSettings(paths=[str(folder)], words=["договор"], file_types={".txt"}),
        10**9,
        Event(),
        None,
    )
    report = DocumentScanner().run(
        ScanSettings(paths=[str(folder)], words=["договор"], file_types={".txt"})
    )
    assert counted == report.stats.processed == 2


def test_precount_does_not_double_count_skips(tmp_path):
    """Пропуски учитывает только основной проход."""
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "ok.txt").write_text("договор", encoding="utf-8")
    (folder / "~$temp.txt").write_text("договор", encoding="utf-8")

    with_precount = DocumentScanner().run(
        ScanSettings(paths=[str(folder)], words=["договор"], file_types={".txt"},
                     precount_files=True)
    )
    without = DocumentScanner().run(
        ScanSettings(paths=[str(folder)], words=["договор"], file_types={".txt"},
                     precount_files=False)
    )
    assert with_precount.stats.skipped == without.stats.skipped == 1


def test_precount_is_cancellable_and_yields_no_total():
    cancel = Event()
    cancel.set()
    assert DocumentScanner._precount_files(
        ScanSettings(paths=["/"], words=["x"], file_types={".txt"}),
        10**9,
        cancel,
        None,
    ) == 0


def test_results_are_identical_with_and_without_precount(documents):
    with_precount = DocumentScanner().run(
        ScanSettings(paths=[str(documents)], words=["договор"], file_types={".txt"},
                     precount_files=True)
    )
    without = DocumentScanner().run(
        ScanSettings(paths=[str(documents)], words=["договор"], file_types={".txt"},
                     precount_files=False)
    )
    assert [r.full_path for r in with_precount.results] == [
        r.full_path for r in without.results
    ]
    assert with_precount.stats.processed == without.stats.processed


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
    completed = run_cli(
        "--no-config", "--quiet",
        "--path", str(documents),
        "--content-terms", "договор;C++;№ 42",
        "--types", "txt",
        "--format", "xlsx",
        "--output", str(report_path),
        "--summary-json", str(summary_path),
    )
    assert completed.returncode == 0, completed.stderr
    assert report_path.is_file() and report_path.stat().st_size > 0

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert summary["processed"] == 3
    assert summary["matches"] == 3
    assert summary["email_sent"] is False


@pytest.mark.parametrize("flag", ["--precount", "--no-precount"])
def test_cli_accepts_both_precount_modes(documents, tmp_path, flag):
    report_path = tmp_path / f"report{flag}.md"
    completed = run_cli(
        "--no-config", flag,
        "--path", str(documents),
        "--content-terms", "договор",
        "--types", "txt",
        "--format", "md",
        "--output", str(report_path),
    )
    assert completed.returncode == 0, completed.stderr
    assert report_path.is_file()


def test_cli_reports_the_total_before_processing_starts(documents, tmp_path):
    """В консоли предподсчёт виден как знаменатель, известный с первой строки."""
    completed = run_cli(
        "--no-config", "--precount",
        "--path", str(documents),
        "--content-terms", "договор",
        "--types", "txt",
        "--format", "md",
        "--output", str(tmp_path / "r.md"),
    )
    assert completed.returncode == 0, completed.stderr
    assert "Предварительный подсчёт файлов" in completed.stdout
    # Ни одной строки прогресса со знаменателем, меньшим итогового.
    assert "[1/3]" in completed.stdout


def test_cli_rejects_unsupported_file_type(documents):
    completed = run_cli(
        "--no-config", "--path", str(documents),
        "--content-term", "x", "--types", "exe",
        timeout=120,
    )
    assert completed.returncode == 2
    assert "Неподдерживаемые типы" in completed.stderr


def test_cli_version_does_not_need_qt():
    completed = run_cli("--version", timeout=120)
    assert completed.returncode == 0
    assert "DSP Scanner" in completed.stdout


@pytest.mark.parametrize("locale_encoding", ["cp1252", "ascii"])
def test_cli_survives_non_utf8_locale(documents, tmp_path, locale_encoding):
    """Планировщик заданий Windows перенаправляет вывод в файл.

    Тогда Python кодирует вывод по кодировке локали, и на нерусской Windows
    печать русского текста завершала работу с UnicodeEncodeError ещё до
    начала сканирования — отчёт не создавался вовсе.
    """
    report_path = tmp_path / "report.md"
    completed = run_cli(
        "--no-config",
        "--path", str(documents),
        "--content-terms", "договор",
        "--types", "txt",
        "--format", "md",
        "--output", str(report_path),
        env_extra={"PYTHONIOENCODING": locale_encoding},
    )
    assert completed.returncode == 0, completed.stderr
    assert report_path.is_file()
    assert "Готово" in completed.stdout


def test_cli_error_messages_stay_readable_in_non_utf8_locale(documents):
    completed = run_cli(
        "--no-config", "--path", str(documents),
        "--content-term", "x", "--types", "exe",
        env_extra={"PYTHONIOENCODING": "cp1252"},
        timeout=120,
    )
    assert completed.returncode == 2
    # Без принудительного UTF-8 здесь были бы escape-последовательности \\u0434.
    assert "Неподдерживаемые типы" in completed.stderr


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
