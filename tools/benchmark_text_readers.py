"""Reproducible reader/keyword experiment, not an OCR or whole-scanner benchmark.

python tools/benchmark_text_readers.py --compare-baseline --repeats 7
--compare-baseline executes the reader source from the fixed repository commit.
Corpora stay in ignored .venv/doc-audit; only metrics/hashes are persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import types
import zipfile
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.readers.doc_reader import DocReader  # noqa: E402
from app.readers.docx_reader import DocxReader  # noqa: E402
from app.readers.txt_reader import TxtReader  # noqa: E402
from app.scanning import search_engine as search  # noqa: E402
from tools.text_corpus import binary_doc, docx, WORD_NS  # noqa: E402

BASELINE = "f5c09ad70d809e6931c13e34700deb7da0296e10"
TERMS = ["договор", "РФ", "contract"]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def old_readers():
    modules, hashes = {}, {}
    for name in ("doc_formats", "txt_reader", "docx_reader", "doc_reader"):
        source = subprocess.check_output(["git", "show", f"{BASELINE}:app/readers/{name}.py"], cwd=ROOT)
        module = types.ModuleType(f"baseline_{name}")
        exec(compile(source, f"{BASELINE}/{name}.py", "exec"), module.__dict__)
        modules[name], hashes[name] = module, digest(source)
    modules["doc_reader"].doc_formats = modules["doc_formats"]
    return {".txt": modules["txt_reader"].TxtReader(), ".docx": modules["docx_reader"].DocxReader(),
            ".doc": modules["doc_reader"].DocReader()}, hashes


def counts(text):
    found = search.find_matches(text or "", TERMS, whole_word=True, max_matches_per_word=1000)
    return {term: sum(m.word == term for m in found) for term in TERMS}


def make_corpus(directory):
    directory.mkdir(parents=True, exist_ok=True)
    lines = [f"Строка {i:04d}: Договор РФ и contract. Поставка оборудования согласована." for i in range(500)]
    text = "\n".join(lines)
    result = []
    for name, encoding, content in [("utf8.txt", "utf-8", text), ("cp1251.txt", "cp1251", text),
                                     ("short-cp1251.txt", "cp1251", "Договор"),
                                     ("utf16-no-bom.txt", "utf-16-le", text)]:
        path = directory / name
        path.write_bytes(content.encode(encoding))
        result.append((path, content))
    result.append((docx(directory / "paragraphs.docx", lines), text))
    table_path = directory / "table.docx"
    rows = "".join('<w:tr><w:tc><w:p><w:r><w:t>' + escape(line) + '</w:t></w:r></w:p></w:tc></w:tr>' for line in lines)
    with zipfile.ZipFile(table_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", f'<w:document xmlns:w="{WORD_NS}"><w:body><w:tbl>{rows}</w:tbl></w:body></w:document>')
    result.append((table_path, text))
    result.append((binary_doc(directory / "unicode.doc", text.replace("\n", "\r")), text))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare-baseline", action="store_true")
    parser.add_argument("--scanner", action="store_true", help="also measure current end-to-end scanner")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/benchmarks/text-readers-linux-2026-10-14.json")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    os.environ["DSP_SCANNER_LIBREOFFICE_PATH"] = str(ROOT / ".venv/no-libreoffice")
    current = {".txt": TxtReader(), ".docx": DocxReader(), ".doc": DocReader()}
    variants = {"current": current}
    baseline_hashes = {}
    if args.compare_baseline:
        variants["baseline"], baseline_hashes = old_readers()
    corpus = make_corpus(ROOT / ".venv/doc-audit/corpus")
    report = {"python": platform.python_version(), "platform": platform.platform(),
              "cpu_count": os.cpu_count(), "repeats": args.repeats,
              "baseline_ref": BASELINE if args.compare_baseline else None,
              "baseline_source_sha256": baseline_hashes,
              "scope": "Warm file-cache, serial reader only; search separately. Synthetic text, no OCR. Native DOC fixture is a generated CFB, not a Word save-roundtrip.",
              "readers": [], "search": []}
    from importlib.metadata import version
    report["versions"] = {name: version(name) for name in ("python-docx", "olefile", "charset-normalizer", "defusedxml", "beautifulsoup4")}
    for path, reference in corpus:
        expected = counts(reference)
        elapsed = {name: [] for name in variants}
        outputs = {}
        for repeat in range(args.repeats + 1):  # entire case warmup for both readers
            order = list(variants)
            if repeat % 2:
                order.reverse()
            for name in order:
                before = time.perf_counter()
                outcome = variants[name][path.suffix].extract_text(path)
                seconds = time.perf_counter() - before
                if repeat:
                    elapsed[name].append(seconds)
                outputs[name] = outcome
        report["readers"].append({"file": path.name, "bytes": path.stat().st_size,
                                  "sha256": digest(path.read_bytes()), "expected": expected,
                                  "variants": {name: {"seconds": values, "median_seconds": statistics.median(values),
                                    "found": counts(outputs[name].text), "method": outputs[name].method_used,
                                    "text_sha256": digest((outputs[name].text or "").encode()),
                                    "warning": outputs[name].warning, "error": outputs[name].error}
                                               for name, values in elapsed.items()}})
    # Hold the SAME Unicode string/query sets fixed; isolate Aho vs regex cost.
    text = corpus[0][1]
    for query_count in (3, 64):
        queries = TERMS + [f"отсутствует{i}" for i in range(query_count - len(TERMS))]
        values = {"auto": [], "regex": [], "aho": []}
        positions = {}
        old_threshold = search._AHO_MIN_TERMS
        try:
            for repeat in range(args.repeats + 1):
                modes = list(values)
                offset = repeat % len(modes)
                for mode in modes[offset:] + modes[:offset]:
                    search._AHO_MIN_TERMS = {"auto": old_threshold, "regex": 10000, "aho": 1}[mode]
                    started = time.perf_counter()
                    matches = search.find_matches(text, queries, whole_word=True, max_matches_per_word=3)
                    elapsed = time.perf_counter() - started
                    positions[mode] = [(m.word, m.position, m.matched_text) for m in matches]
                    if repeat:
                        values[mode].append(elapsed)
        finally:
            search._AHO_MIN_TERMS = old_threshold
        report["search"].append({"characters": len(text), "query_count": query_count,
                                 "max_matches_per_term": 3, "same_matches": positions["auto"] == positions["regex"] == positions["aho"],
                                 "variants": {name: {"seconds": times, "median_seconds": statistics.median(times)}
                                              for name, times in values.items()}})
    if args.scanner:
        from app.config import ScanSettings
        from app.scanning.scanner import DocumentScanner
        os.environ["DSP_SCANNER_RUSSIAN_ONLY"] = "true"
        report["scanner"] = []
        for extension, source_name in [(".doc", "unicode.doc"), (".docx", "paragraphs.docx"), (".txt", "utf8.txt")]:
            folder = ROOT / ".venv/doc-audit/scanner" / extension[1:]
            folder.mkdir(parents=True, exist_ok=True)
            source = next(path for path, _ in corpus if path.name == source_name)
            for i in range(3):
                (folder / f"sample-{i}{extension}").write_bytes(source.read_bytes())
            runs = []
            for _ in range(3):
                settings = ScanSettings(paths=[str(folder)], words=TERMS, file_types={extension},
                                        max_workers=2, max_matches_per_word=3, whole_word=True,
                                        use_ocr_for_pdf=False)
                started = time.perf_counter()
                outcome = DocumentScanner().run(settings)
                runs.append({"seconds": time.perf_counter() - started, "processed": outcome.stats.processed,
                             "errors": outcome.stats.errors, "matches": len(outcome.results)})
            report["scanner"].append({"extension": extension, "workers": 2, "russian_only": True,
                                      "expected_matches": 27, "runs": runs,
                                      "median_seconds": statistics.median(row["seconds"] for row in runs)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(any(row["variants"]["current"]["found"] != row["expected"] or row["variants"]["current"]["error"]
                   for row in report["readers"]) or any(not row["same_matches"] for row in report["search"]) or any(r["errors"] or r["processed"] != 3 or r["matches"] != 27
                   for s in report.get("scanner", []) for r in s["runs"]))


if __name__ == "__main__":
    raise SystemExit(main())
