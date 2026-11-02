"""Repository-backed discovery and workflow selection, without network access."""

import json
from pathlib import Path

import pytest
from examples.healthcare.common import sources
from tests.healthcare_support import cda as cda
from tests.healthcare_support import fhir as fhir


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    specs = sources.manifest()
    monkeypatch.setattr(sources, "HEALTHCARE_ROOT", tmp_path)
    monkeypatch.setattr(sources, "manifest", lambda: specs)
    for name in ("hl7", "onc", "vendor"):
        spec = specs[name]
        root = tmp_path / ".cache" / spec["destination"] / Path(spec["entry"]).parent
        root.mkdir(parents=True)
        (root / "README.md").write_text("upstream", encoding="utf-8")
        (root / "full.xml").write_text(
            '<ClinicalDocument xmlns="urn:hl7-org:v3"/>', encoding="utf-8"
        )
        (root / "fragment.XML").write_text('<section xmlns="urn:hl7-org:v3"/>', encoding="utf-8")
        (root / "other.xml").write_text("<other/>", encoding="utf-8")
        (root / "broken.xml").write_text("<broken>", encoding="utf-8")
        (root / "entity.xml").write_text(
            '<!DOCTYPE r [<!ENTITY x "unsafe">]><r>&x;</r>', encoding="utf-8"
        )
    return specs


def test_default_setup_fetches_hl7_and_all_corpora_is_explicit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sources, "install", lambda spec, **kw: calls.append(spec) or Path(spec["destination"])
    )
    assert len(sources.fetch_sources()) == 3
    assert calls[-1]["repository"] == "HL7/C-CDA-Examples"
    assert len(sources.fetch_sources(include_onc=True, include_vendor=True)) == 5


def test_discovery_preserves_repository_provenance_and_classifies_inputs(repositories):
    from examples.healthcare.common.corpus import discover

    for name in ("hl7", "onc", "vendor"):
        records = discover(name)
        assert [r["path"] for r in records] == [
            "broken.xml",
            "entity.xml",
            "fragment.XML",
            "full.xml",
            "other.xml",
        ]
        assert [r["kind"] for r in records] == [
            "malformed",
            "malformed",
            "fragment",
            "document",
            "other",
        ]
        assert records[3]["selector"] == f"{name}:full.xml"
        assert records[3]["revision"] == repositories[name]["revision"]
        assert records[3]["license"] == repositories[name]["license"]
        assert records[3]["source_url"].endswith("/full.xml")
        assert "entity" in records[1]["error"].lower() or "dtd" in records[1]["error"].lower()


def test_select_repository_document_and_reject_escape(repositories, tmp_path):
    path = sources.resolve_document("onc:full.xml")
    assert path == sources.corpus_root("onc") / "full.xml"
    assert sources.resolve_document(str(path)) == path
    for selector in ("onc:../escape.xml", "onc:/absolute.xml"):
        with pytest.raises(ValueError, match="repository"):
            sources.resolve_document(selector)
    outside = tmp_path / "outside.xml"
    outside.write_text("<r/>", encoding="utf-8")
    (sources.corpus_root("onc") / "link.xml").symlink_to(outside)
    with pytest.raises(ValueError, match="repository"):
        sources.resolve_document("onc:link.xml")
    with pytest.raises(FileNotFoundError):
        sources.resolve_document("onc:missing.xml")


def test_missing_corpus_explains_setup_and_default_aliases_are_repo_backed(tmp_path, monkeypatch):
    specs = sources.manifest()
    monkeypatch.setattr(sources, "HEALTHCARE_ROOT", tmp_path)
    monkeypatch.setattr(sources, "manifest", lambda: specs)
    with pytest.raises(FileNotFoundError, match="download_sources"):
        sources.document_path("discharge")
    assert specs["documents"]["discharge"]["repository"] == "hl7"
    assert (
        specs["hl7"]["sha256"] == "2c83f1814e90a063d9292c636ba33aebc5567386c144c561499fabb76ce50e98"
    )


def test_corpus_cli_filters_and_lists_all_three_repositories(repositories, capsys):
    from examples.healthcare import corpus

    assert corpus.main(["--repo", "all", "--kind", "document", "--match", "full"]) == 0
    records = json.loads(capsys.readouterr().out)
    assert [r["selector"] for r in records] == ["hl7:full.xml", "onc:full.xml", "vendor:full.xml"]
    assert corpus.main(["--repo", "hl7", "--kind", "all"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 5


def test_default_workflow_selectors_use_fetched_documents(repositories, monkeypatch, capsys):
    from examples.healthcare.bad_data_doctor import doctor

    calls = []

    async def run(document, index):
        calls.append(document)
        return doctor.DoctorReport(0, 0, (), None, None, None)

    monkeypatch.setattr(doctor, "run", run)
    assert doctor.main(["--document", "vendor:full.xml"]) == 0
    assert calls == ["vendor:full.xml"]


def test_inspection_and_conversion_consume_repository_selectors(
    cda, fhir, repositories, monkeypatch, capsys, tmp_path
):
    from examples.healthcare.cda_to_fhir import convert
    from examples.healthcare.datatypes import inspect_document
    from tests.healthcare_support import document_path

    target = sources.resolve_document("hl7:full.xml")
    target.write_bytes(document_path("discharge").read_bytes())
    monkeypatch.setattr(inspect_document, "compile_cda", lambda: cda)
    monkeypatch.setattr(convert, "compile_cda", lambda: cda)
    monkeypatch.setattr(convert, "compile_fhir", lambda: fhir)
    assert inspect_document.main(["hl7:full.xml"]) == 0
    assert json.loads(capsys.readouterr().out)["patient"]["birth_date"] == "2005-05-01"
    output = tmp_path / "output"
    assert convert.main(["hl7:full.xml", "--output-dir", str(output)]) == 0
    assert (output / "full.fhir.xml").is_file()


def test_namespace_free_cda_examples_are_fragments_not_complete_documents(repositories):
    from examples.healthcare.common.corpus import discover

    root = sources.corpus_root("hl7")
    for name in ("ClinicalDocument", "section", "component", "entryRelationship"):
        (root / f"{name}.xml").write_text(f"<{name}/>", encoding="utf-8")
    records = {r["path"]: r for r in discover("hl7")}
    assert all(
        records[f"{name}.xml"]["kind"] == "fragment"
        for name in ("ClinicalDocument", "section", "component", "entryRelationship")
    )
    assert records["other.xml"]["kind"] == "other"


def test_aliases_resolve_to_original_repository_paths_and_duplicate_outputs_are_rejected(
    repositories, tmp_path
):
    from examples.healthcare.cda_to_fhir.convert import main

    spec = sources.manifest()["documents"]["discharge"]
    target = sources.corpus_root("hl7") / spec["path"]
    target.parent.mkdir(parents=True)
    target.write_text("<ClinicalDocument/>", encoding="utf-8")
    assert sources.resolve_document("discharge") == target
    with pytest.raises(SystemExit) as exc:
        main(["discharge", f"hl7:{spec['path']}", "--output-dir", str(tmp_path / "out")])
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()


def test_inventory_budget_and_missing_repository_fail_explicitly(
    repositories, monkeypatch, capsys, tmp_path
):
    from examples.healthcare import corpus
    from examples.healthcare.common import corpus as inventory

    monkeypatch.setattr(inventory, "MAX_XML_BYTES", 1)
    assert all("budget" in r["error"] for r in inventory.discover("hl7"))
    monkeypatch.setattr(sources, "HEALTHCARE_ROOT", tmp_path / "empty")
    assert corpus.main([]) == 1
    assert "download_sources" in capsys.readouterr().err


def test_corpus_and_download_entrypoints(monkeypatch, capsys):
    import runpy

    from examples.healthcare import download_sources

    calls = []
    monkeypatch.setattr(download_sources, "fetch_sources", lambda **kw: calls.append(kw) or [])
    assert download_sources.main(["--all-corpora"]) == 0
    assert calls == [{"include_onc": True, "include_vendor": True, "force": False}]
    monkeypatch.setattr("sys.argv", ["corpus.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path("examples/healthcare/corpus.py", run_name="__main__")
    assert exc.value.code == 0
    assert "--repo" in capsys.readouterr().out
