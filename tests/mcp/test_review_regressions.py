"""Boundary regressions from the local output-store review."""

import pytest

pytest.importorskip("pydantic")

from pyxsd.mcp.config import Limits


def test_corpus_limit_cannot_exceed_operator_maximum():
    assert Limits(corpus_files=1000).corpus_files == 1000
    assert Limits(corpus_files=2, corpus_max_files=2).corpus_files == 2
    with pytest.raises(ValueError, match="corpus"):
        Limits(corpus_files=1001)
    with pytest.raises(ValueError, match="corpus"):
        Limits(corpus_files=3, corpus_max_files=2)


def test_historical_quota_and_ttl_settings_are_gone():
    for removed in (
        "artifact_entries",
        "snapshot_revisions",
        "managed_bytes",
        "workspace_idle_seconds",
        "preview_seconds",
        "diagnostic_seconds",
    ):
        assert removed not in Limits.model_fields
