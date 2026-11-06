"""Benchmarks distinguish supported policy skips from unexpected failures."""

import pytest

pytest.importorskip("pydantic")

from benchmarks import mcp_workflows as benchmark

from pyxsd.mcp.captures import InputPolicyError
from pyxsd.mcp.config import Limits


def _seed_musicxml(root):
    example = root / "examples" / "musicxml"
    schemas = example / ".cache" / "musicxml-schemas" / "schema"
    schemas.mkdir(parents=True)
    (schemas / "musicxml.xsd").write_text("<xs:schema/>", encoding="utf-8")
    (example / "instance.xml").write_text("<score/>", encoding="utf-8")


def test_musicxml_unexpected_failure_is_not_a_successful_skip(tmp_path, monkeypatch):
    _seed_musicxml(tmp_path)
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)

    def fail(*args):
        raise RuntimeError("benchmark harness broke")

    monkeypatch.setattr(benchmark, "_run", fail)
    with pytest.raises(RuntimeError, match="benchmark harness broke"):
        benchmark._musicxml_metrics(Limits())


def test_musicxml_policy_denial_is_an_explicit_unbenchmarked_result(tmp_path, monkeypatch):
    _seed_musicxml(tmp_path)
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)

    def deny(*args):
        raise InputPolicyError("dangerous_xml", "DTD is forbidden")

    monkeypatch.setattr(benchmark, "_run", deny)
    result = benchmark._musicxml_metrics(Limits())
    assert result["available"] is True
    assert result["benchmarked"] is False
    assert "dangerous_xml" in result["reason"]


def test_musicxml_missing_cache_points_to_existing_downloader(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    result = benchmark._musicxml_metrics(Limits())
    assert result["available"] is False
    assert "examples/musicxml/download_sources.py" in result["reason"]


def test_musicxml_cached_schema_is_benchmarked_without_unrelated_files(tmp_path, monkeypatch):
    _seed_musicxml(tmp_path)
    example = tmp_path / "examples" / "musicxml"
    (example / ".cache" / "musicxml-schemas" / "schema" / "musicxml.xsd").write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="score" type="xs:string"/></xs:schema>',
        encoding="utf-8",
    )
    (example / "unrelated.txt").write_text("not an input", encoding="utf-8")
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    result = benchmark._musicxml_metrics(Limits())
    assert result["available"] is True
    assert result["files"] == 2
    assert (result["cold_compiles"], result["warm_compiles"]) == (1, 0)
