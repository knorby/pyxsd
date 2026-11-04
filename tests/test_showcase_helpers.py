"""Offline tests for the showcase source spine (no network, no real cache).

Synthetic specs mirror the two manifest section kinds; the real
``sources.toml`` is checked for shape without being installed.
"""

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from examples.showcase.common import render
from examples.showcase.common import sources as sources_module
from examples.showcase.common.sources import (
    SourceVerificationError,
    install,
    localize,
    manifest,
    require_cache,
    resolve_sample,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def served(monkeypatch):
    """Serve local bytes at synthetic URLs by patching the downloader."""
    table: dict[str, bytes] = {}

    def fake_download(url: str) -> bytes:
        if url not in table:
            raise SourceVerificationError(f"not served: {url}")
        return table[url]

    monkeypatch.setattr(sources_module, "_download", fake_download)

    def add(name: str, data: bytes) -> str:
        url = f"https://synthetic.test/{name}"
        table.setdefault(url, data)
        return url

    return add


def _file_spec(url: str, name: str, digest: str, **overrides) -> dict:
    spec = {
        "name": f"synthetic-{name}",
        "kind": "files",
        "destination": "synthetic",
        "entry": name,
        "files": [{"path": name, "url": url, "sha256": digest}],
    }
    spec.update(overrides)
    return spec


def test_manifest_sections_are_well_formed():
    specs = manifest()
    assert len(specs) >= 9
    for name, spec in specs.items():
        assert spec["kind"] in ("archive", "files"), name
        assert spec["destination"] and spec["entry"] and spec["license"] and spec["role"], name
        if spec["kind"] == "files":
            assert spec["files"], name
            for file_spec in spec["files"]:
                assert not Path(file_spec["path"]).is_absolute(), name
                assert ".." not in Path(file_spec["path"]).parts, name
                assert len(file_spec["sha256"]) == 64, name
        else:
            assert len(spec["sha256"]) == 64, name
            assert spec["url"].startswith("https://"), name


def test_install_files_section_verifies_and_reuses_without_network(tmp_path, served):
    url = served("doc.xml", b"<doc/>")
    spec = _file_spec(url, "doc.xml", _sha(b"<doc/>"))
    target = install(spec, cache_root=tmp_path)
    assert (target / "doc.xml").read_bytes() == b"<doc/>"
    marker = json.loads((target / f".{spec['name']}.verified.json").read_text())
    assert marker["files"] == {"doc.xml": _sha(b"<doc/>")}

    # The valid cache verifies without any download: a spec whose URL was
    # never served still succeeds because the network is never touched.
    unseeded = dict(spec)
    unseeded["files"] = [dict(spec["files"][0], url="https://synthetic.test/never-served")]
    assert install(unseeded, cache_root=tmp_path) == target


def test_install_rejects_digest_mismatch(tmp_path, served):
    url = served("doc.xml", b"<doc/>")
    spec = _file_spec(url, "doc.xml", "0" * 64)
    with pytest.raises(SourceVerificationError, match="digest mismatch"):
        install(spec, cache_root=tmp_path)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _archive_spec(url: str, digest: str, name: str = "synthetic-archive") -> dict:
    return {
        "name": name,
        "kind": "archive",
        "destination": name,
        "entry": "README.md",
        "url": url,
        "sha256": digest,
    }


def test_install_archive_section(tmp_path, served):
    archive = _zip_bytes({"a/x.xsd": b"<xs:schema/>", "README.md": b"readme"})
    spec = _archive_spec(served("bundle.zip", archive), _sha(archive))
    target = install(spec, cache_root=tmp_path)
    assert (target / "a" / "x.xsd").read_bytes() == b"<xs:schema/>"

    tampered = _zip_bytes({"a/x.xsd": b"<evil/>", "README.md": b"readme"})
    bad = _archive_spec(served("bundle2.zip", tampered), _sha(archive), name="synthetic-other")
    with pytest.raises(SourceVerificationError, match="digest mismatch"):
        install(bad, cache_root=tmp_path)


def test_install_rejects_zip_slip(tmp_path, served):
    archive = _zip_bytes({"../escape.txt": b"nope", "README.md": b"readme"})
    spec = _archive_spec(served("slip.zip", archive), _sha(archive))
    with pytest.raises(SourceVerificationError, match="unsafe archive member"):
        install(spec, cache_root=tmp_path)


def test_corrupt_cache_demands_force(tmp_path, served):
    url = served("doc.xml", b"<doc/>")
    spec = _file_spec(url, "doc.xml", _sha(b"<doc/>"))
    target = install(spec, cache_root=tmp_path)
    (target / "doc.xml").write_bytes(b"<tampered/>")
    with pytest.raises(SourceVerificationError, match="--force"):
        install(spec, cache_root=tmp_path)
    install(spec, cache_root=tmp_path, force=True)
    assert (target / "doc.xml").read_bytes() == b"<doc/>"


def test_localize_rewrites_absolute_imports_and_keeps_pristine(tmp_path):
    root = tmp_path / "tree"
    (root / "citygml" / "2.0").mkdir(parents=True)
    (root / "gml" / "3.1.1" / "base").mkdir(parents=True)
    (root / "gml" / "3.1.1" / "base" / "gml.xsd").write_text("<xs:schema/>", encoding="utf-8")
    base = root / "citygml" / "2.0" / "cityGMLBase.xsd"
    original = (
        '<xs:schema schemaLocation="http://schemas.opengis.net/gml/3.1.1/base/gml.xsd" '
        'and="http://example.org/keep-me.xsd"/>'
    )
    base.write_text(original, encoding="utf-8")

    changed = localize(root)
    assert changed == [base]
    rewritten = base.read_text(encoding="utf-8")
    assert 'schemaLocation="../../gml/3.1.1/base/gml.xsd"' in rewritten
    # Unknown hosts stay untouched rather than being rewritten blindly.
    assert 'and="http://example.org/keep-me.xsd"' in rewritten
    pristine = root / ".pristine" / "citygml" / "2.0" / "cityGMLBase.xsd"
    assert pristine.read_text(encoding="utf-8") == original
    # Localization is idempotent: the URL is gone, nothing new changes.
    assert localize(root) == []


def test_localize_honors_extra_map_for_sibling_rewrites(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    schema = root / "score.xsd"
    schema.write_text(
        '<xs:schema schemaLocation="http://www.musicxml.org/xsd/xml.xsd"/>', encoding="utf-8"
    )
    (root / "xml.xsd").write_text("<xs:schema/>", encoding="utf-8")
    changed = localize(root, {"http://www.musicxml.org/xsd/xml.xsd": "xml.xsd"})
    assert changed == [schema]
    assert 'schemaLocation="xml.xsd"' in schema.read_text(encoding="utf-8")


def test_require_cache_and_resolve_sample_hint_at_setup(monkeypatch, tmp_path):
    # Point the spine at an empty cache: the real .cache may be installed
    # on a developer machine, and these paths must still guide setup.
    monkeypatch.setattr(sources_module, "CACHE_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError, match="download_sources"):
        require_cache("citygml-schema")
    with pytest.raises(ValueError):
        resolve_sample("bpmn-data", "../../examples/showcase/sources.toml")
    with pytest.raises(FileNotFoundError):
        resolve_sample("bpmn-data", "starter.bpmn")


def test_render_helpers_escape_and_wrap():
    assert render.esc('<a href="x">&amp;</a>') == "&lt;a href=&quot;x&quot;&gt;&amp;amp;&lt;/a&gt;"
    assert render.num(1.0) == "1"
    assert render.num(-0.0001) == "0"
    assert render.num(2.567) == "2.57"
    out = render.svg("<rect/>", width=800, height=600)
    assert out.startswith('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"')
    assert out.endswith('viewBox="0 0 800 600"><rect/></svg>')
