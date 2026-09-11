"""End-to-end tests for the real-world example applications.

Each example is both documentation and a support test: a schema plus a
real-shaped instance document is parsed, a transform from the example
directory is loaded by name, and the transformed result is checked.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD

EXAMPLES = Path(__file__).parent.parent / "examples"


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
    def test_score_validates_cleanly(self, tmp_path):
        parser, _ = _run("musicxml", "NoteStats()", tmp_path)
        assert parser.report.issues == []

    def test_note_stats(self, tmp_path):
        _parser, output = _run("musicxml", "NoteStats()", tmp_path)
        root = ET.parse(output).getroot()
        assert root.tag == "noteStats"
        assert root.attrib["totalNotes"] == "15"
        assert root.attrib["totalDuration"] == "16"
        assert root.attrib["rests"] == "1"
        # C4 is MIDI 60, G4 is 67.
        assert root.attrib["lowestPitch"] == "60"
        assert root.attrib["highestPitch"] == "67"


class TestGpxExample:
    def test_track_validates_cleanly(self, tmp_path):
        parser, _ = _run("gpx", "TrackStats()", tmp_path)
        assert parser.report.issues == []

    def test_track_stats(self, tmp_path):
        _parser, output = _run("gpx", "TrackStats()", tmp_path)
        root = ET.parse(output).getroot()
        assert root.tag == "trackStats"
        assert root.attrib["pointCount"] == "5"
        assert root.attrib["elevationGain"] == "10.0"
        assert root.attrib["elevationLoss"] == "5.0"
        assert root.attrib["minElevation"] == "100.0"
        assert root.attrib["maxElevation"] == "108.0"
        distance = float(root.attrib["distanceMeters"])
        assert 250.0 < distance < 320.0


class TestDocxExample:
    def _markdown(self, mode, tmp_path):
        return _run(
            "docx",
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
        expected = (EXAMPLES / "docx" / "expected.md").read_text()
        assert output.read_text() == expected

    def test_lax_mode_binds_the_mess(self, tmp_path):
        parser, output = self._markdown(ParseModes.LAX, tmp_path)
        # The report is still strict...
        assert "unexpected-element" in _codes(parser)
        assert "value" in _codes(parser)
        assert parser.report.has_errors
        # ...but the output is identical because no data was dropped.
        expected = (EXAMPLES / "docx" / "expected.md").read_text()
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
