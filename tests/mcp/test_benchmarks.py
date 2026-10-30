"""Benchmarks distinguish supported policy skips from unexpected failures."""

import pytest

pytest.importorskip("pydantic")

from benchmarks import mcp_workflows as benchmark

from pyxsd.mcp.captures import InputPolicyError
from pyxsd.mcp.config import Limits


def _seed_musicxml(root):
    example = root / "examples" / "musicxml"
    (example / "schemas").mkdir(parents=True)
    (example / "schemas" / "musicxml.xsd").write_text("<xs:schema/>", encoding="utf-8")
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
