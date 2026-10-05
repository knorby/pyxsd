"""Run the batch example's CLI contract against real files."""

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from pyxsd import PyXSDError, Schema

SCRIPT = Path(__file__).resolve().parents[1] / "examples/batch/validate_documents.py"
XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="count" type="xs:int"/></xs:schema>"""


def setup_files(tmp_path):
    schema = tmp_path / "schema.xsd"
    schema.write_text(XSD)
    root = tmp_path / "inputs"
    root.mkdir()
    (root / "b.xml").write_text("<count>2</count>")
    (root / "a.xml").write_text("<count>1</count>")
    return schema, root


def run_example(schema, root, *paths):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(schema), str(root), *paths],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_sorts_relative_ids_and_success_exit(tmp_path):
    schema, root = setup_files(tmp_path)
    result = run_example(schema, root, "b.xml", "a.xml")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["a.xml: valid", "b.xml: valid"]
    assert str(root) not in result.stdout


def test_cli_reports_all_expected_failures_exit_one(tmp_path):
    schema, root = setup_files(tmp_path)
    (root / "bad.xml").write_text("<count/>")
    (root / "malformed.xml").write_text("<count>")
    result = run_example(schema, root, "bad.xml", "malformed.xml", "missing.xml", "b.xml")
    assert result.returncode == 1
    assert "bad.xml: invalid" in result.stdout
    assert "b.xml: valid" in result.stdout
    assert "malformed.xml: input_error (malformed_xml)" in result.stdout
    assert "missing.xml: input_error (io)" in result.stdout
    assert "Traceback" not in result.stderr


def test_cli_configuration_failures_exit_two(tmp_path):
    schema, root = setup_files(tmp_path)
    result = run_example(schema, root, "a.xml", "a.xml")
    assert result.returncode == 2
    assert "duplicate" in result.stderr
    outside = run_example(schema, root, "../outside.xml")
    assert outside.returncode == 2
    assert "input root" in outside.stderr
    missing_schema = run_example(root / "missing.xsd", root, "a.xml")
    assert missing_schema.returncode == 2


@pytest.mark.parametrize("error", [RuntimeError, TypeError, ValueError, PyXSDError])
def test_cli_internal_defects_exit_two_with_traceback(tmp_path, monkeypatch, capsys, error):
    schema, root = setup_files(tmp_path)
    main = runpy.run_path(str(SCRIPT))["main"]

    def defect(*args):
        raise error("unexpected binder defect")

    monkeypatch.setattr(Schema, "parse", defect)
    assert main([str(schema), str(root), "a.xml"]) == 2
    assert "Traceback" in capsys.readouterr().err
