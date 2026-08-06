"""Базовый интерфейс для всех читателей документов."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ReaderOutcome:
    text: Optional[str] = None
    warning: Optional[str] = None
    error: Optional[str] = None
    locked: bool = False
    method_used: str = ""

    @property
    def ok(self) -> bool:
        return self.text is not None and not self.error


class BaseReader(ABC):
    name: str = "base"

    @abstractmethod
    def extract_text(self, path: Path) -> ReaderOutcome:
        ...

    @staticmethod
    def is_file_accessible(path: Path) -> bool:
        try:
            with open(path, "rb"):
                return True
        except OSError:
            return False

    @staticmethod
    def read_bytes(path: Path) -> Optional[bytes]:
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    @staticmethod
    def read_prefix(path: Path, size: int = 4096) -> Optional[bytes]:
        try:
            with open(path, "rb") as fh:
                return fh.read(max(1, size))
        except OSError:
            return None
