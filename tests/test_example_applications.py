"""End-to-end tests for the real-world example applications.

Each example is both documentation and a support test: a schema plus a
real-shaped instance document is parsed, a transform from the example
directory is loaded by name, and the transformed result is checked.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pyxsd.binding import ParseModes
from pyxsd.cli import parse_transform_call, resolve_transform_class
from pyxsd.document import Document, write_tree
from pyxsd.schema import Schema

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

# A minimal GPX 1.1 subset carrying only what TrackStats reads. The
# real-schema tests above skip when the downloaded schema is missing;
# these keep the segment behavior covered in a clean checkout.
GPX_SUBSET_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="gpx">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="trk" minOccurs="0" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="trkseg" minOccurs="0" maxOccurs="unbounded">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="trkpt" minOccurs="0" maxOccurs="unbounded">
                      <xs:complexType>
                        <xs:sequence>
                          <xs:element name="ele" type="xs:double" minOccurs="0"/>
                        </xs:sequence>
                        <xs:attribute name="lat" type="xs:double" use="required"/>
                        <xs:attribute name="lon" type="xs:double" use="required"/>
                      </xs:complexType>
                    </xs:element>
                  </xs:sequence>
                </xs:complexType>
              </xs:element>
            </xs:sequence>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def _run(
    example, instance, xsd, transform, tmp_path, mode=ParseModes.STRICT, output_name="out.xml"
):
    """Parses an instance against a schema and runs one transform.

    Mirrors the CLI composition: the transform class is resolved from
    the example directory, run through ``Document.transform``, and a
    pass-through result (a synthetic tree, not a schema-bound
    ``Document``) is written by the caller — the CLI no longer writes
    those. A transform that writes its own output (ToMarkdown)
    returns ``None`` and needs no write here.
    """
    output = tmp_path / output_name
    document = Schema.compile(xsd, mode=mode).parse(instance)
    name, args, kwargs = parse_transform_call(transform)
    transformCls = resolve_transform_class(name, search_paths=[EXAMPLES / example])
    result = document.transform(lambda root: transformCls(root)(*args, **kwargs))
    if not isinstance(result, Document) and result is not None:
        write_tree(result, output)
    return document, output


def _codes(document):
    return [issue.code for issue in document.report.issues]


class TestMusicXMLExample:
    """The real example: a Bach chorale against the official MusicXML 4.0 XSD.

    The namespace-less MusicXML schema imports the XML and XLink namespaces,
    so this also proves cross-namespace import loading. The score is
    public-domain and taken from the music21 corpus.
    """

    @requires_musicxml_schemas
    def test_real_score_validates_and_summarizes(self, tmp_path):
        document, output = _run(
            "musicxml",
            EXAMPLES / "musicxml" / "instance.xml",
            MUSICXML_SCHEMA,
            "NoteStats()",
            tmp_path,
            mode=ParseModes.NAMESPACED,
            output_name="note-stats.xml",
        )
        assert not document.report.has_errors

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
        document, output = _run(
            "gpx",
            EXAMPLES / "gpx" / "instance.xml",
            GPX_SCHEMA,
            "TrackStats()",
            tmp_path,
            mode=ParseModes.NAMESPACED,
            output_name="track-stats.xml",
        )
        assert not document.report.has_errors

        root = ET.parse(output).getroot()
        assert root.tag == "trackStats"
        assert root.attrib["pointCount"] == "200"
        assert 6300.0 < float(root.attrib["distanceMeters"]) < 6600.0
        assert float(root.attrib["elevationGain"]) > 0
        # Values read from the generically-bound Garmin extension block.
        assert root.attrib["maxHeartRate"] == "178.0"
        assert float(root.attrib["avgHeartRate"]) > 0
        assert float(root.attrib["avgCadence"]) > 0

    @requires_gpx_schemas
    def test_segments_are_not_bridged(self, tmp_path):
        """Distances are computed within each segment, never across them.

        Two segments that share no recorded movement between them must
        not have a leg invented between the end of one and the start of
        the other.
        """
        instance = tmp_path / "multi.xml"
        instance.write_text(
            "<?xml version='1.0' encoding='utf-8'?>\n"
            '<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">\n'
            "  <trk>\n"
            "    <trkseg>\n"
            '      <trkpt lat="0.0" lon="0.0"><ele>0.0</ele></trkpt>\n'
            '      <trkpt lat="0.0" lon="1.0"><ele>100.0</ele></trkpt>\n'
            "    </trkseg>\n"
            "    <trkseg>\n"
            '      <trkpt lat="0.0" lon="2.0"><ele>50.0</ele></trkpt>\n'
            "    </trkseg>\n"
            "  </trk>\n"
            "</gpx>\n"
        )
        document, output = _run(
            "gpx",
            instance,
            GPX_SCHEMA,
            "TrackStats()",
            tmp_path,
            mode=ParseModes.NAMESPACED,
            output_name="multi-stats.xml",
        )
        assert not document.report.has_errors

        root = ET.parse(output).getroot()
        assert root.tag == "trackStats"
        assert root.attrib["pointCount"] == "3"
        # Only the single leg inside the first segment (1 degree of
        # longitude at the equator); the gap to segment two is not a
        # recorded movement and must not be bridged.
        distance = float(root.attrib["distanceMeters"])
        assert 111100.0 < distance < 111300.0
        assert root.attrib["elevationGain"] == "100.0"
        assert root.attrib["elevationLoss"] == "0.0"


class TestGpxSegmentsSelfContained:
    """Segment-local statistics without the downloaded GPX schema.

    The real-schema tests above skip in a clean checkout; these run the
    same ``TrackStats`` transform against an embedded GPX subset so the
    segment semantics stay covered everywhere.
    """

    def _stats(self, tmp_path, instance_text):
        schema = tmp_path / "gpx-subset.xsd"
        schema.write_text(GPX_SUBSET_SCHEMA)
        instance = tmp_path / "instance.xml"
        instance.write_text(instance_text)
        document, output = _run(
            "gpx",
            instance,
            schema,
            "TrackStats()",
            tmp_path,
            output_name="stats.xml",
        )
        assert not document.report.has_errors
        return ET.parse(output).getroot()

    def test_segments_are_not_bridged(self, tmp_path):
        root = self._stats(
            tmp_path,
            "<gpx><trk><trkseg>"
            '<trkpt lat="0.0" lon="0.0"><ele>0.0</ele></trkpt>'
            '<trkpt lat="0.0" lon="1.0"><ele>100.0</ele></trkpt>'
            "</trkseg><trkseg>"
            '<trkpt lat="0.0" lon="2.0"><ele>50.0</ele></trkpt>'
            "</trkseg></trk></gpx>",
        )
        assert root.attrib["pointCount"] == "3"
        # Only the single leg inside the first segment; the gap to the
        # second segment is not a recorded movement.
        distance = float(root.attrib["distanceMeters"])
        assert 111100.0 < distance < 111300.0
        assert root.attrib["elevationGain"] == "100.0"
        assert root.attrib["elevationLoss"] == "0.0"

    def test_single_point_tracks_do_not_invent_movement(self, tmp_path):
        root = self._stats(
            tmp_path,
            "<gpx><trk><trkseg>"
            '<trkpt lat="0.0" lon="0.0"><ele>0.0</ele></trkpt>'
            "</trkseg></trk><trk><trkseg>"
            '<trkpt lat="0.0" lon="1.0"><ele>100.0</ele></trkpt>'
            "</trkseg></trk></gpx>",
        )
        assert root.attrib["pointCount"] == "2"
        assert root.attrib["distanceMeters"] == "0.0"
        assert root.attrib["elevationGain"] == "0.0"
        assert root.attrib["elevationLoss"] == "0.0"


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
        document, _ = _run(
            "docx",
            EXAMPLES / "docx" / "document.xml",
            DOCX_SCHEMA,
            f"ToMarkdown('{output}')",
            tmp_path,
            mode=ParseModes.NAMESPACED,
            output_name="unused.xml",
        )
        # The real schema composes cleanly; the only diagnostics are the
        # documented circular-include warnings from dml-main.xsd.
        assert not document.report.has_errors
        assert {issue.code for issue in document.report.issues} <= {"compose-cycle"}

        expected = (EXAMPLES / "docx" / "expected.md").read_text()
        assert output.read_text() == expected


class TestDocxLaxExample:
    """The original stand-in example, kept as a lax-binding regression."""

    def _markdown(self, mode, tmp_path):
        return _run(
            "docx/lax",
            EXAMPLES / "docx" / "lax" / "instance.xml",
            EXAMPLES / "docx" / "lax" / "schema.xsd",
            f"ToMarkdown('{tmp_path / 'document.md'}')",
            tmp_path,
            mode=mode,
            output_name="document.md",
        )

    def test_markdown_output(self, tmp_path):
        document, output = self._markdown(ParseModes.STRICT, tmp_path)
        # The document is intentionally messy: an unmodeled element and
        # an invalid run size are still reported in strict mode.
        assert "unexpected-element" in _codes(document)
        assert "value" in _codes(document)
        expected = (EXAMPLES / "docx" / "lax" / "expected.md").read_text()
        assert output.read_text() == expected

    def test_lax_mode_binds_the_mess(self, tmp_path):
        document, output = self._markdown(ParseModes.LAX, tmp_path)
        # The report is still strict...
        assert "unexpected-element" in _codes(document)
        assert "value" in _codes(document)
        assert document.report.has_errors
        # ...but the output is identical because no data was dropped.
        expected = (EXAMPLES / "docx" / "lax" / "expected.md").read_text()
        assert output.read_text() == expected

        # Lax binding keeps the unmodeled element as a generic node...
        root = document.root
        body = next(child for child in root._children_ if child._name_ == "body")
        assert "bookmarkStart" in [child._name_ for child in body._children_]

        # ...and the invalid size as the raw string.
        lastParagraph = [c for c in body._children_ if c._name_ == "p"][-1]
        run = next(c for c in lastParagraph._children_ if c._name_ == "r")
        rPr = next(c for c in run._children_ if c._name_ == "rPr")
        assert rPr.__dict__["sz"] == "not-a-number"
