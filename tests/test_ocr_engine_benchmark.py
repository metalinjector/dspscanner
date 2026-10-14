"""Metrics/manifest tests; no network or installed OCR engine needed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import benchmark_ocr_engines as bench


def scored(reference, prediction):
    pytest.importorskip("rapidfuzz", reason="optional OCR benchmark requirements")
    return bench.score_text(reference, prediction)


def test_normalization_does_not_repair_ocr_errors():
    assert bench.normalize("  ДОГОВОР\nСчёт  ") == "договор счёт"
    assert bench.normalize("счет") != bench.normalize("счёт")
    assert bench.normalize("ZX-2048") != bench.normalize("ZX-2O48")


def test_scoring_identical_text_ignores_case_and_whitespace():
    row = scored("Договор\nСчёт", "ДОГОВОР   счёт")
    assert row["cer"] == row["wer"] == 0
    assert row["terms"]["договор"] == {"expected": 1, "found": 1, "missed": 0, "extra": 0}


def test_scoring_counts_misses_extra_occurrences_and_false_positives():
    row = scored("Договор договор счёт", "договор счёт счёт bankruptcy")
    assert row["terms"]["договор"]["missed"] == 1
    assert row["terms"]["счёт"]["extra"] == 1
    assert row["terms"]["bankruptcy"] == {"expected": 0, "found": 1, "missed": 0, "extra": 1}
    assert row["cer"] > 0


def test_empty_output_is_not_successful_fast_ocr():
    row = scored("Договор счёт", "")
    assert row["empty"]
    assert row["cer"] == row["wer"] == 1
    assert sum(t["missed"] for t in row["terms"].values()) == 2


def test_unicode_normalization_but_not_yo_replacement():
    assert scored("Счёт", "Сче\u0308т")["cer"] == 0
    assert scored("Счёт", "Счет")["terms"]["счёт"]["missed"] == 1


def trial(backend, trial_number, seconds, reference, prediction):
    row = {"page": "p1", "seconds": seconds, **scored(reference, prediction)}
    return {"backend": backend, "trial": trial_number, "rows": [row],
            "warm_corpus_seconds": seconds, "cold_first_page_seconds": seconds + 1,
            "sampled_peak_tree_rss_bytes": 20 * 1024**2,
            "warmup": [{"page": "p1", "text_sha256": row["text_sha256"]}]}


def test_summary_uses_median_and_aggregates_all_trials():
    result = bench.summarize([
        trial("engine", 0, 1, "Договор договор", "договор"),
        trial("engine", 1, 9, "Договор договор", "договор"),
        trial("engine", 2, 2, "Договор договор", "договор"),
    ])[0]
    assert result["median_corpus_seconds"] == 2
    assert result["pages_per_second"] == 0.5
    assert result["p95_page_seconds_nearest_rank"] == 9
    assert result["control_occurrences_expected"] == 6
    assert result["control_occurrences_missed"] == 3
    assert result["control_occurrence_recall"] == 0.5
    # Losing a repeated occurrence is NOT the same as missing the page entirely.
    assert result["page_term_recall"] == 1
    assert result["stable_text_per_page"]
    assert result["warmup_matches_measured"]


def test_summary_separates_backends_and_detects_unstable_text():
    rows = [trial("a", 0, 1, "договор", "договор"),
            trial("a", 1, 1, "договор", "счёт"),
            trial("b", 0, 1, "договор", "договор")]
    summaries = {row["backend"]: row for row in bench.summarize(rows)}
    assert not summaries["a"]["stable_text_per_page"]
    assert summaries["b"]["stable_text_per_page"]
    assert summaries["a"]["false_positive_page_term_pairs"] == 1
    assert summaries["a"]["page_term_recall"] == 0.5


def test_model_manifest_pins_mirrors_and_upstream_digests():
    manifest = json.loads(bench.MANIFEST.read_text())
    assert "v3.10.0" in manifest["upstream_registry"]
    assert len(manifest["models"]) == 4
    for filename, item in manifest["models"].items():
        assert Path(filename).name == filename
        assert filename.endswith(".onnx")
        assert item["url"].startswith("https://api.github.com/repos/")
        commit = item["url"].split("?ref=")[1]
        assert len(commit) == 40
        assert len(bytes.fromhex(item["sha256"])) == 32


def test_corpus_is_deterministic_and_has_no_pdf_text_layer(tmp_path):
    pytest.importorskip("numpy", reason="optional benchmark corpus dependency")
    pytest.importorskip("PIL", reason="optional benchmark corpus dependency")
    fitz = pytest.importorskip("pymupdf")
    fonts = Path("/usr/share/fonts/truetype/dejavu")
    if not (fonts / "DejaVuSans.ttf").is_file():
        pytest.skip("Linux benchmark fonts not installed")
    first = bench.make_corpus(tmp_path / "first", fonts)
    second = bench.make_corpus(tmp_path / "second", fonts)
    assert len(first["pages"]) == 8
    assert [p["sha256"] for p in first["pages"]] == [p["sha256"] for p in second["pages"]]
    with fitz.open(tmp_path / "first/corpus.pdf") as document:
        assert len(document) == 8
        assert all(not page.get_text().strip() for page in document)
    assert any("Contract" in p["reference"] for p in first["pages"])
