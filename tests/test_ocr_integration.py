"""Opt-in real OCR matrix: all official models are required, no silent skips.

DSP_TEST_TESSERACT=/path/to/tesseract DSP_TEST_OCR_MATRIX=1 python -m pytest tests/test_ocr_integration.py
Install models first: python tools/install_tesseract.py --root /path/to/portable
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import ScanSettings
from app.readers import pdf_reader as pdf
from app.scanning.scanner import DocumentScanner
from app.settings_store import AppSettings
from tools.benchmark_ocr import synthetic_document

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DSP_TEST_TESSERACT") and os.environ.get("DSP_TEST_OCR_MATRIX") == "1"),
    reason="set DSP_TEST_TESSERACT and DSP_TEST_OCR_MATRIX=1 (six official models required)",
)


@pytest.fixture
def scanned_pdf(tmp_path):
    path = tmp_path / "mixed-scan.pdf"
    with synthetic_document(1, "mixed") as document:
        assert not document[0].get_text()
        document.save(path)
    return path


@pytest.mark.parametrize("tier", ["fast", "medium", "best"])
@pytest.mark.parametrize("russian_only", [True, False])
def test_real_pdf_ocr_all_model_and_language_modes(scanned_pdf, monkeypatch, tier, russian_only):
    binary = str(Path(os.environ["DSP_TEST_TESSERACT"]).resolve())
    settings = AppSettings(tesseract_path=binary, ocr_model_tier=tier, russian_only=russian_only)
    monkeypatch.setattr(pdf, "load_settings", lambda: settings)
    directory = pdf._ocr_tessdata_dir(binary, tier)
    assert directory and directory.name == f"tessdata-{tier}", "requested tier is not installed"
    assert (directory / "rus.traineddata").is_file()
    assert (directory / "eng.traineddata").is_file()
    outcome = pdf.PdfReader().extract_text(scanned_pdf)
    assert not outcome.error and not outcome.warning, outcome
    assert outcome.method_used == "pymupdf+ocr"
    text = outcome.text.casefold()
    assert text.count("договор") == 5
    assert text.count("документов") == 5
    if not russian_only:
        assert text.count("contract") == 5


def test_real_ocr_through_spawn_scanner(scanned_pdf, monkeypatch):
    monkeypatch.setenv("DSP_SCANNER_TESSERACT_PATH", str(Path(os.environ["DSP_TEST_TESSERACT"]).resolve()))
    monkeypatch.setenv("DSP_SCANNER_OCR_MODEL_TIER", "fast")
    monkeypatch.setenv("DSP_SCANNER_RUSSIAN_ONLY", "false")
    report = DocumentScanner().run(ScanSettings(
        paths=[str(scanned_pdf)], words=["договор", "contract"], file_types={".pdf"}, max_workers=1,
    ))
    assert report.stats.processed == 1
    assert not report.errors, report.errors
    assert len(report.results) == 10
    assert {item.word for item in report.results} == {"договор", "contract"}
