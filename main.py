"""DSP Scanner — точка входа GUI и командного интерфейса."""
from __future__ import annotations

import multiprocessing
import sys
import traceback
from pathlib import Path
from typing import Sequence

# Пакет app доступен независимо от текущей рабочей директории.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _set_windows_app_id() -> None:
    """Даёт приложению собственную иконку/группу на панели задач Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        from app.config import APP_VERSION

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"BytskoDmitry.DSPScanner.{APP_VERSION.rsplit('.', 1)[0]}"
        )
    except (AttributeError, OSError, ImportError):
        pass


def _extract_cli_args(argv: Sequence[str]) -> list[str] | None:
    """Возвращает аргументы CLI либо ``None``, если нужно открыть GUI."""
    items = list(argv)
    if items and items[0].lower() == "cli":
        return items[1:]
    if "--cli" in items:
        copy = list(items)
        copy.remove("--cli")
        return copy
    return None


def main(argv: Sequence[str] | None = None) -> int:
    # Важно для spawn-процессов и будущей сборки через PyInstaller.
    multiprocessing.freeze_support()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    cli_args = _extract_cli_args(raw_args)
    if cli_args is not None:
        # Ни один Qt-модуль не импортируется в headless/серверном режиме.
        from app.cli import run_cli

        return run_cli(cli_args)

    _set_windows_app_id()

    # Тяжёлые Qt-импорты находятся внутри main(): дочерние процессы чтения
    # PDF/DOC при spawn и CLI-режим не загружают весь GUI.
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.gui.main_window import MainWindow
    from app.gui.theme import apply_modern_theme
    from app.logging_utils import setup_logger
    from app.settings_store import ensure_portable_tesseract_dir

    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    app = QApplication([sys.argv[0], *raw_args])
    app.setApplicationName("DSP Scanner")
    app.setOrganizationName("DSPScanner")
    apply_modern_theme(app)

    logger, qt_log_handler = setup_logger()
    logger.info("=== DSP Scanner запущен ===")

    tesseract_dir = ensure_portable_tesseract_dir()
    if tesseract_dir is not None:
        logger.debug("Переносимая папка Tesseract: %s", tesseract_dir)
    else:
        logger.warning(
            "Не удалось создать папку Tesseract-OCR рядом с программой; "
            "будут проверены PATH и стандартные каталоги установки"
        )

    def excepthook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.critical("Необработанное исключение:\n%s", text)
        try:
            QMessageBox.critical(
                None,
                "Критическая ошибка",
                f"{exc_value}\n\nПодробности в журнале.",
            )
        except Exception:
            return

    sys.excepthook = excepthook
    window = MainWindow(logger=logger, qt_log_handler=qt_log_handler)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
