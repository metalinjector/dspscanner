"""Install pinned official GitHub OCR models, optionally build Tesseract on Linux.

Models-only is portable (Windows/Linux/macOS) and needs only Python 3.10+.
Place the platform's Tesseract executable in ROOT or ROOT/bin first, or use
--build on Linux (C/C++, make, CMake >= 3.22). No admin rights required.
Runtime/models/build artifacts stay under the git-ignored Tesseract-OCR/.

  python tools/install_tesseract.py --build
  python tools/install_tesseract.py --tiers fast medium best
  python tools/install_tesseract.py --verify-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).parent / "ocr" / "manifest.json"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def download(url: str, target: Path, sha256: str) -> None:
    """Verify before replace; an interrupted download never destroys a model."""
    if target.is_file() and digest(target) == sha256:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"Download: {target.name}", file=sys.stderr, flush=True)
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.raw+json", "User-Agent": "DSPScanner-OCR-setup",
    })
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(request, timeout=120) as response:
            shutil.copyfileobj(response, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if digest(temporary) != sha256:
            raise ValueError(f"SHA-256 mismatch for {target.name}; existing file preserved")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def unpack_source(archive: Path, target: Path) -> None:
    """Extract only files/directories, rejecting traversal and links on Python 3.10+."""
    if target.exists():
        raise FileExistsError(f"Source target already exists: {target}")
    with tempfile.TemporaryDirectory(prefix="source-", dir=target.parent) as temp:
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            for member in members:
                parts = Path(member.name.replace("\\", "/")).parts
                if (not (member.isfile() or member.isdir()) or not parts
                        or member.name.startswith(("/", "\\")) or ".." in parts or ":" in member.name):
                    raise ValueError(f"Unsafe archive member: {member.name}")
            # A fresh private directory and no links: safe without 3.12's filter API.
            bundle.extractall(temp, members=members)
        children = list(Path(temp).iterdir())
        if len(children) != 1 or not children[0].is_dir():
            raise ValueError("Expected one source directory")
        children[0].rename(target)


def build_engine(root: Path, manifest: dict, jobs: int) -> None:
    if sys.platform != "linux":
        raise ValueError("--build supports Linux; install your platform's Tesseract, then install models without --build")
    cmake = shutil.which("cmake")
    if not cmake:
        venv_cmake = Path(sys.executable).parent / "cmake"
        if venv_cmake.is_file():
            cmake = str(venv_cmake)
    if not cmake or not shutil.which("cc") or not shutil.which("c++") or not shutil.which("make"):
        raise ValueError("Need C/C++, make and CMake >= 3.22. Install CMake with: python -m pip install 'cmake>=3.22,<4'")
    flags = {
        "zlib": ["-DZLIB_BUILD_EXAMPLES=OFF"],
        "png": ["-DPNG_SHARED=OFF", "-DPNG_TESTS=OFF", "-DPNG_TOOLS=OFF"],
        "jpeg": ["-DENABLE_SHARED=OFF", "-DWITH_TURBOJPEG=OFF", "-DWITH_SIMD=OFF"],
        "leptonica": ["-DBUILD_SHARED_LIBS=OFF", "-DBUILD_PROG=OFF", "-DENABLE_GIF=OFF",
                      "-DENABLE_TIFF=OFF", "-DENABLE_WEBP=OFF", "-DENABLE_OPENJPEG=OFF"],
        "tesseract": ["-DBUILD_SHARED_LIBS=OFF", "-DBUILD_TRAINING_TOOLS=OFF", "-DGRAPHICS_DISABLED=ON",
                      "-DDISABLE_ARCHIVE=ON", "-DDISABLE_CURL=ON", "-DDISABLE_TIFF=ON", "-DOPENMP_BUILD=ON"],
    }
    for name, item in manifest["sources"].items():
        workspace = root / "build" / f"{name}-{item['tag']}"
        workspace.mkdir(parents=True, exist_ok=True)
        archive = workspace / "source.tar.gz"
        download(f"https://codeload.github.com/{item['repository']}/tar.gz/refs/tags/{item['tag']}",
                 archive, item["sha256"])
        source = workspace / "source"
        if not source.exists():
            unpack_source(archive, source)
        output = workspace / "compiled"
        command = [cmake, "-S", str(source), "-B", str(output),
                   "-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_INSTALL_PREFIX={root}",
                   f"-DCMAKE_PREFIX_PATH={root}", "-DCMAKE_INSTALL_LIBDIR=lib",
                   "-DCMAKE_POSITION_INDEPENDENT_CODE=ON", "-DCMAKE_INSTALL_RPATH=$ORIGIN/../lib",
                   *flags[name]]
        # Keep stdout free for the machine-readable final verification report.
        subprocess.run(command, check=True, stdout=sys.stderr)
        subprocess.run([cmake, "--build", str(output), "--parallel", str(jobs)], check=True, stdout=sys.stderr)
        subprocess.run([cmake, "--install", str(output)], check=True, stdout=sys.stderr)


def find_binary(root: Path) -> Path:
    for path in (root / "tesseract.exe", root / "tesseract", root / "bin/tesseract.exe", root / "bin/tesseract"):
        if path.is_file():
            return path
    raise FileNotFoundError(f"Tesseract not found in {root} or its bin directory; use --build on Linux")


def verify(root: Path, manifest: dict, tiers: list[str], languages: list[str]) -> dict:
    binary = find_binary(root)
    kwargs = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
              "check": True, "timeout": 30}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    version = subprocess.run([str(binary), "--version"], **kwargs)
    report = {"binary": str(binary), "version": version.stdout.splitlines(), "tiers": {}}
    for tier in tiers:
        directory = root / f"tessdata-{tier}"
        files = {}
        for lang in languages:
            name = lang + ".traineddata"
            file = directory / name
            expected = manifest["models"][tier]["files"][name]["sha256"]
            if not file.is_file() or digest(file) != expected:
                raise ValueError(f"Missing or invalid model: {file}")
            files[name] = expected
        env = {**os.environ, "TESSDATA_PREFIX": str(directory), "OMP_THREAD_LIMIT": "1"}
        result = subprocess.run([str(binary), "--list-langs"], env=env, **kwargs)
        available = {line.strip() for line in result.stdout.splitlines()}
        if not set(languages).issubset(available):
            raise ValueError(f"Tesseract cannot see requested languages in {directory}")
        report["tiers"][tier] = {"directory": str(directory), "sha256": files, "languages": languages}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT / "Tesseract-OCR")
    parser.add_argument("--tiers", nargs="+", choices=("fast", "medium", "best"), default=["fast", "medium", "best"])
    parser.add_argument("--languages", nargs="+", choices=("rus", "eng"), default=["rus", "eng"])
    parser.add_argument("--build", action="store_true", help="compile pinned GitHub sources on Linux")
    parser.add_argument("--verify-only", action="store_true", help="offline hashes + binary + languages; no installation")
    parser.add_argument("--jobs", type=int, default=min(2, os.cpu_count() or 1))
    args = parser.parse_args()
    if args.jobs < 1 or (args.build and args.verify_only):
        parser.error("jobs must be positive; --build and --verify-only are mutually exclusive")
    root = args.root.expanduser().resolve()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    try:
        if not args.verify_only:
            root.mkdir(parents=True, exist_ok=True)
            if args.build:
                build_engine(root, manifest, args.jobs)
            for tier in args.tiers:
                item = manifest["models"][tier]
                for lang in args.languages:
                    name = lang + ".traineddata"
                    url = f"https://api.github.com/repos/{item['repository']}/contents/{name}?ref={item['commit']}"
                    download(url, root / f"tessdata-{tier}" / name, item["files"][name]["sha256"])
        print(json.dumps(verify(root, manifest, args.tiers, args.languages), ensure_ascii=False, indent=2))
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f"OCR setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
