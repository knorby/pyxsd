"""Benchmarks distinguish supported policy skips from unexpected failures."""

import pytest

pytest.importorskip("pydantic")

from benchmarks import mcp_workflows as benchmark

from pyxsd.mcp.config import Limits
from pyxsd.mcp.snapshots import WorkspacePolicyError


def test_musicxml_unexpected_failure_is_not_a_successful_skip(monkeypatch):
    def fail(*args):
        raise RuntimeError("benchmark harness broke")

    monkeypatch.setattr(benchmark, "_run", fail)
    with pytest.raises(RuntimeError, match="benchmark harness broke"):
        benchmark._musicxml_metrics(Limits())


def test_musicxml_policy_denial_is_an_explicit_unbenchmarked_result(monkeypatch):
    def deny(*args):
        raise WorkspacePolicyError("dangerous_xml", "DTD is forbidden")

    monkeypatch.setattr(benchmark, "_run", deny)
    result = benchmark._musicxml_metrics(Limits())
    assert result["available"] is True
    assert result["benchmarked"] is False
    assert "dangerous_xml" in result["reason"]
