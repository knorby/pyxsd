"""Offline source installation, integrity, and path safety."""

import hashlib
import io
import zipfile

import pytest


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as out:
        for name, data in files.items():
            out.writestr(name, data)
    return stream.getvalue()


def test_sources_install_verified_tree_and_reuse_offline(tmp_path, monkeypatch):
    from examples.healthcare.common import sources

    data = archive({"schema/main.xsd": b"schema", "schema/types.xsd": b"types"})
    spec = {
        "url": "https://example.test/schema.zip",
        "sha256": hashlib.sha256(data).hexdigest(),
        "entry": "schema/main.xsd",
        "destination": "schemas/test",
    }
    monkeypatch.setattr(sources, "_download", lambda url: data)
    target = sources.install(spec, root=tmp_path)
    assert (target / "schema/types.xsd").read_bytes() == b"types"
    monkeypatch.setattr(
        sources, "_download", lambda url: pytest.fail("offline cache re-downloaded")
    )
    assert sources.install(spec, root=tmp_path) == target
    (target / "schema/types.xsd").write_bytes(b"corrupt")
    with pytest.raises(sources.SourceVerificationError, match="cache"):
        sources.install(spec, root=tmp_path)


def test_bad_digest_and_unsafe_archive_do_not_install(tmp_path, monkeypatch):
    from examples.healthcare.common import sources

    data = archive({"../escape": "bad"})
    spec = {
        "url": "https://example.test/a.zip",
        "sha256": "bad",
        "entry": "main.xsd",
        "destination": "schemas/test",
    }
    monkeypatch.setattr(sources, "_download", lambda url: data)
    with pytest.raises(sources.SourceVerificationError, match="digest"):
        sources.install(spec, root=tmp_path)
    spec["sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(sources.SourceVerificationError, match="member"):
        sources.install(spec, root=tmp_path)
    assert not (tmp_path / "schemas/test").exists()
    assert not (tmp_path.parent / "escape").exists()


def test_missing_entry_and_document_provenance(tmp_path, monkeypatch):
    from examples.healthcare.common import sources

    data = archive({"other.xsd": "x"})
    monkeypatch.setattr(sources, "_download", lambda url: data)
    with pytest.raises(FileNotFoundError):
        sources.install(
            {
                "url": "https://example.test/a.zip",
                "sha256": hashlib.sha256(data).hexdigest(),
                "entry": "main.xsd",
                "destination": "schemas/test",
            },
            root=tmp_path,
        )
    for name, spec in sources.manifest()["documents"].items():
        assert (
            hashlib.sha256(sources.document_path(name).read_bytes()).hexdigest() == spec["sha256"]
        )
    with pytest.raises(KeyError):
        sources.document_path("../untrusted")
