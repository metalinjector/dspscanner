"""Offline, isolated Russian OCR experiment; NOT a production backend change.

See docs/OCR_ENGINE_BENCHMARK_2026-10-14.md for setup and limitations.
Downloads are opt-in and hash-verified. Generated images stay in ignored runtime
storage. Each trial starts a fresh worker, warms it on ALL corpus pages, then
measures rotated page order. Different engines never run concurrently.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MANIFEST = ROOT / "tools/ocr/benchmark_models.json"
BACKENDS = (
    "tess_cli_rus", "tess_api_rus", "tess_cli_rus_eng", "tess_api_rus_eng",
    "rapid_v5_cyrillic", "rapid_v5_eslav", "rapid_v6tiny_v5_eslav",
    "rapid_v5_eslav_openvino", "rapid_v6tiny_v5_eslav_openvino",
)
TERMS = ("договор", "счёт", "оборудование", "поставка", "согласовано",
         "приложение", "contract", "invoice", "ZX-2048",
         "расторжение", "неустойка", "bankruptcy")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def normalize(text):
    """Whitespace/NFC/case only; DO NOT repair OCR confusions or transliterate."""
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def score_text(reference, prediction):
    from rapidfuzz.distance import Levenshtein

    ref, pred = normalize(reference), normalize(prediction)
    rw, pw = ref.split(), pred.split()
    counts = {}
    for term in TERMS:
        key = normalize(term)
        expected, found = ref.count(key), pred.count(key)
        counts[term] = {"expected": expected, "found": found,
                        "missed": max(0, expected - found),
                        "extra": max(0, found - expected)}
    return {
        "char_edits": Levenshtein.distance(ref, pred), "ref_chars": len(ref),
        "word_edits": Levenshtein.distance(rw, pw), "ref_words": len(rw),
        "cer": Levenshtein.distance(ref, pred) / max(1, len(ref)),
        "wer": Levenshtein.distance(rw, pw) / max(1, len(rw)),
        "terms": counts, "text_sha256": sha256(prediction.encode()),
        "characters": len(prediction), "empty": not bool(pred),
    }


def make_corpus(directory, font_dir):
    """Deterministic 8-page image-only A4 corpus with known transcription."""
    import numpy as np
    import pymupdf as fitz
    from PIL import Image, ImageFilter

    directory.mkdir(parents=True, exist_ok=True)
    fonts = {name: font_dir / filename for name, filename in (
        ("sans", "DejaVuSans.ttf"), ("serif", "DejaVuSerif.ttf"))}
    for path in fonts.values():
        if not path.is_file():
            raise FileNotFoundError(f"Missing font: {path}; pass --font-dir")
    sentences = [
        "Договор на поставку оборудования подписан сторонами.",
        "Счёт за оборудование передан в бухгалтерию.",
        "Поставка производится после проверки документов.",
        "Согласовано руководителем отдела закупок.",
        "Приложение содержит перечень запасных частей.",
        "Оплата производится в течение десяти рабочих дней.",
    ]
    cases = [
        ("clean_ru", "sans", 12, 20, 26),
        ("dense_ru", "serif", 10, 13, 52),
        ("mixed_ru_en", "sans", 11, 18, 34),
        ("table", "sans", 10, 23, 26),
        ("two_columns", "serif", 9, 17, 36),
        ("small_print", "serif", 7.5, 12, 52),
        ("degraded", "sans", 11, 19, 30),
        ("sparse_note", "sans", 14, 30, 5),
    ]
    manifest = {"dpi": 150, "source_dpi": 250, "seed": 20261014,
                "font_sha256": {k: sha256(v.read_bytes()) for k, v in fonts.items()},
                "pages": []}
    pdf_path = directory / "corpus.pdf"
    scans = fitz.open()
    for case, font_name, size, spacing, count in cases:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        font = fitz.Font(fontfile=str(fonts[font_name]))
        writer = fitz.TextWriter(page.rect)
        lines = []
        for i in range(count):
            x, y = 35, 48 + i * spacing
            line = f"{i + 1:02d}. {sentences[i % len(sentences)]}"
            if case == "mixed_ru_en" and i % 3 == 1:
                line = f"{i + 1:02d}. Contract ZX-2048: invoice for equipment delivery."
            if case == "table":
                line = f"{i + 1:02d}     Оборудование     {10 + i} шт.     Счёт {4000 + i}"
                page.draw_line((32, y + 6), (562, y + 6), color=(0.5,) * 3, width=0.4)
            if case == "two_columns":
                column, row = divmod(i, count // 2)
                x, y = 35 + column * 277, 48 + row * spacing
                line = f"{i + 1:02d}. " + ["Договор согласовано.", "Счёт за оборудование.",
                                           "Поставка: приложение."][i % 3]
            if font.text_length(line, fontsize=size) > (250 if case == "two_columns" else 525):
                raise ValueError(f"Text would be clipped in {case}: {line}")
            writer.append((x, y), line, font=font, fontsize=size)
            lines.append(line)
        writer.write_text(page)
        pix = page.get_pixmap(dpi=250, colorspace=fitz.csGRAY, alpha=False)
        image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        if case == "degraded":
            image = image.rotate(1.3, resample=Image.Resampling.BICUBIC, fillcolor=255)
            image = image.filter(ImageFilter.GaussianBlur(0.7))
            rng = np.random.default_rng(20261014)
            array = np.asarray(image, dtype=np.float32) * 0.66 + 65
            array += rng.normal(0, 5, array.shape)
            image = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8))
            jpeg = io.BytesIO()
            image.save(jpeg, format="JPEG", quality=55)
            image = Image.open(io.BytesIO(jpeg.getvalue())).convert("L")
        encoded = io.BytesIO()
        image.save(encoded, format="PNG")
        scan_page = scans.new_page(width=595, height=842)
        scan_page.insert_image(scan_page.rect, stream=encoded.getvalue())
        started = time.perf_counter()
        payload = scan_page.get_pixmap(dpi=150, colorspace=fitz.csGRAY, alpha=False).tobytes("ppm")
        render = time.perf_counter() - started
        (directory / f"{case}.pgm").write_bytes(payload)
        manifest["pages"].append({"id": case, "reference": "\n".join(lines),
                                  "sha256": sha256(payload), "render_seconds": render,
                                  "file": f"{case}.pgm", "bytes": len(payload)})
        doc.close()
    scans.save(pdf_path)
    scans.close()
    manifest["note"] = "Synthetic, upright pages; not an independent real-world accuracy corpus."
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


class TessAPI:
    """Minimal C API binding, linked from the SAME static build as the CLI.

    This measures reusable TessBaseAPI, NOT a tesserocr wheel with another engine.
    The handle stays in one isolated worker; results/adaptation clear each page.
    """

    def __init__(self, library, tessdata, language):
        self.lib = ctypes.CDLL(str(library))
        signatures = {
            "TessVersion": (ctypes.c_char_p, []),
            "TessBaseAPICreate": (ctypes.c_void_p, []),
            "TessBaseAPIInit2": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]),
            "TessBaseAPISetPageSegMode": (None, [ctypes.c_void_p, ctypes.c_int]),
            "TessBaseAPISetImage": (None, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]),
            "TessBaseAPISetSourceResolution": (None, [ctypes.c_void_p, ctypes.c_int]),
            "TessBaseAPIRecognize": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
            "TessBaseAPIGetUTF8Text": (ctypes.c_void_p, [ctypes.c_void_p]),
            "TessDeleteText": (None, [ctypes.c_void_p]),
            "TessBaseAPIClear": (None, [ctypes.c_void_p]),
            "TessBaseAPIClearAdaptiveClassifier": (None, [ctypes.c_void_p]),
            "TessBaseAPIDelete": (None, [ctypes.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.lib, name)
            function.restype, function.argtypes = result, arguments
        self.handle = self.lib.TessBaseAPICreate()
        if not self.handle:
            raise RuntimeError("TessBaseAPICreate returned null")
        if self.lib.TessBaseAPIInit2(self.handle, os.fsencode(tessdata), language.encode(), 1):
            self.close()
            raise RuntimeError("TessBaseAPIInit2 failed")
        self.lib.TessBaseAPISetPageSegMode(self.handle, 3)
        self.version = self.lib.TessVersion().decode()

    def recognize(self, payload):
        from PIL import Image

        image = Image.open(io.BytesIO(payload)).convert("L")
        pixels = ctypes.create_string_buffer(image.tobytes())
        self.lib.TessBaseAPISetImage(self.handle, pixels, image.width, image.height, 1, image.width)
        self.lib.TessBaseAPISetSourceResolution(self.handle, 150)
        if self.lib.TessBaseAPIRecognize(self.handle, None):
            raise RuntimeError("TessBaseAPIRecognize failed")
        pointer = self.lib.TessBaseAPIGetUTF8Text(self.handle)
        if not pointer:
            raise RuntimeError("TessBaseAPIGetUTF8Text returned null")
        try:
            return ctypes.string_at(pointer).decode("utf-8")
        finally:
            self.lib.TessDeleteText(pointer)
            self.lib.TessBaseAPIClear(self.handle)
            self.lib.TessBaseAPIClearAdaptiveClassifier(self.handle)

    def close(self):
        if self.handle:
            self.lib.TessBaseAPIDelete(self.handle)
            self.handle = None


def rapid_params(backend, model_dir):
    from rapidocr import EngineType, ModelType, OCRVersion

    tiny = "v6tiny" in backend
    rec = "cyrillic" if "cyrillic" in backend else "eslav"
    det_file = "PP-OCRv6_det_tiny.onnx" if tiny else "ch_PP-OCRv5_det_mobile.onnx"
    provider = EngineType.OPENVINO if backend.endswith("openvino") else EngineType.ONNXRUNTIME
    return {
        "Global.use_cls": False, "Global.log_level": "error",
        "Global.max_side_len": 2000,
        "Det.engine_type": provider, "Rec.engine_type": provider,
        "Det.model_path": str(model_dir / det_file),
        "Det.ocr_version": OCRVersion.PPOCRV6 if tiny else OCRVersion.PPOCRV5,
        "Det.model_type": ModelType.TINY if tiny else ModelType.MOBILE,
        "Det.lang_type": "multi", "Det.limit_type": "min", "Det.limit_side_len": 736,
        "Rec.model_path": str(model_dir / f"{rec}_PP-OCRv5_rec_mobile.onnx"),
        "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE,
        "Rec.lang_type": rec,
        "EngineConfig.onnxruntime.intra_op_num_threads": 1,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.use_cuda": False,
        "EngineConfig.openvino.inference_num_threads": 1,
        "EngineConfig.openvino.num_streams": 1,
        "EngineConfig.openvino.performance_hint": "LATENCY",
        "EngineConfig.openvino.enable_cpu_pinning": "NO",
    }


def build_backend(args):
    backend = args.worker
    language = "rus+eng" if backend.endswith("rus_eng") else "rus"
    tessdata = args.runtime / "tessdata-fast"
    if backend.startswith("tess_api"):
        api = TessAPI(args.runtime / "lib/libtesseract-benchmark.so", tessdata, language)
        return lambda data: (api.recognize(data), {}), api.close, {"version": api.version, "language": language}
    if backend.startswith("tess_cli"):
        command = [str(args.runtime / "bin/tesseract"), "stdin", "stdout", "--tessdata-dir", str(tessdata),
                   "-l", language, "--oem", "1", "--psm", "3", "--dpi", "150"]
        def recognize(data):
            result = subprocess.run(command, input=data, capture_output=True, timeout=90, check=True)
            return result.stdout.decode("utf-8"), {"stderr": result.stderr.decode("utf-8", errors="replace")}
        return recognize, lambda: None, {"command": command, "language": language}
    import cv2
    import numpy as np
    from rapidocr import RapidOCR
    from omegaconf import OmegaConf

    cv2.setNumThreads(1)
    engine = RapidOCR(params=rapid_params(backend, args.runtime / "benchmark-models"))
    # Fail closed: no model/dictionary download is allowed during measurements.
    from rapidocr.utils.download_file import DownloadFile
    def forbidden_download(*unused_args, **unused_kwargs):
        raise RuntimeError("Unexpected network model download during offline benchmark")
    DownloadFile.run = forbidden_download

    def recognize(data):
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Could not decode corpus PGM")
        result = engine(image)
        sessions = {"det": engine.text_det.session.session, "rec": engine.text_rec.session.session}
        if backend.endswith("openvino"):
            execution = {key: {prop: str(session.get_compiled_model().get_property(prop))
                               for prop in ("EXECUTION_DEVICES", "INFERENCE_PRECISION_HINT", "INFERENCE_NUM_THREADS", "NUM_STREAMS")}
                         for key, session in sessions.items()}
        else:
            execution = {key: session.get_providers() for key, session in sessions.items()}
        return "\n".join(result.txts or ()), {"stage_seconds": list(result.elapse_list or ()),
                                             "boxes": len(result.txts or ()), "execution": execution}
    return recognize, lambda: None, {"config": OmegaConf.to_container(engine.cfg, resolve=True, enum_to_str=True)}


class MemorySampler:
    """Sample sum of worker+child RSS (shared pages may be double counted)."""

    def __init__(self):
        import psutil
        self.psutil = psutil
        self.process = psutil.Process()
        self.peak = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            total = 0
            try:
                processes = [self.process, *self.process.children(recursive=True)]
            except self.psutil.Error:
                processes = [self.process]
            for process in processes:
                try:
                    total += process.memory_info().rss
                except self.psutil.Error:
                    pass
            self.peak = max(self.peak, total)
            self.stop.wait(0.01)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *unused):
        self.stop.set()
        self.thread.join()


def worker(args):
    affinity = None
    if hasattr(os, "sched_setaffinity"):
        affinity = min(os.sched_getaffinity(0))
        os.sched_setaffinity(0, {affinity})
    corpus = json.loads((args.corpus_dir / "manifest.json").read_text())
    pages = corpus["pages"]
    payloads = [(args.corpus_dir / page["file"]).read_bytes() for page in pages]
    for page, payload in zip(pages, payloads):
        if sha256(payload) != page["sha256"]:
            raise ValueError(f"Corpus hash mismatch: {page['id']}")
    with MemorySampler() as memory:
        started = time.perf_counter()
        recognize, close, metadata = build_backend(args)
        setup = time.perf_counter() - started
        try:
            # Entire corpus warm-up prevents biased warm-up on only clean pages.
            warm = []
            for index, page in enumerate(pages):
                before = time.perf_counter()
                text, extra = recognize(payloads[index])
                elapsed = time.perf_counter() - before
                warm.append({"page": page["id"], "seconds": elapsed,
                             "text_sha256": sha256(text.encode())})
            rows = []
            order = list(range(len(pages)))
            offset = args.trial % len(order)
            order = order[offset:] + order[:offset]
            for index in order:
                page = pages[index]
                before = time.perf_counter()
                text, extra = recognize(payloads[index])
                elapsed = time.perf_counter() - before
                rows.append({"page": page["id"], "seconds": elapsed,
                             **score_text(page["reference"], text), **extra})
        finally:
            close()
    return {"backend": args.worker, "trial": args.trial, "affinity_cpu": affinity,
            "setup_seconds": setup, "cold_first_page_seconds": setup + warm[0]["seconds"],
            "cold_note": "Engine imports/init + first inference; OS file cache is NOT flushed; Python spawn excluded.",
            "warmup": warm, "rows": rows, "metadata": metadata,
            "sampled_peak_tree_rss_bytes": memory.peak,
            "warm_corpus_seconds": sum(row["seconds"] for row in rows)}


def summarize(trials):
    summaries = []
    for backend in sorted({trial["backend"] for trial in trials}):
        group = [trial for trial in trials if trial["backend"] == backend]
        rows = [row for trial in group for row in trial["rows"]]
        expected = sum(t["expected"] for row in rows for t in row["terms"].values())
        missed = sum(t["missed"] for row in rows for t in row["terms"].values())
        extra = sum(t["extra"] for row in rows for t in row["terms"].values())
        page_positive = sum(t["expected"] > 0 for row in rows for t in row["terms"].values())
        page_missed = sum(t["expected"] > 0 and t["found"] == 0 for row in rows for t in row["terms"].values())
        times = sorted(row["seconds"] for row in rows)
        median_corpus = statistics.median(t["warm_corpus_seconds"] for t in group)
        summaries.append({
            "backend": backend, "trials": len(group), "pages_per_trial": len(group[0]["rows"]),
            "median_corpus_seconds": median_corpus,
            "pages_per_second": len(group[0]["rows"]) / median_corpus,
            "p50_page_seconds": statistics.median(times),
            "p95_page_seconds_nearest_rank": times[max(0, (95 * len(times) + 99) // 100 - 1)],
            "median_cold_first_page_seconds": statistics.median(t["cold_first_page_seconds"] for t in group),
            "max_sampled_tree_rss_mb": max(t["sampled_peak_tree_rss_bytes"] for t in group) / 1024**2,
            "cer_micro": sum(row["char_edits"] for row in rows) / max(1, sum(row["ref_chars"] for row in rows)),
            "wer_micro": sum(row["word_edits"] for row in rows) / max(1, sum(row["ref_words"] for row in rows)),
            "control_occurrences_expected": expected, "control_occurrences_missed": missed,
            "control_occurrences_extra": extra, "control_occurrence_recall": 1 - missed / max(1, expected),
            "positive_page_term_pairs": page_positive, "missed_page_term_pairs": page_missed,
            "page_term_recall": 1 - page_missed / max(1, page_positive),
            "false_positive_page_term_pairs": sum(t["expected"] == 0 and t["found"] > 0 for row in rows for t in row["terms"].values()),
            "empty_pages": sum(row["empty"] for row in rows),
            "stable_text_per_page": all(len({row["text_sha256"] for row in rows if row["page"] == page}) == 1 for page in {row["page"] for row in rows}),
            "warmup_matches_measured": all(next(w["text_sha256"] for w in t["warmup"] if w["page"] == row["page"]) == row["text_sha256"] for t in group for row in t["rows"]),
        })
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=ROOT / "Tesseract-OCR")
    parser.add_argument("--corpus-dir", type=Path, default=ROOT / "Tesseract-OCR/engine-benchmark")
    parser.add_argument("--font-dir", type=Path, default=Path("/usr/share/fonts/truetype/dejavu"))
    parser.add_argument("--output", type=Path, default=ROOT / "docs/benchmarks/ocr-engines-linux-2026-10-14.json")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--backends", nargs="+", choices=BACKENDS, default=list(BACKENDS))
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--worker", choices=BACKENDS, help=argparse.SUPPRESS)
    parser.add_argument("--trial", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    args.runtime, args.corpus_dir = args.runtime.resolve(), args.corpus_dir.resolve()
    if args.worker:
        print(json.dumps(worker(args), ensure_ascii=False, default=str))
        return 0
    model_manifest = json.loads(MANIFEST.read_text())
    if args.download_models:
        from tools.install_tesseract import download
        for name, item in model_manifest["models"].items():
            download(item["url"], args.runtime / "benchmark-models" / name, item["sha256"])
    corpus = make_corpus(args.corpus_dir, args.font_dir)
    if args.prepare_only:
        print(json.dumps(corpus, ensure_ascii=False, indent=2))
        return 0
    for name, item in model_manifest["models"].items():
        if any(b.startswith("rapid") for b in args.backends):
            if sha256((args.runtime / "benchmark-models" / name).read_bytes()) != item["sha256"]:
                raise ValueError(f"Model hash mismatch: {name}")
    from importlib.metadata import version
    tess_manifest = json.loads((ROOT / "tools/ocr/manifest.json").read_text())
    tess_hashes = {}
    for name, item in tess_manifest["models"]["fast"]["files"].items():
        actual = sha256((args.runtime / "tessdata-fast" / name).read_bytes())
        if actual != item["sha256"]:
            raise ValueError(f"Tesseract model hash mismatch: {name}")
        tess_hashes[name] = actual
    report = {
        "schema": 1, "platform": platform.platform(), "python": platform.python_version(),
        "cpu_count": os.cpu_count(), "worker_cpu_budget": 1, "workers_concurrent": 1,
        "cpu_info": next((line.strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")), "unknown"),
        "versions": {p: version(p) for p in ("rapidocr", "onnxruntime", "numpy", "opencv-python-headless", "pillow", "pymupdf", "psutil", "rapidfuzz", "openvino", "onnx")},
        "tesseract_version": subprocess.check_output([str(args.runtime / "bin/tesseract"), "--version"], text=True).splitlines(),
        "tesseract_fast_hashes": tess_hashes, "rapid_models": model_manifest,
        "corpus": corpus, "repeats": args.repeats, "trials": [], "failures": [],
        "scope": "OCR on identical pre-rendered gray PGM; not whole scanner throughput. Corpus rendering timed separately once. Synthetic, not real-world generalization.",
        "metrics_note": "CER/WER normalize case/NFC/whitespace only and penalize reading-order differences. Keyword counts use case-insensitive literal substrings (scanner-style), not semantic matching. Peak RSS sampled every 10ms, includes Python and CLI child; shared pages may be double-counted.",
    }
    env = {**os.environ, "OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1",
           "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for trial in range(args.repeats):
        offset = trial % len(args.backends)
        for backend in args.backends[offset:] + args.backends[:offset]:
            print(f"Trial {trial + 1}/{args.repeats}: {backend}", file=sys.stderr, flush=True)
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", backend,
                       "--trial", str(trial), "--runtime", str(args.runtime), "--corpus-dir", str(args.corpus_dir)]
            started = time.perf_counter()
            try:
                completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300, check=True)
                result = json.loads(completed.stdout)
                result["worker_wall_seconds"] = time.perf_counter() - started
                result["worker_stderr"] = completed.stderr
                report["trials"].append(result)
            except (subprocess.SubprocessError, ValueError) as exc:
                report["failures"].append({"backend": backend, "trial": trial, "error": str(exc),
                                           "stderr": str(getattr(exc, "stderr", ""))[-8000:]})
                print(f"FAILED: {backend}: {exc}", file=sys.stderr, flush=True)
            report["summary"] = summarize(report["trials"])
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
