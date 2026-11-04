"""Offline source installation, integrity, and path safety."""

import hashlib
import io
import json
import struct
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
    from tests.healthcare_support import document_path

    for name, spec in sources.manifest()["documents"].items():
        assert hashlib.sha256(document_path(name).read_bytes()).hexdigest() == spec["sha256"]
    with pytest.raises(KeyError):
        sources.document_path("../untrusted")


def test_missing_cache_marker_force_replacement_and_archive_directories(tmp_path, monkeypatch):
    from examples.healthcare.common import sources

    data = archive({"schema/": b"", "schema/main.xsd": b"new"})
    spec = {
        "url": "https://example.test",
        "sha256": hashlib.sha256(data).hexdigest(),
        "entry": "schema/main.xsd",
        "destination": "schemas/test",
    }
    target = tmp_path / spec["destination"]
    target.mkdir(parents=True)
    (target / "obsolete").write_text("old", encoding="utf-8")
    with pytest.raises(sources.SourceVerificationError, match="corrupt cache"):
        sources.install(spec, root=tmp_path)
    monkeypatch.setattr(sources, "_download", lambda url: data)
    sources.install(spec, root=tmp_path, force=True)
    assert not (target / "obsolete").exists()
    assert (target / spec["entry"]).read_bytes() == b"new"
    marker = target / ".verified.json"
    saved = json.loads(marker.read_text(encoding="utf-8"))
    saved["archive"] = "wrong"
    marker.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(sources.SourceVerificationError, match="corrupt cache"):
        sources.install(spec, root=tmp_path)


def test_download_budget_and_archive_expansion_budget(tmp_path, monkeypatch):
    from examples.healthcare.common import sources

    monkeypatch.setattr(sources.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"zip"))
    assert sources._download("https://example.test") == b"zip"
    monkeypatch.setattr(
        sources.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"x" * 80_000_001)
    )
    with pytest.raises(sources.SourceVerificationError, match="archive too large"):
        sources._download("https://example.test")
    data = bytearray(archive({"main.xsd": b"x"}))
    # The ZIP central directory declares size before any member is expanded.
    struct.pack_into("<I", data, data.index(b"PK\x01\x02") + 24, 300_000_001)
    data = bytes(data)
    monkeypatch.setattr(sources, "_download", lambda url: data)
    with pytest.raises(sources.SourceVerificationError, match="expanded archive too large"):
        sources.install(
            {
                "url": "https://example.test",
                "sha256": hashlib.sha256(data).hexdigest(),
                "entry": "main.xsd",
                "destination": "schemas/test",
            },
            root=tmp_path,
        )
    assert not (tmp_path / "schemas/test").exists()


@pytest.mark.parametrize("member", ["/absolute.xsd", "dir\\main.xsd"])
def test_other_unsafe_members(tmp_path, monkeypatch, member):
    from examples.healthcare.common import sources

    data = archive({member: b"x"})
    monkeypatch.setattr(sources, "_download", lambda url: data)
    with pytest.raises(sources.SourceVerificationError, match="unsafe"):
        sources.install(
            {
                "url": "https://example.test",
                "sha256": hashlib.sha256(data).hexdigest(),
                "entry": "main.xsd",
                "destination": "schemas/test",
            },
            root=tmp_path,
        )


def test_fetch_selected_corpora_and_schema_paths(tmp_path, monkeypatch):
    from examples.healthcare.common import sources

    specs = sources.manifest()
    monkeypatch.setattr(sources, "HEALTHCARE_ROOT", tmp_path)
    monkeypatch.setattr(sources, "manifest", lambda: specs)
    with pytest.raises(FileNotFoundError, match="download_sources"):
        sources.cda_schema_path()
    calls = []

    def install(spec, *, root, force):
        target = root / spec["destination"]
        entry = target / spec["entry"]
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(b"x")
        calls.append((spec, force))
        return target

    monkeypatch.setattr(sources, "install", install)
    assert len(sources.fetch_sources()) == 3
    assert sources.cda_schema_path().is_file() and sources.fhir_schema_path().is_file()
    assert len(sources.fetch_sources(include_onc=True, include_vendor=True, force=True)) == 5
    assert [spec["revision"] for spec, force in calls[-2:]] == [
        specs["onc"]["revision"],
        specs["vendor"]["revision"],
    ]
    assert all(force for _, force in calls[-5:])
