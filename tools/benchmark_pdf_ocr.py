"""Run from repo root: python -m tools.benchmark_pdf_ocr scan.pdf --term время."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from app.config import APP_VERSION, ScanSettings
from app.readers.pdf_reader import _ocr_tessdata_dir
from app.scanning.scanner import DocumentScanner
from app.settings_store import invalidate_settings_cache, load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--term', action='append', required=True)
    parser.add_argument('--quality', choices=('a2fast', 'adaptive', 'thorough', 'fast150'), default='a2fast')
    parser.add_argument('--tier', choices=('fast', 'medium', 'best'), default='fast')
    parser.add_argument('--russian-only', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--repeat', type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20 or any(not p.is_file() for p in args.files):
        parser.error('Нужны существующие PDF и repeat от 1 до 20')
    os.environ['DSP_SCANNER_OCR_QUALITY'] = args.quality
    os.environ['DSP_SCANNER_OCR_MODEL_TIER'] = args.tier
    os.environ['DSP_SCANNER_RUSSIAN_ONLY'] = str(args.russian_only)
    invalidate_settings_cache()
    app = load_settings()
    if not app.tesseract_path:
        parser.error('Tesseract не найден')
    directory = _ocr_tessdata_dir(app.tesseract_path, args.tier)
    models = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in directory.glob('*.traineddata')} if directory else {}
    engine = subprocess.run([app.tesseract_path, '--version'], capture_output=True, text=True, timeout=15)
    output = {'version': APP_VERSION, 'engine': engine.stdout.splitlines()[0],
              'cpu_count': os.cpu_count(), 'quality': args.quality, 'tier': args.tier,
              'model_directory': str(directory), 'model_sha256': models,
              'ocr_workers': app.ocr_workers or min(8, os.cpu_count() or 1), 'runs': []}
    scan = ScanSettings(paths=[str(p.resolve()) for p in args.files], words=args.term,
                        file_types={'.pdf'}, max_matches_per_word=10000, max_file_size_mb=1024)
    for number in range(args.repeat):
        report = DocumentScanner().run(scan)
        output['runs'].append({'run': number + 1, 'seconds': round(report.elapsed_seconds, 3),
                               'matches': {term: sum(r.word == term for r in report.results) for term in args.term},
                               'cache_hits': report.stats.cache_hits,
                               'issues': [{'file': e.file_path, 'message': e.message} for e in report.errors]})
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
