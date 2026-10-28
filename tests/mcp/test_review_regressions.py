"""Boundary regressions found during the D1-E4 review."""

import pytest

pytest.importorskip("pydantic")

from pyxsd.mcp.config import Limits
from pyxsd.mcp.state import State


def test_corpus_limit_cannot_exceed_operator_maximum():
    assert Limits(corpus_files=1000).corpus_files == 1000
    assert Limits(corpus_files=2, corpus_max_files=2).corpus_files == 2
    with pytest.raises(ValueError, match="corpus"):
        Limits(corpus_files=1001)
    with pytest.raises(ValueError, match="corpus"):
        Limits(corpus_files=3, corpus_max_files=2)


def test_fresh_state_has_publications_without_a_jobs_table(tmp_path):
    import anyio

    async def scenario():
        with State(tmp_path / "state") as state:
            tables = await state.run(
                lambda db: {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            )
        assert "publications" in tables
        assert "jobs" not in tables

    anyio.run(scenario)
