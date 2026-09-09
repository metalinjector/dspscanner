"""Opt-in real Tesseract checks. DSP_TEST_TESSDATA must contain rus and eng."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import fitz
import pytest

from app.config import ScanSettings
from app.readers.pdf_reader import PdfReader
from app.scanning.scanner import DocumentScanner

pytestmark = pytest.mark.skipif(not os.environ.get('DSP_TEST_TESSDATA'), reason='real OCR models not configured')


@pytest.fixture
def real_ocr(monkeypatch):
    binary = shutil.which('tesseract')
    assert binary, 'Tesseract required for integration tests'
    for key, value in {'TESSDATA_PREFIX': os.environ['DSP_TEST_TESSDATA'],
                       'DSP_SCANNER_TESSERACT_PATH': binary, 'DSP_SCANNER_RUSSIAN_ONLY': 'false',
                       'DSP_SCANNER_OCR_QUALITY': 'adaptive', 'DSP_SCANNER_OCR_MODEL_TIER': 'fast',
                       'DSP_SCANNER_OCR_WORKERS': '2', 'DSP_SCANNER_OCR_PAGE_TIMEOUT': '30',
                       'DSP_SCANNER_PDF_TIMEOUT': '120', 'DSP_SCANNER_OCR_FORCE': 'false'}.items():
        monkeypatch.setenv(key, value)


def make_scan(path, *, mixed=False, pages=1):
    font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    assert font_path.exists(), 'DejaVu Sans required for Cyrillic fixture'
    with fitz.open() as source:
        page = source.new_page(width=595, height=842)
        page.insert_font(fontname='dspfont', fontfile=str(font_path))
        page.insert_text((40, 100), 'Время проверки договора', fontsize=18, fontname='dspfont')
        page.insert_text((40, 150), 'OCR TARGET ALPHA', fontsize=18, fontname='dspfont')
        image = page.get_pixmap(dpi=300, colorspace=fitz.csGRAY).tobytes('png')
    with fitz.open() as doc:
        for _ in range(pages):
            page = doc.new_page(width=595, height=842)
            page.insert_image(page.rect, stream=image)
            if mixed:
                page.insert_text((30, 25), 'HEADER')
        doc.save(path)
    return path


@pytest.mark.parametrize('mixed', [False, True])
def test_real_russian_english_image_text(tmp_path, real_ocr, mixed):
    path = make_scan(tmp_path / 'scan.pdf', mixed=mixed)
    result = PdfReader().extract_text(path)
    assert result.error is None and result.text
    assert 'время' in result.text.casefold() and 'TARGET' in result.text
    if mixed:
        assert 'HEADER' in result.text


def test_spawn_pipeline_and_second_search_cache(tmp_path, real_ocr):
    path = make_scan(tmp_path / 'scan.pdf', pages=2)
    scan = ScanSettings(paths=[str(path)], words=['время'], file_types={'.pdf'})
    first = DocumentScanner().run(scan)
    assert len(first.results) == 2 and not first.stats.errors, first.errors
    assert first.stats.cache_hits == 0
    scan.words = ['TARGET']
    second = DocumentScanner().run(scan)
    assert len(second.results) == 2 and not second.stats.errors, second.errors
    assert second.stats.cache_hits == 1, second.errors
