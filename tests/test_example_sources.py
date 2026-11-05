"""Offline tests for the example families' shared source spine (no network).

Each self-contained family under ``examples/`` carries its own copy of the
pinned-source installer; these tests run against every copy so the five
cannot drift. Synthetic specs mirror the two manifest section kinds; each
family's real ``sources.toml`` is checked for shape without being installed.
"""

import hashlib
import io
import json
import zipfile
from importlib import import_module
from pathlib import Path

import pytest

FAMILIES = ["bpmn", "citygml", "musicxml", "opendrive", "xrechnung"]

# Each family pins exactly its own manifest sections; guard against an
# extraction bug or a copy-paste dragging another family's sources in.
EXPECTED_SECTIONS = {
    "bpmn": {"bpmn-schema", "bpmn-data"},
    "citygml": {"citygml-schema", "citygml-gml"},
    "musicxml": {"musicxml-schema"},
    "opendrive": {"opendrive-schema", "opendrive-data"},
    "xrechnung": {"ubl-schema", "ubl-data"},
}


def _sources(family: str):
    return import_module(f"examples.{family}.common.sources")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def served(monkeypatch, request):
    """Serve local bytes at synthetic URLs by patching the family downloader."""
    try:
        family = request.node.callspec.params["family"]
    except (AttributeError, KeyError):
        pytest.fail("the served fixture requires @pytest.mark.parametrize('family', ...)")
    sources_module = _sources(family)
    table: dict[str, bytes] = {}

    def fake_download(url: str) -> bytes:
        if url not in table:
            raise sources_module.SourceVerificationError(f"not served: {url}")
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


@pytest.mark.parametrize("family", FAMILIES)
def test_manifest_sections_are_well_formed(family):
    specs = _sources(family).manifest()
    assert set(specs) == EXPECTED_SECTIONS[family], family
    for name, spec in specs.items():
        assert spec["kind"] in ("archive", "files"), (family, name)
        assert spec["destination"] and spec["entry"] and spec["license"] and spec["role"], (
            family,
            name,
        )
        if spec["kind"] == "files":
            assert spec["files"], name
            for file_spec in spec["files"]:
                assert not Path(file_spec["path"]).is_absolute(), (family, name)
                assert ".." not in Path(file_spec["path"]).parts, (family, name)
                assert len(file_spec["sha256"]) == 64, (family, name)
        else:
            assert len(spec["sha256"]) == 64, (family, name)
            assert spec["url"].startswith("https://"), (family, name)


@pytest.mark.parametrize("family", FAMILIES)
def test_install_files_section_verifies_and_reuses_without_network(family, tmp_path, served):
    sources = _sources(family)
    url = served("doc.xml", b"<doc/>")
    spec = _file_spec(url, "doc.xml", _sha(b"<doc/>"))
    target = sources.install(spec, cache_root=tmp_path)
    assert (target / "doc.xml").read_bytes() == b"<doc/>"
    marker = json.loads((target / f".{spec['name']}.verified.json").read_text())
    assert marker["files"] == {"doc.xml": _sha(b"<doc/>")}

    # The valid cache verifies without any download: a spec whose URL was
    # never served still succeeds because the network is never touched.
    unseeded = dict(spec)
    unseeded["files"] = [dict(spec["files"][0], url="https://synthetic.test/never-served")]
    assert sources.install(unseeded, cache_root=tmp_path) == target


@pytest.mark.parametrize("family", FAMILIES)
def test_install_rejects_digest_mismatch(family, tmp_path, served):
    sources = _sources(family)
    url = served("doc.xml", b"<doc/>")
    spec = _file_spec(url, "doc.xml", "0" * 64)
    with pytest.raises(sources.SourceVerificationError, match="digest mismatch"):
        sources.install(spec, cache_root=tmp_path)


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


@pytest.mark.parametrize("family", FAMILIES)
def test_install_archive_section(family, tmp_path, served):
    sources = _sources(family)
    archive = _zip_bytes({"a/x.xsd": b"<xs:schema/>", "README.md": b"readme"})
    spec = _archive_spec(served("bundle.zip", archive), _sha(archive))
    target = sources.install(spec, cache_root=tmp_path)
    assert (target / "a" / "x.xsd").read_bytes() == b"<xs:schema/>"

    tampered = _zip_bytes({"a/x.xsd": b"<evil/>", "README.md": b"readme"})
    bad = _archive_spec(served("bundle2.zip", tampered), _sha(archive), name="synthetic-other")
    with pytest.raises(sources.SourceVerificationError, match="digest mismatch"):
        sources.install(bad, cache_root=tmp_path)


@pytest.mark.parametrize("family", FAMILIES)
def test_install_rejects_zip_slip(family, tmp_path, served):
    sources = _sources(family)
    archive = _zip_bytes({"../escape.txt": b"nope", "README.md": b"readme"})
    spec = _archive_spec(served("slip.zip", archive), _sha(archive))
    with pytest.raises(sources.SourceVerificationError, match="unsafe archive member"):
        sources.install(spec, cache_root=tmp_path)


@pytest.mark.parametrize("family", FAMILIES)
def test_corrupt_cache_demands_force(family, tmp_path, served):
    sources = _sources(family)
    url = served("doc.xml", b"<doc/>")
    spec = _file_spec(url, "doc.xml", _sha(b"<doc/>"))
    target = sources.install(spec, cache_root=tmp_path)
    (target / "doc.xml").write_bytes(b"<tampered/>")
    with pytest.raises(sources.SourceVerificationError, match="--force"):
        sources.install(spec, cache_root=tmp_path)
    sources.install(spec, cache_root=tmp_path, force=True)
    assert (target / "doc.xml").read_bytes() == b"<doc/>"


@pytest.mark.parametrize("family", FAMILIES)
def test_localize_rewrites_absolute_imports_and_keeps_pristine(family, tmp_path):
    sources = _sources(family)
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

    changed = sources.localize(root)
    assert changed == [base]
    rewritten = base.read_text(encoding="utf-8")
    assert 'schemaLocation="../../gml/3.1.1/base/gml.xsd"' in rewritten
    # Unknown hosts stay untouched rather than being rewritten blindly.
    assert 'and="http://example.org/keep-me.xsd"' in rewritten
    pristine = root / ".pristine" / "citygml" / "2.0" / "cityGMLBase.xsd"
    assert pristine.read_text(encoding="utf-8") == original
    # Localization is idempotent: the URL is gone, nothing new changes.
    assert sources.localize(root) == []


@pytest.mark.parametrize("family", FAMILIES)
def test_localize_honors_extra_map_for_sibling_rewrites(family, tmp_path):
    sources = _sources(family)
    root = tmp_path / "tree"
    root.mkdir()
    schema = root / "score.xsd"
    schema.write_text(
        '<xs:schema schemaLocation="http://www.musicxml.org/xsd/xml.xsd"/>', encoding="utf-8"
    )
    (root / "xml.xsd").write_text("<xs:schema/>", encoding="utf-8")
    changed = sources.localize(root, {"http://www.musicxml.org/xsd/xml.xsd": "xml.xsd"})
    assert changed == [schema]
    assert 'schemaLocation="xml.xsd"' in schema.read_text(encoding="utf-8")


@pytest.mark.parametrize("family", FAMILIES)
def test_require_cache_and_resolve_sample_hint_at_setup(family, monkeypatch, tmp_path):
    sources = _sources(family)
    # Point the family at an empty cache: the real .cache may be installed
    # on a developer machine, and these paths must still guide setup.
    monkeypatch.setattr(sources, "CACHE_ROOT", tmp_path)
    section = next(iter(sources.manifest()))
    with pytest.raises(FileNotFoundError, match="download_sources"):
        sources.require_cache(section)
    with pytest.raises(ValueError):
        sources.resolve_sample(section, "../../examples/xrechnung/sources.toml")
    with pytest.raises(FileNotFoundError):
        sources.resolve_sample(section, "missing.xml")


@pytest.mark.parametrize(
    "family",
    [
        f
        for f in FAMILIES
        if (Path(__file__).parents[1] / "examples" / f / "common" / "render.py").is_file()
    ],
)
def test_render_helpers_escape_and_wrap(family):
    render = import_module(f"examples.{family}.common.render")
    assert render.esc('<a href="x">&amp;</a>') == "&lt;a href=&quot;x&quot;&gt;&amp;amp;&lt;/a&gt;"
    assert render.num(1.0) == "1"
    assert render.num(-0.0001) == "0"
    assert render.num(2.567) == "2.57"
    out = render.svg("<rect/>", width=800, height=600)
    assert out.startswith('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"')
    assert out.endswith('viewBox="0 0 800 600"><rect/></svg>')
