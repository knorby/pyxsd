"""End-to-end tests for the real-world example applications.

Each example is both documentation and a support test: a schema plus a
real-shaped instance document is parsed, a transform from the example
directory is loaded by name, and the transformed result is checked.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD

EXAMPLES = Path(__file__).parent.parent / "examples"
DOCX_SCHEMA = EXAMPLES / "docx" / "schemas" / "wml.xsd"
GPX_SCHEMA = EXAMPLES / "gpx" / "schemas" / "gpx.xsd"
MUSICXML_SCHEMA = EXAMPLES / "musicxml" / "schemas" / "musicxml.xsd"

requires_docx_schemas = pytest.mark.skipif(
    not DOCX_SCHEMA.exists(),
    reason="run examples/docx/download_schemas.py to fetch the ECMA-376 schemas",
)
requires_gpx_schemas = pytest.mark.skipif(
    not GPX_SCHEMA.exists(),
    reason="run examples/gpx/download_schemas.py to fetch the GPX schema",
)
requires_musicxml_schemas = pytest.mark.skipif(
    not MUSICXML_SCHEMA.exists(),
    reason="run examples/musicxml/download_schemas.py to fetch the MusicXML schemas",
)


def _run(example, transform, tmp_path, mode=ParseModes.STRICT, output_name="out.xml"):
    output = tmp_path / output_name
    parser = PyXSD(
        EXAMPLES / example / "instance.xml",
        xsdFile=EXAMPLES / example / "schema.xsd",
        xmlFileOutput="_No_Output_",
        transformOutputName=str(output),
        transforms=[transform],
        mode=mode,
    )
    return parser, output


def _codes(parser):
    return [issue.code for issue in parser.report.issues]


class TestMusicXMLExample:
    """The real example: a Bach chorale against the official MusicXML 4.0 XSD.

    The namespace-less MusicXML schema imports the XML and XLink namespaces,
    so this also proves cross-namespace import loading. The score is
    public-domain and taken from the music21 corpus.
    """

    @requires_musicxml_schemas
    def test_real_score_validates_and_summarizes(self, tmp_path):
        output = tmp_path / "note-stats.xml"
        parser = PyXSD(
            EXAMPLES / "musicxml" / "instance.xml",
            xsdFile=MUSICXML_SCHEMA,
            xmlFileOutput="_No_Output_",
            transformOutputName=str(output),
            transforms=["NoteStats()"],
            mode=ParseModes.NAMESPACED,
        )
        assert not parser.report.has_errors

        root = ET.parse(output).getroot()
        assert root.tag == "noteStats"
        assert root.attrib["parts"] == "4"
        assert root.attrib["measures"] == "40"
        assert root.attrib["totalNotes"] == "165"
        assert root.attrib["totalDuration"] == "288"
        assert root.attrib["rests"] == "0"
        # D2 (MIDI 42) to E5 (MIDI 76).
        assert root.attrib["lowestPitch"] == "42"
        assert root.attrib["highestPitch"] == "76"


class TestGpxExample:
    """The real example: GPX 1.1 plus Garmin TrackPointExtension.

    The instance is a real, public-domain ride (200 trackpoints). The
    extension block lives in a foreign namespace that GPX admits with
    ``xs:any namespace="##other" processContents="lax"``, so the test
    also proves wildcard pass-through end to end.
    """

    @requires_gpx_schemas
    def test_real_track_validates_and_summarizes(self, tmp_path):
        output = tmp_path / "track-stats.xml"
        parser = PyXSD(
            EXAMPLES / "gpx" / "instance.xml",
            xsdFile=GPX_SCHEMA,
            xmlFileOutput="_No_Output_",
            transformOutputName=str(output),
            transforms=["TrackStats()"],
            mode=ParseModes.NAMESPACED,
        )
        assert not parser.report.has_errors

        root = ET.parse(output).getroot()
        assert root.tag == "trackStats"
        assert root.attrib["pointCount"] == "200"
        assert 6300.0 < float(root.attrib["distanceMeters"]) < 6600.0
        assert float(root.attrib["elevationGain"]) > 0
        # Values read from the generically-bound Garmin extension block.
        assert root.attrib["maxHeartRate"] == "178.0"
        assert float(root.attrib["avgHeartRate"]) > 0
        assert float(root.attrib["avgCadence"]) > 0


class TestDocxExample:
    """The real example: full ECMA-376 WordprocessingML.

    The schema set is large and fetched on demand (see
    ``examples/docx/download_schemas.py``); the test skips when it has
    not been downloaded.  The document is a realistic mixture of
    headings, numbered and bulleted lists, a table, a hyperlink, and
    whitespace-preserving runs.
    """

    @requires_docx_schemas
    def test_real_document_markdown(self, tmp_path):
        output = tmp_path / "document.md"
        parser = PyXSD(
            EXAMPLES / "docx" / "document.xml",
            xsdFile=DOCX_SCHEMA,
            xmlFileOutput="_No_Output_",
            transformOutputName=str(output),
            transforms=[f"ToMarkdown('{output}')"],
            mode=ParseModes.NAMESPACED,
        )
        # The real schema composes cleanly; the only diagnostics are the
        # documented circular-include warnings from dml-main.xsd.
        assert not parser.report.has_errors
        assert {issue.code for issue in parser.report.issues} <= {"compose-cycle"}

        expected = (EXAMPLES / "docx" / "expected.md").read_text()
        assert output.read_text() == expected


class TestDocxLaxExample:
    """The original stand-in example, kept as a lax-binding regression."""

    def _markdown(self, mode, tmp_path):
        return _run(
            "docx/lax",
            f"ToMarkdown('{tmp_path / 'document.md'}')",
            tmp_path,
            mode=mode,
            output_name="document.md",
        )

    def test_markdown_output(self, tmp_path):
        parser, output = self._markdown(ParseModes.STRICT, tmp_path)
        # The document is intentionally messy: an unmodeled element and
        # an invalid run size are still reported in strict mode.
        assert "unexpected-element" in _codes(parser)
        assert "value" in _codes(parser)
        expected = (EXAMPLES / "docx" / "lax" / "expected.md").read_text()
        assert output.read_text() == expected

    def test_lax_mode_binds_the_mess(self, tmp_path):
        parser, output = self._markdown(ParseModes.LAX, tmp_path)
        # The report is still strict...
        assert "unexpected-element" in _codes(parser)
        assert "value" in _codes(parser)
        assert parser.report.has_errors
        # ...but the output is identical because no data was dropped.
        expected = (EXAMPLES / "docx" / "lax" / "expected.md").read_text()
        assert output.read_text() == expected

        # Lax binding keeps the unmodeled element as a generic node...
        root = parser.schemaRootInstance
        body = next(child for child in root._children_ if child._name_ == "body")
        assert "bookmarkStart" in [child._name_ for child in body._children_]

        # ...and the invalid size as the raw string.
        lastParagraph = [c for c in body._children_ if c._name_ == "p"][-1]
        run = next(c for c in lastParagraph._children_ if c._name_ == "r")
        rPr = next(c for c in run._children_ if c._name_ == "rPr")
        assert rPr.__dict__["sz"] == "not-a-number"
