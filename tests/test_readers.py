"""Читатели документов: DOCX (в т.ч. altChunk), TXT-кодировки, защита контейнера."""
from __future__ import annotations

import zipfile

from app.config import ScanSettings
from app.readers import extract_text
from app.readers.docx_reader import DocxReader
from app.readers.txt_reader import TxtReader

from conftest import CONTENT_TYPES, make_docx


def test_docx_plain_document(tmp_path):
    path = make_docx(tmp_path / "plain.docx", body_text="Просто договор")
    outcome = DocxReader().extract_text(path)
    assert outcome.error is None
    assert outcome.text == "Просто договор"
    assert outcome.method_used == "manual-xml"


def test_docx_altchunk_is_merged_with_ordinary_text(tmp_path):
    """Импортированный раздел лежит в отдельной MHTML-части.

    Без склейки он терялся в любом документе, где есть хотя бы один
    обычный абзац.
    """
    path = make_docx(
        tmp_path / "mixed.docx",
        body_text="ОБЫЧНЫЙ титульный текст",
        altchunk_html="ALTCHUNK_UNIQUE_TARGET внутри импортированного раздела",
    )
    outcome = DocxReader().extract_text(path)
    assert "ОБЫЧНЫЙ титульный текст" in outcome.text
    assert "ALTCHUNK_UNIQUE_TARGET" in outcome.text
    assert outcome.method_used == "manual-xml+altchunk"


def test_docx_with_only_altchunk_still_readable(tmp_path):
    path = make_docx(tmp_path / "only.docx", altchunk_html="ALTCHUNK_UNIQUE_TARGET")
    outcome = DocxReader().extract_text(path)
    assert "ALTCHUNK_UNIQUE_TARGET" in outcome.text
    assert outcome.method_used == "altchunk-mht-parser"


def test_broken_container_reports_error_instead_of_raising(tmp_path):
    path = tmp_path / "bad.docx"
    path.write_bytes(b"not a zip at all")
    outcome = DocxReader().extract_text(path)
    assert outcome.error and outcome.text is None


def test_zip_bomb_is_rejected_before_unpacking(tmp_path):
    """Маленький архив не должен разворачиваться в гигабайты."""
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("word/document.xml", "\0" * (200 * 1024 * 1024))
    outcome = DocxReader().extract_text(path)
    assert outcome.error is not None


def test_txt_utf8_with_bom(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes("﻿Договор".encode("utf-8"))
    outcome = TxtReader().extract_text(path)
    assert outcome.text == "Договор"
    assert outcome.method_used == "utf-8-bom"


def test_txt_utf16_le_is_not_confused_with_utf32(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes("﻿Договор".encode("utf-16-le"))
    assert TxtReader().extract_text(path).text == "Договор"


def test_txt_cp1251_without_bom(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes("Служебная записка".encode("cp1251"))
    assert "записка" in TxtReader().extract_text(path).text


def test_unsupported_extension_is_reported(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"x")
    assert "Неподдерживаемое расширение" in extract_text(path, ".zip", ScanSettings()).error


def test_dispatch_by_extension(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("договор", encoding="utf-8")
    assert extract_text(path, ".txt", ScanSettings()).text == "договор"
