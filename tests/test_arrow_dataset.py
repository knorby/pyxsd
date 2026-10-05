"""Manifested datasets preserve exact schemas and isolate failed inputs."""

import json

import pytest

from pyxsd.batch import DocumentSource, InputFailure, ParseOutcome
from pyxsd.integrations import IntegrationError
from pyxsd.validation import IssueSeverity, ValidationIssue

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


def test_augmented_schema_preserves_fields_and_metadata():
    from pyxsd.integrations._dataset import augmented_schema

    original = pa.schema(
        [pa.field("n", pa.decimal128(9, 2), nullable=True, metadata={b"x": b"y"})],
        metadata={b"route": b"r"},
    )
    result = augmented_schema(original)
    assert result.names == ["n", "__pyxsd_source_id", "__pyxsd_row_index"]
    assert result.field(0).equals(original.field(0), check_metadata=True)
    assert result.metadata == original.metadata
    assert result.field(1).type == pa.string() and not result.field(1).nullable
    assert result.field(2).type == pa.int64() and not result.field(2).nullable


@pytest.mark.parametrize("name", ["__pyxsd_source_id", "__pyxsd_row_index"])
def test_augmented_schema_rejects_collision(name):
    from pyxsd.integrations._dataset import augmented_schema

    with pytest.raises(IntegrationError, match="reserved"):
        augmented_schema(pa.schema([(name, pa.string())]))


@pytest.mark.parametrize(
    "status", ["written", "empty", "invalid", "input_error", "projection_error"]
)
def test_entry_is_json_safe_snapshot(status, tmp_path):
    from pyxsd.integrations._dataset import entry_snapshot

    issue = ValidationIssue(IssueSeverity.ERROR, "bad", "sensitive diagnostic", "r")
    outcome = ParseOutcome(
        DocumentSource("../label", tmp_path / "secret.xml"),
        "input_error",
        None,
        (issue,),
        InputFailure("io", "missing"),
    )
    entry = json.loads(json.dumps(entry_snapshot(outcome, 3, status, 0, None)))
    assert entry == {
        "source_id": "../label",
        "ordinal": 3,
        "status": status,
        "rows": 0,
        "part": None,
        "issues": [
            {
                "severity": "error",
                "code": "bad",
                "message": "sensitive diagnostic",
                "element": "r",
                "phase": "instance",
            }
        ],
        "failure": {"kind": "io", "message": "missing"},
    }
