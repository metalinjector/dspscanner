"""Reproducible OCR experiments; no document text is written to the report.

python tools/benchmark_ocr.py --repeats 5
python tools/benchmark_ocr.py --ocr --tesseract /path/to/tesseract --pdf scan.pdf

Transport benchmark compares the old RGB/PNG path, gray/PNG ablation, and
new gray/PGM path at identical DPI. It does NOT measure recognition speed.
--ocr adds real PdfReader measurements (not the scanner's spawn/discovery).
Synthetic input is image-only A4 (English by default; --corpus mixed or
russian embeds a bundled Unicode font). --all-modes tests 3 model tiers x
2 language modes; --compare-transports adds RGB PNG vs gray PGM ablation.
Use a reviewed real corpus before making general accuracy or speed claims.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import statistics
import sys
import time
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import pymupdf as fitz
except ImportError:  # legacy PyMuPDF releases
    import fitz

from app.readers import pdf_reader as pdf  # noqa: E402


def synthetic_document(pages: int, corpus: str = "english"):
    document = fitz.open()
    with fitz.open() as source:
        page = source.new_page(width=595, height=842)
        if corpus == "english":
            lines = [f"Contract {i:03d}: review the document and confirm delivery." for i in range(45)]
            page.insert_text((45, 45), "\n".join(lines), fontsize=11)
        else:
            lines = [
                "Служебная записка",
                "Договор на поставку оборудования № 12345.",
                "Срок поставки: октябрь 2026 года.",
                "Оплата после проверки документов.",
            ]
            if corpus == "mixed":
                lines += ["Contract number 12345 for equipment delivery.", "Payment after document review."]
            writer = fitz.TextWriter(page.rect)
            font = fitz.Font("cjk")  # bundled Unicode font, no OS-font dependency
            for number, line in enumerate(lines * 5):
                writer.append((45, 45 + 20 * number), line, font=font, fontsize=12)
            writer.write_text(page)
        scan = page.get_pixmap(dpi=250, alpha=False).tobytes("png")
        for _ in range(pages):
            document.new_page(width=595, height=842).insert_image(page.rect, stream=scan)
    return document


def measure_transport(document, indices, repeats: int):
    variants = [
        ("baseline_rgb_png", fitz.csRGB, "png"),
        ("ablation_gray_png", fitz.csGRAY, "png"),
        ("candidate_gray_pgm", fitz.csGRAY, "ppm"),
    ]
    results = {label: {"seconds": [], "payload_bytes": 0} for label, _, _ in variants}
    for repeat in range(repeats + 1):  # one warmup per variant
        # Rotate order so CPU/cache drift does not always favour one variant.
        offset = repeat % len(variants)
        for label, colorspace, image_format in variants[offset:] + variants[:offset]:
            payload_bytes = 0
            started = time.perf_counter()
            for index in indices:
                page = document[index]
                dpi = min(150, pdf._safe_ocr_dpi(page))
                pixmap = page.get_pixmap(dpi=dpi, colorspace=colorspace, alpha=False)
                image = pixmap.tobytes(image_format)
                payload_bytes += len(image)
                del image, pixmap
            elapsed = time.perf_counter() - started
            if repeat:
                results[label]["seconds"].append(elapsed)
            results[label]["payload_bytes"] = payload_bytes
    for result in results.values():
        result["median_seconds"] = statistics.median(result["seconds"])
    return results


def measure_ocr(document, indices, args, binary: str):
    """Same scheduler/OEM/DPI; optional transport ablation, not old scanner vs new."""
    tiers = list(pdf.OCR_MODEL_TIERS) if args.all_modes else [args.tier]
    language_modes = [True, False] if args.all_modes else [args.russian_only]
    transports = ["rgb_png", "gray_pgm"] if args.compare_transports else ["gray_pgm"]
    original_render = pdf._render_ocr_page

    def render_rgb_png(doc, index, target_dpi):
        page = doc[index]
        dpi = min(target_dpi, pdf._safe_ocr_dpi(page))
        return page.get_pixmap(dpi=dpi, alpha=False, colorspace=fitz.csRGB).tobytes("png"), dpi

    version = pdf._run_tesseract(binary, ["--version"], timeout=15)
    if version.returncode:
        raise RuntimeError(version.stderr.decode(errors="replace"))
    results = []
    cases = [(tier, russian, transport) for tier in tiers for russian in language_modes for transport in transports]
    # Rotate configurations, not all Fast repeats first and Best repeats last.
    for repeat in range(args.repeats):
        offset = repeat % len(cases)
        for tier, russian_only, transport in cases[offset:] + cases[:offset]:
            os.environ["DSP_SCANNER_OCR_MODEL_TIER"] = tier
            os.environ["DSP_SCANNER_RUSSIAN_ONLY"] = str(russian_only)
            render = original_render if transport == "gray_pgm" else render_rgb_png
            with patch.object(pdf, "_render_ocr_page", render):
                started = time.perf_counter()
                texts, used, warning = pdf.PdfReader._ocr_pages(document, indices, "benchmark.pdf")
                elapsed = time.perf_counter() - started
            text = "\n".join(texts.get(index, "") for index in indices)
            results.append({
                "repeat": repeat + 1, "tier": tier, "requested_language": "rus" if russian_only else "rus+eng",
                "resolved_language": pdf._resolve_ocr_lang(binary, russian_only, tier),
                "model_directory": str(pdf._ocr_tessdata_dir(binary, tier)), "transport": transport,
                "workers": min(args.workers, 4, len(indices)), "oem": 1, "psm": 3,
                "wall_seconds": elapsed, "pages_with_text": len(texts), "used": used, "warning": warning,
                "characters": len(text), "term_occurrences": text.casefold().count(args.term.casefold()),
                "control_term_counts": {term: text.casefold().count(term) for term in ("договор", "документов", "contract")},
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            })
    summary = []
    for tier, russian, transport in cases:
        language = "rus" if russian else "rus+eng"
        rows = [row for row in results if (row["tier"], row["requested_language"], row["transport"]) == (tier, language, transport)]
        summary.append({
            "tier": tier, "language": language, "transport": transport,
            "median_wall_seconds": statistics.median(row["wall_seconds"] for row in rows),
            "all_pages_read": all(row["pages_with_text"] == len(indices) for row in rows),
            "warnings": sorted({row["warning"] for row in rows if row["warning"]}),
        })
    return {"engine_version": version.stdout.decode(errors="replace").splitlines(),
            "ocr": results, "ocr_summary": summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--pages", type=int, default=4, help="first N pages (synthetic if no --pdf)")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--ocr", action="store_true", help="also run real Tesseract")
    parser.add_argument("--tesseract", type=Path)
    parser.add_argument("--tier", choices=tuple(pdf.OCR_MODEL_TIERS), default="fast")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--corpus", choices=("english", "russian", "mixed"), default="english")
    parser.add_argument("--all-modes", action="store_true", help="OCR matrix: 3 tiers x rus/rus+eng")
    parser.add_argument("--compare-transports", action="store_true", help="real OCR with RGB PNG vs gray PGM")
    parser.add_argument("--russian-only", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--term", default="contract", help="case-insensitive literal count, not an accuracy metric")
    args = parser.parse_args()
    if (args.all_modes or args.compare_transports) and not args.ocr:
        parser.error("--all-modes / --compare-transports require --ocr")
    if min(args.pages, args.repeats, args.workers) < 1:
        parser.error("pages, repeats and workers must be positive")
    if args.tesseract:
        if not args.tesseract.is_file():
            parser.error("Tesseract executable not found")
        os.environ["DSP_SCANNER_TESSERACT_PATH"] = str(args.tesseract.resolve())
    os.environ["DSP_SCANNER_OCR_MODEL_TIER"] = args.tier
    os.environ["DSP_SCANNER_RUSSIAN_ONLY"] = str(args.russian_only)
    pdf.configure_ocr_limit(args.workers)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    report = {
        "python": platform.python_version(), "platform": platform.platform(),
        "pymupdf": fitz.VersionBind, "cpu_count": os.cpu_count(),
        "input": "user_pdf" if args.pdf else f"synthetic_image_only_A4_{args.corpus}",
        "repeats": args.repeats, "transport_dpi": 150,
    }
    with fitz.open(args.pdf) if args.pdf else synthetic_document(args.pages, args.corpus) as document:
        indices = list(range(min(args.pages, len(document))))
        if not indices:
            parser.error("empty PDF")
        report["pages"] = len(indices)
        report["transport_only"] = measure_transport(document, indices, args.repeats)
        if args.ocr:
            from app.settings_store import load_settings

            settings = load_settings()
            if not settings.tesseract_path:
                parser.error("Tesseract not found; pass --tesseract")
            report.update(measure_ocr(document, indices, args, settings.tesseract_path))
    failed = any(not row["used"] or row["pages_with_text"] != report["pages"] or row["warning"]
                 for row in report.get("ocr", []))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
