"""Regression tests for OCR decisions, isolation, scheduling and cache correctness."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from app.config import FileEntry, ScanSettings
from app.readers import pdf_reader as pdf
from app.readers.base import ReaderOutcome
from app.scanning import pdf_cache, scanner
from app.settings_store import AppSettings, _apply_environment_overrides


@pytest.fixture
def settings(monkeypatch):
    # Регрессионные тесты в этом файле описывают поведение adaptive-режима;
    # качество задаётся явно, т.к. дефолт приложения теперь a2fast.
    value = AppSettings(tesseract_path=sys.executable, ocr_quality='adaptive')
    monkeypatch.setattr(pdf, 'load_settings', lambda: value)
    monkeypatch.setattr(pdf, '_resolve_ocr_lang', lambda *args: 'rus')
    monkeypatch.setattr(pdf, '_ocr_tessdata_dir', lambda *args: Path('/models/tessdata-fast'))
    monkeypatch.setattr(pdf, '_OCR_LIMIT', 3)
    monkeypatch.setattr(pdf, '_PROGRESS_CALLBACK', None)
    monkeypatch.setattr(pdf, '_CAPACITY_CALLBACK', None)
    return value


def good(text='Распознанный текст страницы'):
    return pdf.OcrResult(text, 96, 0, 4, 20)


class FakePage:
    rect = SimpleNamespace(width=595., height=842.)

    def __init__(self, index, calls):
        self.index, self.calls = index, calls

    def get_pixmap(self, *, dpi, colorspace, alpha, clip):
        self.calls.append((self.index, dpi, threading.get_ident(), colorspace.n, alpha))
        return SimpleNamespace(tobytes=lambda fmt: f'{self.index}:{dpi}'.encode())


class FakeDocument:
    def __init__(self):
        self.calls = []

    def __getitem__(self, index):
        return FakePage(index, self.calls)


def install_ocr(monkeypatch, function):
    def wrapped(binary, png, language, tier, timeout, psm=3):
        if png.startswith(b'\x89PNG'):  # tiny model preflight
            return pdf.OcrResult('')
        index, dpi = map(int, png.decode().split(':'))
        return function(index, dpi, psm)
    monkeypatch.setattr(pdf, '_ocr_image', wrapped)


def test_rendering_stays_on_owner_thread_and_preserves_order(settings, monkeypatch):
    doc = FakeDocument()
    install_ocr(monkeypatch, lambda i, dpi, psm: good(str(i)))
    texts, used, warning = pdf.PdfReader._ocr_pages(doc, [0, 1, 2], 'input')
    assert texts == {0: '0', 1: '1', 2: '2'} and used and warning is None
    assert all(tid == threading.get_ident() and colors == 1 and not alpha for _, _, tid, colors, alpha in doc.calls)


def test_failed_page_reports_partial_success(settings, monkeypatch):
    def recognize(i, dpi, psm):
        if i == 1:
            raise subprocess.TimeoutExpired('tesseract', 60)
        return good()
    install_ocr(monkeypatch, recognize)
    texts, used, warning = pdf.PdfReader._ocr_pages(FakeDocument(), [0, 1], 'input')
    assert texts[0] and not texts[1] and used
    assert 'стр. 2' in warning and 'OCR' in warning


def test_long_bad_text_retries_and_shorter_good_text_wins(settings, monkeypatch):
    doc = FakeDocument()
    install_ocr(monkeypatch, lambda i, dpi, psm: pdf.OcrResult('ошибка ' * 100, 20, 1, 100, 8)
                if dpi == 200 else good('искомое слово'))
    texts, _, warning = pdf.PdfReader._ocr_pages(doc, [0], 'input')
    assert texts[0] == 'искомое слово' and warning is None
    assert [dpi for _, dpi, *_ in doc.calls] == [200, 300]


def test_retry_failure_retains_text_and_warning(settings, monkeypatch):
    def recognize(i, dpi, psm):
        if dpi == 300:
            raise RuntimeError('failed retry')
        return pdf.OcrResult('первый результат', 50, .2, 2, 10)
    install_ocr(monkeypatch, recognize)
    texts, used, warning = pdf.PdfReader._ocr_pages(FakeDocument(), [0], 'input')
    assert texts[0] == 'первый результат' and used and 'failed retry' in warning


def test_blank_page_has_bounded_retry_and_warning(settings, monkeypatch):
    doc, calls = FakeDocument(), []
    def recognize(i, dpi, psm):
        calls.append(psm)
        return pdf.OcrResult('')
    install_ocr(monkeypatch, recognize)
    texts, used, warning = pdf.PdfReader._ocr_pages(doc, [0], 'input')
    assert len(doc.calls) == 2 and calls == [3, 11]
    assert not used and not texts[0] and 'полнота не подтверждена' in warning


def test_thorough_starts_at_300(settings, monkeypatch):
    settings.ocr_quality = 'thorough'
    doc = FakeDocument()
    install_ocr(monkeypatch, lambda *args: good())
    pdf.PdfReader._ocr_pages(doc, [0], 'input')
    assert [dpi for _, dpi, *_ in doc.calls] == [300]


def test_a2fast_single_pass_250_dpi_no_retry(settings, monkeypatch):
    # A2fast (алгоритм 2.9.7): один проход на 250 DPI; низкая уверенность
    # сама по себе не вызывает повторного распознавания.
    doc, calls = FakeDocument(), []
    def recognize(i, dpi, psm):
        calls.append(dpi)
        return pdf.OcrResult('текст', 70, 0, 3, 15)
    install_ocr(monkeypatch, recognize)
    settings.ocr_quality = 'a2fast'
    texts, used, warning = pdf.PdfReader._ocr_pages(doc, [0], 'input')
    assert calls == [250] and used and texts[0] == 'текст'


def test_a2fast_skips_pages_with_text_layer(settings, monkeypatch):
    # Страница с нормальным текстовым слоем не распознаётся вовсе;
    # пустая страница — распознаётся.
    path_calls = []
    monkeypatch.setattr(pdf.PdfReader, '_ocr_pages',
                        lambda self, doc, idx, name, **kw: path_calls.extend(idx) or ({}, False, None))
    settings.ocr_quality = 'a2fast'
    import fitz as _fitz
    with _fitz.open() as tmp:
        page_text = tmp.new_page()
        page_text.insert_text((20, 20), 'HEADER 1')
        page_blank = tmp.new_page()
        pix = _fitz.Pixmap(_fitz.csGRAY, _fitz.IRect(0, 0, 50, 50), False)
        pix.clear_with(255)
        page_blank.insert_image(_fitz.Rect(20, 50, 400, 700), stream=pix.tobytes('png'))
        tmp.save('a2fast_check.pdf')
    outcome = pdf.PdfReader().extract_text(Path('a2fast_check.pdf'))
    assert 'HEADER 1' in outcome.text
    assert path_calls == [1], 'OCR должен получить только страницу без текста'
    Path('a2fast_check.pdf').unlink(missing_ok=True)


def test_render_error_does_not_drop_other_pages(settings, monkeypatch):
    class Broken(FakeDocument):
        def __getitem__(self, index):
            if index == 1:
                raise ValueError('broken page')
            return super().__getitem__(index)
    install_ocr(monkeypatch, lambda *args: good())
    texts, _, warning = pdf.PdfReader._ocr_pages(Broken(), [0, 1, 2], 'input')
    assert texts[0] and texts[2] and 'стр. 2' in warning


def test_mixed_pdf_preserves_native_text_and_ocr_image(tmp_path, settings, monkeypatch):
    path = tmp_path / 'mixed.pdf'
    pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 100, 100), False)
    pix.clear_with(255)
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((20, 20), 'HEADER 1')
        page.insert_image(fitz.Rect(20, 50, 500, 700), stream=pix.tobytes('png'))
        doc.save(path)
    monkeypatch.setattr(pdf, '_ocr_image', lambda *args: good('TARGET'))
    outcome = pdf.PdfReader().extract_text(path)
    assert 'HEADER 1' in outcome.text and 'TARGET' in outcome.text
    assert outcome.method_used == 'pymupdf+ocr'


def test_force_ocr_and_page_limit(tmp_path, settings, monkeypatch):
    path = tmp_path / 'text.pdf'
    with fitz.open() as doc:
        for text in ('FIRST', 'SECOND'):
            page = doc.new_page()
            page.insert_text((20, 20), text)
        doc.save(path)
    monkeypatch.setattr(pdf, '_ocr_image', lambda *args: good('EXTRA'))
    assert 'EXTRA' not in pdf.PdfReader().extract_text(path).text
    settings.ocr_force = True
    limited = pdf.PdfReader().extract_text(path, max_pages=1)
    assert 'FIRST' in limited.text and 'EXTRA' in limited.text and 'SECOND' not in limited.text


def test_merge_does_not_duplicate_text():
    assert pdf._merge_native_ocr('Header\nA native sentence.', 'Header\nA native sentence.\nNew text') == 'Header\nA native sentence.\n\nNew text'
    assert pdf._looks_garbled('(cid:123)')


@pytest.mark.parametrize('raw,expected', [('0', False), ('false', False), ('False', False), ('off', False),
                                         ('1', True), ('true', True), ('YES', True)])
def test_bool_environment(monkeypatch, raw, expected):
    monkeypatch.setenv('DSP_SCANNER_RUSSIAN_ONLY', raw)
    monkeypatch.setenv('DSP_SCANNER_OCR_FORCE', raw)
    value = _apply_environment_overrides(AppSettings())
    assert value.russian_only is expected and value.ocr_force is expected


def test_model_paths_and_openmp(tmp_path, monkeypatch):
    binary = tmp_path / 'bin' / 'tesseract'
    binary.parent.mkdir()
    binary.touch()
    monkeypatch.delenv('TESSDATA_PREFIX', raising=False)
    assert pdf._ocr_tessdata_dir(str(binary), 'fast') is None
    assert 'TESSDATA_PREFIX' not in pdf._tesseract_environment(str(binary))
    explicit = tmp_path / 'explicit data'
    monkeypatch.setenv('TESSDATA_PREFIX', explicit.as_posix())
    assert Path(pdf._tesseract_environment(str(binary))['TESSDATA_PREFIX']) == explicit
    tier = tmp_path / 'tessdata-best'
    tier.mkdir()
    (tier / 'eng.traineddata').touch()
    assert pdf._ocr_tessdata_dir(str(binary), 'best') == tier
    monkeypatch.setenv('OMP_THREAD_LIMIT', '16')
    assert pdf._tesseract_environment(str(binary))['OMP_THREAD_LIMIT'] == '1'


def test_missing_language_is_not_silently_replaced(monkeypatch):
    monkeypatch.setattr(pdf, '_run_tesseract', lambda *a, **k: subprocess.CompletedProcess([], 0, b'List of available languages (1):\neng\n', b''))
    with pytest.raises(RuntimeError, match='rus'):
        pdf._resolve_ocr_lang(sys.executable, True)


def test_preflight_avoids_rendering_with_bad_models(settings, monkeypatch):
    def fail(*args):
        raise RuntimeError('corrupt traineddata')
    monkeypatch.setattr(pdf, '_ocr_image', fail)
    doc = FakeDocument()
    texts, used, warning = pdf.PdfReader._ocr_pages(doc, [0, 1, 2], 'input')
    assert not doc.calls and not texts and not used and 'corrupt traineddata' in warning


def test_tsv_confidence_and_literal_quotes():
    result = pdf._parse_tsv(b'level\tblock_num\tpar_num\tline_num\theight\tconf\ttext\n5\t1\t1\t1\t20\t95\t"Hello"\n5\t1\t1\t1\t20\t90\tworld\n5\t1\t2\t1\t20\t40\tNext\n')
    assert result.text == '"Hello" world\n\nNext'
    assert result.low_fraction == pytest.approx(1/3) and result.needs_retry
    with pytest.raises(ValueError):
        pdf._parse_tsv(b'plain text instead of tsv')


def test_safe_dpi():
    page = SimpleNamespace(rect=SimpleNamespace(width=10000, height=10000))
    assert (10000 * pdf._safe_ocr_dpi(page) / 72) ** 2 <= pdf._OCR_MAX_PIXELS
    page.rect.width = float('inf')
    with pytest.raises(ValueError):
        pdf._safe_ocr_dpi(page)


def test_cpu_reservation_is_recoverable():
    budget, cancel = scanner._OcrBudget(8), threading.Event()
    slots = budget.acquire(cancel)
    assert slots == 8
    cancel.set()
    assert budget.acquire(cancel) == 0
    budget.release(slots)
    assert budget.available == 8


def test_scheduler_refills_before_slow_page_finishes(settings, monkeypatch):
    monkeypatch.setattr(pdf, '_OCR_LIMIT', 2)
    third_started = threading.Event()
    def recognize(i, dpi, psm):
        if i == 0:
            assert third_started.wait(3), 'chunk barrier prevented page 3 from starting'
        if i == 2:
            third_started.set()
        return good(str(i))
    install_ocr(monkeypatch, recognize)
    texts, _, warning = pdf.PdfReader._ocr_pages(FakeDocument(), [0, 1, 2], 'input')
    assert warning is None and texts == {0: '0', 1: '1', 2: '2'}


def test_timeout_keeps_partial_matches_and_releases_budget(tmp_path, monkeypatch):
    path = tmp_path / 'input.pdf'
    path.write_bytes(b'%PDF')
    entry = FileEntry(path, path.stat().st_size, path.stat().st_mtime, '.pdf')
    scan = ScanSettings(paths=[str(path)], words=['TARGET'])
    messages = [('page', (0, 'TARGET partial result'))]
    class Connection:
        def poll(self, timeout):
            return bool(messages)
        def recv(self):
            return messages.pop(0)
        def close(self):
            pass
    class Process:
        pid = 123
        exitcode = None
        def start(self):
            pass
        def is_alive(self):
            return True
    context = SimpleNamespace(Pipe=lambda **k: (Connection(), Connection()), Process=lambda **k: Process())
    monkeypatch.setattr(scanner.multiprocessing, 'get_context', lambda _: context)
    monkeypatch.setattr(scanner, 'load_settings', lambda: AppSettings(pdf_timeout=30))
    ticks = iter([0, 0, 1, 2, 31, 31])
    monkeypatch.setattr(scanner.time, 'monotonic', lambda: next(ticks, 32))
    killed = []
    monkeypatch.setattr(scanner, '_terminate_process_tree', lambda p: killed.append(p.pid))
    budget = scanner._OcrBudget(4)
    matches, _, error, _ = scanner._run_isolated(entry, scan, 60, threading.Event(), budget)
    assert len(matches) == 1 and 'Таймаут' in error and 'частичные' in error
    assert killed == [123] and budget.available == 4


def test_cache_content_change_with_restored_mtime(tmp_path):
    path = tmp_path / 'file.pdf'
    path.write_bytes(b'one')
    scan, app = ScanSettings(use_ocr_for_pdf=False), AppSettings()
    key = pdf_cache.cache_key(path, scan, app)
    pdf_cache.put(key, ReaderOutcome(text='cached'))
    assert pdf_cache.get(key).text == 'cached'
    stat = path.stat()
    path.write_bytes(b'two')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert pdf_cache.cache_key(path, scan, app) != key
    pdf_cache.put(('bad',), ReaderOutcome(text='partial', warning='failed page'))
    assert pdf_cache.get(('bad',)) is None


def test_identical_words_outside_image_are_distinct_occurrences(tmp_path, settings, monkeypatch):
    path = tmp_path / 'two-targets.pdf'
    pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 100, 100), False)
    pix.clear_with(255)
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((20, 20), 'TARGET')
        page.insert_image(fitz.Rect(20, 100, 500, 700), stream=pix.tobytes('png'))
        doc.save(path)
    monkeypatch.setattr(pdf, '_ocr_image', lambda *args: good('TARGET'))
    assert pdf.PdfReader().extract_text(path).text.count('TARGET') == 2
