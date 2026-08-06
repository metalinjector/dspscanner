"""Фоновые QThread для длительных операций."""
from __future__ import annotations

from datetime import datetime
import socket
from threading import Event
from typing import Mapping, Sequence

from PySide6.QtCore import QThread, Signal

from app.config import ScanReport, ScanSettings
from app.email_sender import send_email
from app.email_settings import EmailSettings
from app.reporting import export_report
from app.fileops.file_operations import copy_found_files, secure_delete_files, secure_move_files
from app.scanning.scanner import DocumentScanner


class ScanWorker(QThread):
    progress = Signal(int, int, str)
    results_found = Signal(list)
    log_message = Signal(str, str)
    finished_ok = Signal(object)
    failed = Signal(str)
    stats_update = Signal(object)  # промежуточная статистика во время сканирования

    def __init__(self, settings: ScanSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.cancel_event = Event()
        self._scanner = DocumentScanner()

    def cancel(self) -> None:
        self.cancel_event.set()

    def _on_results(self, results) -> None:
        """Немедленно передаёт совпадения завершённого файла в GUI."""
        if results:
            self.results_found.emit(list(results))

    def run(self) -> None:
        try:
            report: ScanReport = self._scanner.run(
                self.settings,
                cancel_event=self.cancel_event,
                on_progress=lambda cur, total, msg: self.progress.emit(cur, total, msg),
                on_results=self._on_results,
                on_log=lambda level, msg: self.log_message.emit(level, msg),
                on_stats=lambda stats: self.stats_update.emit(stats),
            )
            self.finished_ok.emit(report)
        except BaseException as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class CopyWorker(QThread):
    finished_ok = Signal(list)
    failed = Signal(str)

    def __init__(self, unique_paths: Sequence[str], destination: str, parent=None):
        super().__init__(parent)
        self.unique_paths = list(unique_paths)
        self.destination = destination

    def run(self) -> None:
        try:
            self.finished_ok.emit(copy_found_files(self.unique_paths, self.destination))
        except Exception as exc:
            self.failed.emit(str(exc))


class SecureOpWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, mode: str, paths: Sequence[str], destination: str = "", passes: int = 3, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.paths = list(paths)
        self.destination = destination
        self.passes = passes

    def run(self) -> None:
        try:
            if self.mode == "delete":
                result = secure_delete_files(self.paths, self.passes)
            else:
                result = secure_move_files(self.paths, self.destination, self.passes)
            self.finished_ok.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class EmailReportWorker(QThread):
    """Экспортирует отчёт и отправляет его по SMTP, не блокируя GUI."""

    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        report: ScanReport,
        search_info: Mapping[str, object],
        email_settings: EmailSettings,
        parent=None,
    ):
        super().__init__(parent)
        self.report = report
        self.search_info = dict(search_info)
        self.email_settings = email_settings.sanitized()

    def run(self) -> None:
        try:
            artifacts = export_report(
                self.report,
                self.search_info,
                self.email_settings.report_format,
            )
            values = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "matches": len(self.report.results),
                "processed": self.report.stats.processed,
                "errors": len(self.report.errors),
                "elapsed": f"{self.report.elapsed_seconds:.1f}",
                "host": socket.gethostname(),
                "status": "отменено" if self.report.cancelled else "завершено",
                "report_format": self.email_settings.report_format,
            }
            sent = send_email(
                self.email_settings,
                template_values=values,
                attachments=artifacts.attachments,
            )
            self.finished_ok.emit({"sent": sent, "artifacts": artifacts})
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
