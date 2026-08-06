"""Логирование приложения в файл и GUI."""
from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime
from typing import Tuple

from PySide6.QtCore import QObject, Signal

from app.config import LOG_DIR, LOG_FILE

LOGGER_NAME = "dspscanner"


class QtLogHandler(logging.Handler, QObject):
    new_record = Signal(str, str)

    def __init__(self) -> None:
        logging.Handler.__init__(self)
        QObject.__init__(self)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.new_record.emit(record.levelname, self.format(record))
        except Exception:
            self.handleError(record)


def setup_logger() -> Tuple[logging.Logger, QtLogHandler]:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for handler in logger.handlers:
        try:
            handler.close()
        except Exception:
            pass
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
    except OSError:
        # Приложение должно запускаться даже при недоступной домашней папке.
        pass

    qt_handler = QtLogHandler()
    qt_handler.setFormatter(formatter)
    qt_handler.setLevel(logging.DEBUG)
    logger.addHandler(qt_handler)
    return logger, qt_handler


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
