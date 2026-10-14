"""Offline tests for the reproducible GitHub installer."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from tools import install_tesseract as setup


def test_download_verifies_then_atomically_replaces(tmp_path, monkeypatch):
    target = tmp_path / "model.traineddata"
    target.write_bytes(b"old valid model")
    payload = b"new model"
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(payload))
    setup.download("https://api.github.com/example", target, hashlib.sha256(payload).hexdigest())
    assert target.read_bytes() == payload
    assert [path for path in tmp_path.iterdir() if path.is_file()] == [target]


def test_bad_hash_keeps_existing_model(tmp_path, monkeypatch):
    target = tmp_path / "model.traineddata"
    target.write_bytes(b"old valid model")
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"corrupt download"))
    with pytest.raises(ValueError, match="SHA-256"):
        setup.download("https://api.github.com/example", target, hashlib.sha256(b"expected model").hexdigest())
    assert target.read_bytes() == b"old valid model"
    assert [path for path in tmp_path.iterdir() if path.is_file()] == [target]


def test_already_verified_model_needs_no_network(tmp_path, monkeypatch):
    target = tmp_path / "model.traineddata"
    target.write_bytes(b"model")
    monkeypatch.setattr(setup.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("unexpected download"))
    setup.download("https://api.github.com/example", target, hashlib.sha256(b"model").hexdigest())


@pytest.mark.parametrize("name,kind", [("../escape", tarfile.REGTYPE), ("/escape", tarfile.REGTYPE),
                                       ("source/link", tarfile.SYMTYPE), ("source/link", tarfile.LNKTYPE)])
def test_unsafe_source_archive_is_rejected(tmp_path, name, kind):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        entry.linkname = "../../escape"
        bundle.addfile(entry)
    target = tmp_path / "unpacked"
    with pytest.raises(ValueError, match="Unsafe archive"):
        setup.unpack_source(archive, target)
    assert not target.exists()
    assert not (tmp_path / "escape").exists()


def test_source_archive_extracts_into_expected_directory(tmp_path):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        entry = tarfile.TarInfo("project-v1/README")
        entry.size = 4
        bundle.addfile(entry, io.BytesIO(b"read"))
    setup.unpack_source(archive, tmp_path / "source")
    assert (tmp_path / "source/README").read_bytes() == b"read"


def test_manifest_pins_all_six_models():
    manifest = json.loads(setup.MANIFEST.read_text())
    assert set(manifest["models"]) == {"fast", "medium", "best"}
    for item in manifest["models"].values():
        assert len(item["commit"]) == 40
        assert set(item["files"]) == {"rus.traineddata", "eng.traineddata"}
        for model in item["files"].values():
            assert len(model["sha256"]) == 64
            assert model["bytes"] > 1_000_000
