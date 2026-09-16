"""DSP Scanner — точка входа GUI и командного интерфейса."""
from __future__ import annotations

import faulthandler
import multiprocessing
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Sequence

# Пакет app доступен независимо от текущей рабочей директории.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Дескриптор журнала аварийных дампов держим открытым до конца процесса:
# faulthandler пишет в него напрямую, а закрытие файла уничтожило бы дамп.
_FAULT_LOG_HANDLE = None


def _enable_faulthandler() -> Path | None:
    """Включает faulthandler поверх файла, а не stderr.

    В оконной сборке (``console=False``) у процесса нет stderr: ``sys.stderr``
    равен ``None``, и вызов ``faulthandler.enable()`` без аргументов падает с
    RuntimeError — раньше это молча глоталось, и падение не оставляло следа
    нигде. Путь можно переопределить переменной ``DSP_SCANNER_FAULT_LOG``.
    """
    global _FAULT_LOG_HANDLE
    candidates: list[Path] = []
    override = os.environ.get("DSP_SCANNER_FAULT_LOG")
    if override:
        candidates.append(Path(override).expanduser())
    else:
        try:
            from app.config import LOG_DIR

            candidates.append(Path(LOG_DIR) / "faulthandler.log")
        except Exception:
            pass

    for target in candidates:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _FAULT_LOG_HANDLE = open(target, "a", encoding="utf-8")
        except OSError:
            continue
        try:
            faulthandler.enable(file=_FAULT_LOG_HANDLE, all_threads=True)
        except Exception:
            try:
                _FAULT_LOG_HANDLE.close()
            except OSError:
                pass
            _FAULT_LOG_HANDLE = None
            continue
        return target

    # Запасной вариант: консольная сборка и запуск из терминала.
    if sys.stderr is not None:
        try:
            faulthandler.enable(file=sys.stderr, all_threads=True)
        except Exception:
            return None
    return None


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
    # Перехват аппаратных сбоев (SIGSEGV, SIGABRT и т.п.) включается первым
    # делом: падение на импорте Qt тоже должно оставить трассировку в файле.
    fault_log = _enable_faulthandler()
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
    if fault_log is not None:
        logger.info("Аварийные дампы процесса: %s", fault_log)
    else:
        logger.warning("Не удалось включить faulthandler: дампов падений не будет")

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

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        logger.critical("Необработанное исключение в потоке %s:\n%s", args.thread.name, text)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    window = MainWindow(logger=logger, qt_log_handler=qt_log_handler)
    window.show()
    exit_code = app.exec()
    # Завершение интерпретатора разрушает оставшиеся объекты Python, и живой
    # QThread в этот момент валит процесс через abort (0xC0000409). Поэтому
    # дожидаемся воркеров сразу после выхода из цикла событий.
    try:
        window.shutdown_workers()
    except Exception:
        logger.exception("Не удалось дождаться фоновых потоков при выходе")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
