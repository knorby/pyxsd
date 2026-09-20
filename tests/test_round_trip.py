"""Integration tests: parse an instance, write it out, compare."""

import pytest

from conftest import (
    ALL_FIXTURES,
    assert_xml_canonically_equal,
    fixture_dir,
    run_parser,
)
from pyxsd.cli import main


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_parsed_output_round_trips(fixture, tmp_path):
    """The pre-transform output must match the input document.

    Whitespace and attribute-order differences are ignored by the
    canonical comparison; structure, values, attributes, and
    multi-line text content must survive the parse/write cycle.
    """
    output = tmp_path / "parsed.xml"
    doc = run_parser(fixture, xmlFileOutput=str(output))
    assert output.exists()
    assert_xml_canonically_equal(fixture_dir(fixture) / "instance.xml", output)
    # The schema classes are still reachable for inspection.
    assert "schema" in doc.schema.classes


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_transformed_output_round_trips(fixture, tmp_path):
    """PrintData regurgitates the tree; the output must match the input."""
    output = tmp_path / "transformed.xml"
    main(
        [
            "-i",
            str(fixture_dir(fixture) / "instance.xml"),
            "-s",
            str(fixture_dir(fixture) / "schema.xsd"),
            "-t",
            "PrintData()",
            "-o",
            str(output),
        ]
    )
    assert output.exists()
    assert_xml_canonically_equal(fixture_dir(fixture) / "instance.xml", output)


def test_print_data_stdout_round_trip(capsys):
    """The default transform output goes to stdout and round-trips."""
    main(["-i", str(fixture_dir("inventory") / "instance.xml"), "-t", "PrintData()"])
    out = capsys.readouterr().out
    assert "<inventory" in out
    assert "wrench" in out


def test_multi_line_text_is_preserved(tmp_path):
    """Multi-line element values keep every line through the pipeline."""
    output = tmp_path / "parsed.xml"
    run_parser("primitives", xmlFileOutput=str(output))
    text = output.read_text()
    assert "first line of notes" in text
    assert "second line of notes" in text
    assert "third line of notes" in text


def test_missing_required_attribute_is_reported(tmp_path):
    """A required attribute absent from the instance is flagged on the report."""
    directory = fixture_dir("nested")
    instance = directory / "instance.xml"
    stripped = tmp_path / "instance.xml"
    content = instance.read_text().replace(' id="p-1"', "")
    stripped.write_text(content)
    (tmp_path / "schema.xsd").write_text((directory / "schema.xsd").read_text())

    from pyxsd.schema import Schema

    doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(stripped))
    codes = [issue.code for issue in doc.report]
    assert "missing-attribute" in codes
    assert doc.report.has_errors
    matching = [i for i in doc.report if i.code == "missing-attribute"]
    assert any("required but was not found" in i.message for i in matching)


def test_wrong_element_order_is_reported(tmp_path):
    """Sequence order violations are flagged on the report."""
    directory = fixture_dir("inventory")
    instance = directory / "instance.xml"
    swapped = tmp_path / "instance.xml"
    content = instance.read_text()
    # Swap name and quantity in the first item.
    content = content.replace(
        "<name>wrench</name>\n    <quantity>12</quantity>",
        "<quantity>12</quantity>\n    <name>wrench</name>",
    )
    swapped.write_text(content)
    (tmp_path / "schema.xsd").write_text((directory / "schema.xsd").read_text())

    from pyxsd.schema import Schema

    doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(swapped))
    codes = [issue.code for issue in doc.report]
    assert "order" in codes
    assert doc.report.has_errors
    matching = [i for i in doc.report if i.code == "order"]
    assert any("order error" in i.message for i in matching)
