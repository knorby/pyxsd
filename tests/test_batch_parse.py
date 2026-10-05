"""Sequential, explicit-path parsing contracts."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from pyxsd.batch import BatchParseError, DocumentSource, InputFailure, ParseOutcome
from pyxsd.exceptions import PyXSDError


@pytest.mark.parametrize("identifier", ["", None, 1])
def test_source_requires_nonempty_string_id(identifier, tmp_path):
    with pytest.raises((TypeError, ValueError), match="id"):
        DocumentSource(identifier, tmp_path / "missing.xml")


@pytest.mark.parametrize("path", [None, 1, b"input.xml", object()])
def test_source_rejects_non_text_paths(path):
    with pytest.raises(TypeError, match="path"):
        DocumentSource("input", path)


def test_source_anchors_path_at_construction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = DocumentSource("input", "missing.xml")
    monkeypatch.chdir(tmp_path.parent)
    assert source.path == tmp_path / "missing.xml"
    assert isinstance(source.path, Path)
    with pytest.raises(FrozenInstanceError):
        source.id = "changed"


def test_outcome_snapshots_issues_and_failure_has_no_exception(tmp_path):
    issues = []
    failure = InputFailure("io", "missing file")
    outcome = ParseOutcome(
        DocumentSource("input", tmp_path / "missing"), "input_error", None, issues, failure
    )
    issues.append("later")
    assert outcome.issues == ()
    assert vars(failure) == {"kind": "io", "message": "missing file"}
    error = BatchParseError(outcome)
    assert isinstance(error, PyXSDError)
    assert error.outcome is outcome
    assert "input" in str(error)
