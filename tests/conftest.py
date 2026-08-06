"""Общая обвязка тестов.

Тесты не должны зависеть от установленных LibreOffice/Tesseract/MS Word и от
графической сессии: Qt поднимается в offscreen-режиме, а внешние программы
явно отключаются переменными окружения. Пользовательские настройки в
``~/DSPScanner`` не изменяются — тесты только читают их.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

# До первого импорта PySide6: иначе Qt попытается открыть настоящий дисплей.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WORD_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
CONTENT_TYPES = (
    '<?xml version="1.0"?><Types '
    'xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Уводит переносимую конфигурацию в tmp, чтобы тесты не писали в проект."""
    monkeypatch.setenv("DSP_SCANNER_CONFIG_DIR", str(tmp_path / "config"))
    from app.settings_store import invalidate_settings_cache

    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


def make_docx(path: Path, body_text: str | None = None, altchunk_html: str | None = None) -> Path:
    """Собирает минимальный, но настоящий DOCX-контейнер."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        if body_text is not None:
            archive.writestr(
                "word/document.xml",
                f'<?xml version="1.0"?><w:document {WORD_NS}><w:body>'
                f"<w:p><w:r><w:t>{body_text}</w:t></w:r></w:p>"
                "</w:body></w:document>",
            )
        if altchunk_html is not None:
            archive.writestr(
                "word/afchunk.mht",
                "MIME-Version: 1.0\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "Content-Transfer-Encoding: 8bit\r\n\r\n"
                f"<html><body><p>{altchunk_html}</p></body></html>\r\n",
            )
    return path


def supports_symlinks(directory: Path) -> bool:
    """Windows без прав/Developer Mode не даёт создавать ссылки."""
    probe = directory / "_symlink_probe"
    target = directory / "_symlink_target"
    try:
        target.write_text("x", encoding="utf-8")
        os.symlink(target, probe)
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        probe.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
    return True


requires_symlinks = pytest.mark.skipif(
    not supports_symlinks(Path(os.environ.get("TMPDIR", "/tmp"))),
    reason="в этой системе нельзя создавать символьные ссылки",
)
