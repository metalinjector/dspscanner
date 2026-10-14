"""PDF/OCR contract tests: no external engine needed except the opt-in smoke test."""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from app.readers import pdf_reader as pdf
from app.settings_store import AppSettings


@pytest.fixture
def engine(tmp_path, monkeypatch):
    binary = tmp_path / "tesseract"
    binary.touch()
    models = tmp_path / "tessdata-fast"
    models.mkdir()
    (models / "rus.traineddata").touch()
    monkeypatch.setattr(pdf, "load_settings", lambda: AppSettings(tesseract_path=str(binary)))
    monkeypatch.setattr(pdf, "_resolve_ocr_lang", lambda *a, **kw: "rus")
    monkeypatch.setattr(pdf, "_OCR_LIMIT", 2)
    monkeypatch.setattr(pdf, "_OCR_CHUNK", 2)
    monkeypatch.setattr(pdf, "_OCR_SEMAPHORE", threading.BoundedSemaphore(2))
    return binary


def test_system_tessdata_is_not_replaced_by_nonexistent_path(tmp_path, monkeypatch):
    binary = str(tmp_path / "bin" / "tesseract")
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)
    assert pdf._ocr_tessdata_dir(binary, "fast") is None
    env = pdf._tesseract_environment(binary)
    assert "TESSDATA_PREFIX" not in env
    assert env["OMP_THREAD_LIMIT"] == "1"
    assert "OMP_THREAD_LIMIT" not in os.environ
    monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path / "custom"))
    monkeypatch.setenv("OMP_THREAD_LIMIT", "3")
    assert pdf._tesseract_environment(binary)["TESSDATA_PREFIX"] == str(tmp_path / "custom")
    assert pdf._tesseract_environment(binary)["OMP_THREAD_LIMIT"] == "3"


def test_model_resolution_accepts_english_and_prefers_tier(tmp_path):
    binary = tmp_path / "bin" / "tesseract"
    fallback = tmp_path / "bin" / "tessdata"
    fallback.mkdir(parents=True)
    (fallback / "rus.traineddata").touch()
    tier = tmp_path / "tessdata-fast"
    tier.mkdir()
    # An empty tier must not hide a working installation.
    assert pdf._ocr_tessdata_dir(str(binary), "fast") == fallback
    (tier / "eng.traineddata").touch()
    # Fast with English alone must not hide working Russian fallback models.
    assert pdf._ocr_tessdata_dir(str(binary), "fast") == fallback
    (fallback / "rus.traineddata").unlink()
    assert pdf._ocr_tessdata_dir(str(binary), "fast") == tier
    (tier / "rus.traineddata").touch()
    (fallback / "rus.traineddata").touch()
    assert pdf._ocr_tessdata_dir(str(binary), "fast") == tier


def test_language_probe_failure_and_environment_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "_ocr_lang_done", False)
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return subprocess.CompletedProcess([], 1, b"rus\n", b"invalid models")
        return subprocess.CompletedProcess([], 0, b"List of available languages (2):\nrus\neng\n", b"")

    monkeypatch.setattr(pdf, "_run_tesseract", run)
    binary = str(tmp_path / "tesseract")
    assert pdf._resolve_ocr_lang(binary, russian_only=False) is None
    assert pdf._resolve_ocr_lang(binary, russian_only=False) is None
    assert len(calls) == 1
    monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path / "other-models"))
    assert pdf._resolve_ocr_lang(binary, russian_only=False) == "rus+eng"
    assert len(calls) == 2


def test_grayscale_pgm_and_dpi_cap():
    with fitz.open() as document:
        page = document.new_page(width=595, height=842)
        page.insert_text((40, 40), "Contract test")
        image, dpi = pdf._render_ocr_page(document, 0, 150)
        assert dpi == 150
        assert image.startswith(b"P5\n")  # PyMuPDF's 'ppm' output for csGRAY is PGM.
        decoded = fitz.Pixmap(image)
        assert decoded.n == 1
        assert decoded.width == 1240
        assert decoded.height == 1755
    huge = SimpleNamespace(rect=SimpleNamespace(width=10000, height=10000))
    assert pdf._safe_ocr_dpi(huge) < 150
    dpi = pdf._safe_ocr_dpi(huge)
    assert (10000 * dpi / 72) ** 2 <= pdf._OCR_MAX_PIXELS


def test_rolling_window_and_document_thread_ownership(engine, monkeypatch):
    owner = threading.get_ident()
    third_started = threading.Event()
    first_finished = threading.Event()
    active = 0
    peak = 0
    lock = threading.Lock()

    def render(document, idx, dpi):
        assert threading.get_ident() == owner
        # Only two images may be outstanding: page 2 reuses page 1's slot.
        if idx == 2:
            assert not first_finished.is_set()
        return str(idx).encode(), dpi

    def run(binary, arguments, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        idx = int(kwargs["input_bytes"])
        assert arguments[-2:] == ["--dpi", "150"]
        assert arguments[arguments.index("--oem") + 1] == "1"
        if idx == 0:
            assert third_started.wait(3), "chunk barrier: next page could not start"
            first_finished.set()
        if idx == 2:
            third_started.set()
        with lock:
            active -= 1
        return subprocess.CompletedProcess([], 0, (f"page {idx} " * 10).encode(), b"")

    monkeypatch.setattr(pdf, "_render_ocr_page", render)
    monkeypatch.setattr(pdf, "_run_tesseract", run)
    texts, used, warning = pdf.PdfReader._ocr_pages(object(), list(range(5)), "scan.pdf")
    assert used and warning is None
    assert len(texts) == 5 and peak <= 2
    assert third_started.is_set()


@pytest.mark.parametrize("failure", ["timeout", "exit", "launch", "render"])
def test_failures_are_reported_without_high_dpi_retry(engine, monkeypatch, failure):
    attempts = []
    renders = []

    def render(doc, idx, dpi):
        renders.append((idx, dpi))
        if failure == "render" and idx == 1:
            raise RuntimeError("broken page")
        return str(idx).encode(), dpi

    def run(binary, arguments, **kwargs):
        idx = int(kwargs["input_bytes"])
        attempts.append(idx)
        if idx == 1:
            if failure == "timeout":
                raise subprocess.TimeoutExpired("tesseract", 60)
            if failure == "launch":
                raise OSError("cannot launch")
            return subprocess.CompletedProcess([], 1, b"", b"broken engine")
        return subprocess.CompletedProcess([], 0, b"normal text " * 10, b"")

    monkeypatch.setattr(pdf, "_render_ocr_page", render)
    monkeypatch.setattr(pdf, "_run_tesseract", run)
    texts, used, warning = pdf.PdfReader._ocr_pages(object(), [0, 1], "scan.pdf")
    assert used and set(texts) == {0}
    assert "стр. 2" in warning
    assert attempts.count(1) == (0 if failure == "render" else 1)
    assert all(dpi == 150 for idx, dpi in renders)


@pytest.mark.parametrize("second", [b"", b"much longer recognized text", None])
def test_short_text_retries_preserve_best_result(engine, monkeypatch, second):
    calls = []
    monkeypatch.setattr(pdf, "_render_ocr_page", lambda doc, idx, dpi: (b"page", dpi))

    def run(binary, arguments, **kwargs):
        dpi = int(arguments[-1])
        calls.append(dpi)
        if dpi == 250 and second is None:
            raise subprocess.TimeoutExpired("tesseract", 60)
        return subprocess.CompletedProcess([], 0, b"short" if dpi == 150 else second, b"")

    monkeypatch.setattr(pdf, "_run_tesseract", run)
    texts, used, warning = pdf.PdfReader._ocr_pages(object(), [0], "scan.pdf")
    assert used and calls == [150, 250]
    assert texts[0] == (second.decode() if second else "short")
    assert bool(warning) == (second is None)


def test_no_language_fails_before_render(engine, monkeypatch):
    monkeypatch.setattr(pdf, "_resolve_ocr_lang", lambda *a, **kw: None)
    monkeypatch.setattr(pdf, "_render_ocr_page", lambda *a: pytest.fail("must not render"))
    texts, used, warning = pdf.PdfReader._ocr_pages(object(), [0, 1, 2], "scan.pdf")
    assert not used and not texts
    assert "rus.traineddata" in warning
    assert "Переход на eng запрещён" in warning


def test_model_fallback_is_visible(engine, monkeypatch):
    (engine.parent / "tessdata-fast").rename(engine.parent / "tessdata")
    monkeypatch.setattr(pdf, "_render_ocr_page", lambda *a: (b"page", 150))
    monkeypatch.setattr(pdf, "_run_tesseract", lambda *a, **kw: subprocess.CompletedProcess([], 0, b"text " * 10, b""))
    _, used, warning = pdf.PdfReader._ocr_pages(object(), [0], "scan.pdf")
    assert used
    assert "fast не подтверждена" in warning


def test_pdf_text_layer_order_and_page_limit(tmp_path, monkeypatch):
    path = tmp_path / "mixed.pdf"
    with fitz.open() as document:
        for text in ("first page", "", "third page"):
            page = document.new_page()
            if text:
                page.insert_text((40, 40), text)
        document.save(path)
    calls = []

    def ocr(doc, indices, name):
        calls.append(indices)
        return {1: "second page"}, True, "partial warning"

    monkeypatch.setattr(pdf.PdfReader, "_ocr_pages", staticmethod(ocr))
    outcome = pdf.PdfReader().extract_text(path)
    assert outcome.text == "first page\n\nsecond page\n\nthird page"
    assert outcome.warning == "partial warning"
    assert calls == [[1]]
    calls.clear()
    limited = pdf.PdfReader().extract_text(path, max_pages=1)
    assert limited.text == "first page"
    assert "first-1-pages" in limited.method_used
    assert not calls
    without_ocr = pdf.PdfReader().extract_text(path, use_ocr=False)
    assert without_ocr.text == "first page\n\nthird page"
    assert not calls


@pytest.mark.skipif(not os.environ.get("DSP_TEST_TESSERACT"), reason="set DSP_TEST_TESSERACT for real OCR smoke")
def test_real_tesseract_accepts_pgm(monkeypatch):
    binary = Path(os.environ["DSP_TEST_TESSERACT"])
    assert binary.is_file()
    monkeypatch.setattr(pdf, "_ocr_lang_done", False)
    language = pdf._resolve_ocr_lang(str(binary), russian_only=False)
    assert language and "eng" in language, "smoke test needs eng.traineddata"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((50, 100), "Contract number 12345", fontsize=24)
        image, dpi = pdf._render_ocr_page(document, 0, 150)
        result = pdf._run_tesseract(
            str(binary), ["stdin", "stdout", "-l", "eng", "--psm", "3", "--dpi", str(dpi)],
            input_bytes=image, timeout=30,
        )
    assert result.returncode == 0, result.stderr
    assert "contract" in result.stdout.decode().lower()


def test_language_fallback_is_visible(engine, monkeypatch):
    monkeypatch.setattr(pdf, "_resolve_ocr_lang", lambda *a, **kw: "eng")
    monkeypatch.setattr(pdf, "_render_ocr_page", lambda *a: (b"page", 150))
    monkeypatch.setattr(pdf, "_run_tesseract", lambda *a, **kw: subprocess.CompletedProcess([], 0, b"text " * 10, b""))
    _, used, warning = pdf.PdfReader._ocr_pages(object(), [0], "scan.pdf")
    assert used
    assert "Запрошен язык OCR rus, доступен только eng" in warning
