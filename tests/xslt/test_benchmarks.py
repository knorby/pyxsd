"""XSLT engine benchmarks report stable counters without timing thresholds."""

from __future__ import annotations

import json
import platform
import tracemalloc

import pytest
from benchmarks import xslt_engine

from pyxsd import __version__

COUNTER_FIELDS = {
    "annotations",
    "documents",
    "frames",
    "max_frame_depth",
    "output_bytes",
    "output_nodes",
    "resource_bytes",
    "steps",
}
OPERATIONS = {"attributes", "grouping"}


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> dict[str, object]:
    assert xslt_engine.main(argv) == 0
    captured = capsys.readouterr()
    return json.loads(captured.out)


def test_operation_counts_are_the_documented_nonnegative_integers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _run(capsys, ["--operation-counts"])
    assert set(payload) == {"python", "pyxsd", "operation_counts"}
    assert payload["python"] == platform.python_version()
    assert payload["pyxsd"] == __version__
    section = payload["operation_counts"]
    assert set(section) == {"limits", "operations"}
    assert set(section["limits"]) == {
        "max_annotations",
        "max_documents",
        "max_frames",
        "max_output_bytes",
        "max_output_nodes",
        "max_resource_bytes",
        "max_steps",
    }
    assert section["operations"].keys() == OPERATIONS
    for row in section["operations"].values():
        assert set(row) == {"compile", "counters", "serialized_bytes"}
        assert set(row["compile"]) == {"modes", "template_rules"}
        assert set(row["counters"]) == COUNTER_FIELDS
        assert all(
            isinstance(value, int) and value >= 0
            for value in (
                *row["compile"].values(),
                *row["counters"].values(),
                row["serialized_bytes"],
            )
        )
        assert row["serialized_bytes"] > 0
        assert row["counters"]["steps"] > 0
        assert row["counters"]["max_frame_depth"] > 0


def test_operation_counts_are_identical_across_two_runs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _run(capsys, ["--operation-counts"])
    second = _run(capsys, ["--operation-counts"])
    assert first["operation_counts"] == second["operation_counts"]


def test_allocation_counts_have_the_documented_shape(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _run(capsys, ["--allocations"])
    assert set(payload) == {"python", "pyxsd", "allocations"}
    section = payload["allocations"]
    assert set(section) == {"operations"}
    assert section["operations"].keys() == OPERATIONS
    for row in section["operations"].values():
        assert set(row) == {"blocks", "bytes", "peak_bytes"}
        assert all(isinstance(value, int) and value >= 0 for value in row.values())
        assert row["peak_bytes"] > 0
    assert not tracemalloc.is_tracing()


def test_selectors_combine_and_default_to_both(capsys: pytest.CaptureFixture[str]) -> None:
    combined = _run(capsys, ["--operation-counts", "--allocations"])
    assert set(combined) == {"python", "pyxsd", "allocations", "operation_counts"}
    default = _run(capsys, [])
    assert set(default) == {"python", "pyxsd", "allocations", "operation_counts"}


def test_output_is_sorted_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert xslt_engine.main(["--operation-counts"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out == json.dumps(payload, sort_keys=True, indent=2) + "\n"


def test_a_wrong_serialized_result_fails_the_benchmark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        xslt_engine.OPERATIONS,
        "attributes",
        (xslt_engine.ATTRIBUTES_STYLESHEET, "<different/>"),
    )
    with pytest.raises(RuntimeError, match="benchmark operation 'attributes' produced"):
        compiled = xslt_engine._compile("attributes")
        xslt_engine._invoke(compiled, "attributes")
