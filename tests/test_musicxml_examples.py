"""Tests for this family score workflow (MusicXML to WAV and SVG preview).

Offline tests build a minimal partwise schema in the test file, following
the ``GPX_SUBSET_SCHEMA`` precedent in ``tests/test_example_applications.py``.
The real-score tests skip until the pinned MusicXML 4.0 schema is fetched
into ``examples/musicxml/.cache/``.
"""

from __future__ import annotations

import io
import wave
import xml.etree.ElementTree as ET
from functools import cache
from pathlib import Path

import pytest
from examples.musicxml import note_stats, render_preview, synthesize
from examples.musicxml.common.schemas import compile_schema
from examples.musicxml.common.sources import require_cache

from pyxsd import Schema
from pyxsd.binding import ParseModes


def test_cli_defaults_use_the_musicxml_family_root():
    family_root = Path(synthesize.__file__).resolve().parent
    for module, output_name in (
        (synthesize, "chorale.wav"),
        (render_preview, "score-preview.svg"),
        (note_stats, "note-stats.xml"),
    ):
        assert family_root / "instance.xml" == module._DEFAULT_INPUT
        assert family_root / "out" / output_name == module._DEFAULT_OUTPUT
    assert (family_root / "instance.xml").is_file()


SCORE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="score-partwise">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="part-list" minOccurs="0">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="score-part" maxOccurs="unbounded">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="part-name" type="xs:string" minOccurs="0"/>
                  </xs:sequence>
                  <xs:attribute name="id" type="xs:string" use="required"/>
                </xs:complexType>
              </xs:element>
            </xs:sequence>
          </xs:complexType>
        </xs:element>
        <xs:element name="part" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="measure" maxOccurs="unbounded">
                <xs:complexType>
                  <xs:choice minOccurs="0" maxOccurs="unbounded">
                    <xs:element name="attributes">
                      <xs:complexType>
                        <xs:sequence>
                          <xs:element name="divisions" type="xs:integer"/>
                          <xs:element name="time" minOccurs="0">
                            <xs:complexType>
                              <xs:sequence>
                                <xs:element name="beats" type="xs:integer"/>
                                <xs:element name="beat-type" type="xs:integer"/>
                              </xs:sequence>
                            </xs:complexType>
                          </xs:element>
                          <xs:element name="clef" minOccurs="0">
                            <xs:complexType>
                              <xs:sequence>
                                <xs:element name="sign" type="xs:string"/>
                                <xs:element name="line" type="xs:integer"/>
                              </xs:sequence>
                            </xs:complexType>
                          </xs:element>
                        </xs:sequence>
                      </xs:complexType>
                    </xs:element>
                    <xs:element name="note">
                      <xs:complexType>
                        <xs:sequence>
                          <xs:element name="chord" minOccurs="0"/>
                          <xs:choice>
                            <xs:element name="pitch">
                              <xs:complexType>
                                <xs:sequence>
                                  <xs:element name="step" type="xs:string"/>
                                  <xs:element name="alter" type="xs:decimal" minOccurs="0"/>
                                  <xs:element name="octave" type="xs:integer"/>
                                </xs:sequence>
                              </xs:complexType>
                            </xs:element>
                            <xs:element name="rest"/>
                          </xs:choice>
                          <xs:element name="duration" type="xs:integer"/>
                          <xs:element name="voice" type="xs:string" minOccurs="0"/>
                          <xs:element name="type" type="xs:string" minOccurs="0"/>
                          <xs:element name="tie" minOccurs="0" maxOccurs="2">
                            <xs:complexType>
                              <xs:attribute name="type" type="xs:string" use="required"/>
                            </xs:complexType>
                          </xs:element>
                        </xs:sequence>
                      </xs:complexType>
                    </xs:element>
                    <xs:element name="forward">
                      <xs:complexType>
                        <xs:sequence>
                          <xs:element name="duration" type="xs:integer"/>
                        </xs:sequence>
                      </xs:complexType>
                    </xs:element>
                    <xs:element name="backup">
                      <xs:complexType>
                        <xs:sequence>
                          <xs:element name="duration" type="xs:integer"/>
                        </xs:sequence>
                      </xs:complexType>
                    </xs:element>
                    <xs:element name="sound">
                      <xs:complexType>
                        <xs:attribute name="tempo" type="xs:decimal"/>
                      </xs:complexType>
                    </xs:element>
                  </xs:choice>
                  <xs:attribute name="number" type="xs:string"/>
                </xs:complexType>
              </xs:element>
            </xs:sequence>
            <xs:attribute name="id" type="xs:string"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def _measure(attributes: str, body: str, number: str = "1") -> str:
    return f'<measure number="{number}">{attributes}{body}</measure>'


_DIVISIONS_TWO = (
    "<attributes><divisions>2</divisions><clef><sign>G</sign><line>2</line></clef></attributes>"
)


def _score(*parts: str) -> str:
    return f"<score-partwise>{''.join(parts)}</score-partwise>"


def _part(body: str, part_id: str = "P1") -> str:
    return f'<part id="{part_id}">{body}</part>'


TIED_SCORE = _score(
    _part(
        _measure(
            _DIVISIONS_TWO,
            "<sound tempo='96'/>"
            "<note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration>"
            "<voice>1</voice><type>quarter</type><tie type='start'/></note>"
            "<note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration>"
            "<voice>1</voice><type>quarter</type><tie type='stop'/></note>"
            "<forward><duration>4</duration></forward>",
        )
    )
)

TWO_NOTE_SCORE = _score(
    _part(
        _measure(
            _DIVISIONS_TWO,
            "<note><pitch><step>A</step><octave>4</octave></pitch><duration>2</duration>"
            "<voice>1</voice></note>"
            "<note><pitch><step>B</step><octave>4</octave></pitch><duration>2</duration>"
            "<voice>1</voice></note>",
        )
    )
)

FORWARD_SCORE = _score(
    _part(
        _measure(
            _DIVISIONS_TWO,
            "<note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration></note>"
            "<forward><duration>4</duration></forward>"
            "<note><pitch><step>D</step><octave>4</octave></pitch><duration>2</duration></note>",
        )
    )
)

REST_SCORE = _score(
    _part(
        _measure(
            _DIVISIONS_TWO,
            "<note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration></note>"
            "<note><rest/><duration>2</duration></note>"
            "<note><pitch><step>D</step><octave>4</octave></pitch><duration>2</duration></note>",
        )
    )
)

BACKUP_SCORE = _score(
    _part(
        _measure(
            _DIVISIONS_TWO,
            "<note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration></note>"
            "<backup><duration>2</duration></backup>"
            "<note><pitch><step>D</step><octave>4</octave></pitch><duration>2</duration></note>",
        )
    )
)

CHORD_SCORE = _score(
    _part(
        _measure(
            _DIVISIONS_TWO,
            "<note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration></note>"
            "<note><chord/><pitch><step>E</step><octave>4</octave></pitch>"
            "<duration>2</duration></note>"
            "<note><pitch><step>D</step><octave>4</octave></pitch><duration>2</duration></note>",
        )
    )
)

DIVISIONS_SCORE = _score(
    _part(
        _measure(
            _DIVISIONS_TWO,
            "<note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration></note>",
        )
        + _measure(
            "<attributes><divisions>4</divisions></attributes>",
            "<note><pitch><step>C</step><octave>4</octave></pitch><duration>8</duration></note>",
            number="2",
        )
    )
)

PITCH_SCORE = _score(
    _part(
        _measure(
            _DIVISIONS_TWO,
            "<note><pitch><step>A</step><octave>4</octave></pitch><duration>2</duration></note>"
            "<note><pitch><step>C</step><alter>1</alter><octave>4</octave></pitch>"
            "<duration>2</duration></note>"
            "<note><pitch><step>B</step><alter>-1</alter><octave>3</octave></pitch>"
            "<duration>2</duration></note>",
        )
    )
)

FOUR_PART_SCORE = _score(
    "<part-list>"
    '<score-part id="P1"><part-name>Soprano</part-name></score-part>'
    '<score-part id="P2"><part-name>Alto</part-name></score-part>'
    '<score-part id="P3"><part-name>Tenor</part-name></score-part>'
    '<score-part id="P4"><part-name>Bass</part-name></score-part>'
    "</part-list>",
    _part(
        _measure(
            _DIVISIONS_TWO,
            "<note><pitch><step>C</step><octave>5</octave></pitch><duration>2</duration></note>",
        ),
        "P1",
    ),
    _part(
        _measure(
            "<attributes><divisions>2</divisions>"
            "<clef><sign>G</sign><line>2</line></clef></attributes>",
            "<note><pitch><step>A</step><octave>4</octave></pitch><duration>2</duration></note>",
        ),
        "P2",
    ),
    _part(
        _measure(
            "<attributes><divisions>2</divisions>"
            "<clef><sign>F</sign><line>4</line></clef></attributes>",
            "<note><pitch><step>E</step><octave>3</octave></pitch><duration>2</duration></note>",
        ),
        "P3",
    ),
    _part(
        _measure(
            "<attributes><divisions>2</divisions>"
            "<clef><sign>F</sign><line>4</line></clef></attributes>",
            "<note><pitch><step>F</step><octave>2</octave></pitch><duration>2</duration></note>",
        ),
        "P4",
    ),
)

EMPTY_SCORE = _score(_part("<measure/>"))


@cache
def _schema() -> Schema:
    return Schema.compile(
        io.StringIO(SCORE_SCHEMA),
        mode=ParseModes.LAX.replace(namespaces="strict"),
    )


def _parse(xml: str):
    document = _schema().parse(io.StringIO(xml))
    assert not document.report.has_errors, [issue.message for issue in document.report.issues]
    return document


class TestNotes:
    def test_tie_pair_merges_into_one_span(self):
        found = synthesize.notes(_parse(TIED_SCORE))
        assert found == [{"part": "P1", "midi": 60, "start_q": 0.0, "dur_q": 2.0}]

    def test_forward_advances_time_without_a_note(self):
        found = synthesize.notes(_parse(FORWARD_SCORE))
        assert len(found) == 2
        assert [note["start_q"] for note in found] == [0.0, 3.0]

    def test_rest_advances_time_without_a_note(self):
        found = synthesize.notes(_parse(REST_SCORE))
        assert len(found) == 2
        assert [note["start_q"] for note in found] == [0.0, 2.0]

    def test_backup_rewinds_the_cursor(self):
        found = synthesize.notes(_parse(BACKUP_SCORE))
        assert [note["start_q"] for note in found] == [0.0, 0.0]

    def test_chord_shares_the_previous_onset(self):
        found = synthesize.notes(_parse(CHORD_SCORE))
        assert [note["start_q"] for note in found] == [0.0, 0.0, 1.0]
        assert [note["midi"] for note in found] == [60, 64, 62]

    def test_divisions_are_reread_from_later_attributes(self):
        found = synthesize.notes(_parse(DIVISIONS_SCORE))
        assert [(note["start_q"], note["dur_q"]) for note in found] == [
            (0.0, 1.0),
            (1.0, 2.0),
        ]

    def test_a4_is_midi_69_and_alter_applies(self):
        found = synthesize.notes(_parse(PITCH_SCORE))
        # A4 = 69; C♯4 = 61; B♭3 = 58.
        assert [note["midi"] for note in found] == [69, 61, 58]

    def test_empty_measure_has_no_notes(self):
        assert synthesize.notes(_parse(EMPTY_SCORE)) == []


class TestSchedule:
    def test_quarter_is_625_ms_at_96_qpm(self):
        spans = synthesize.schedule([{"part": "P1", "midi": 69, "start_q": 0.0, "dur_q": 1.0}])
        assert spans == [(0.0, 0.625, 440.0)]

    def test_tempo_scales_duration_and_start(self):
        spans = synthesize.schedule(
            [{"part": "P1", "midi": 69, "start_q": 2.0, "dur_q": 1.0}],
            tempo_qpm=120.0,
        )
        assert spans == [(1.0, 0.5, 440.0)]


class TestSynthesize:
    def test_writes_exact_wav_length_and_is_deterministic(self, tmp_path):
        document = _parse(TWO_NOTE_SCORE)
        first = synthesize.synthesize(document, tmp_path / "first.wav", sample_rate=8000)
        second = synthesize.synthesize(document, tmp_path / "second.wav", sample_rate=8000)

        # Two quarter notes at 96 qpm are 1.25 s at 8000 Hz.
        frames = round(2 * (60 / 96) * 8000)
        assert frames == 10_000
        assert len(first.read_bytes()) == 44 + frames * 2
        assert first.read_bytes() == second.read_bytes()

        with wave.open(str(first), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == 8000
            assert handle.getnframes() == frames

    def test_empty_score_writes_a_header_only_wav(self, tmp_path):
        dest = synthesize.synthesize(_parse(EMPTY_SCORE), tmp_path / "empty.wav", sample_rate=8000)
        assert len(dest.read_bytes()) == 44
        with wave.open(str(dest), "rb") as handle:
            assert handle.getnframes() == 0

    def test_rejects_non_positive_parameters(self, tmp_path):
        document = _parse(TWO_NOTE_SCORE)
        with pytest.raises(ValueError, match="tempo_qpm"):
            synthesize.synthesize(document, tmp_path / "x.wav", tempo_qpm=0)
        with pytest.raises(ValueError, match="sample_rate"):
            synthesize.synthesize(document, tmp_path / "x.wav", sample_rate=0)


class TestPreview:
    def test_draws_one_staff_per_part_and_one_notehead_per_note(self):
        svg = render_preview.render_preview(_parse(FOUR_PART_SCORE))
        assert svg.startswith("<svg")
        assert svg.count('class="staff"') == 4
        assert svg.count('class="staff-line"') == 20
        assert svg.count("<ellipse") == 4
        assert "Soprano" in svg
        assert "Bass" in svg

    def test_marks_accidentals(self):
        svg = render_preview.render_preview(_parse(PITCH_SCORE))
        assert svg.count("<ellipse") == 3
        assert "♯" in svg
        assert "♭" in svg

    def test_draws_ties_as_arcs(self):
        svg = render_preview.render_preview(_parse(TIED_SCORE))
        assert svg.count("<ellipse") == 2
        assert svg.count('class="tie"') == 1
        assert "<path" in svg

    def test_is_deterministic_and_honours_width(self):
        document = _parse(FOUR_PART_SCORE)
        first = render_preview.render_preview(document, width=640)
        assert first == render_preview.render_preview(document, width=640)
        assert 'width="640"' in first
        assert render_preview.render_preview(document) != first

    def test_rejects_non_positive_width(self):
        with pytest.raises(ValueError, match="width"):
            render_preview.render_preview(_parse(TWO_NOTE_SCORE), width=0)


INSTANCE = Path(__file__).resolve().parents[1] / "examples" / "musicxml" / "instance.xml"


def _musicxml_schema_ready() -> bool:
    try:
        require_cache("musicxml-schema")
    except FileNotFoundError:
        return False
    return True


requires_musicxml_schema = pytest.mark.skipif(
    not _musicxml_schema_ready(),
    reason=(
        "MusicXML 4.0 schema not fetched; run: "
        "uv run python examples/musicxml/download_sources.py --only musicxml-schema"
    ),
)


@pytest.fixture(scope="module")
def chorale():
    schema = compile_schema()
    return schema.parse(INSTANCE)


@requires_musicxml_schema
def test_real_score_validates_with_no_errors(chorale):
    assert chorale.report.error_count == 0
    assert INSTANCE.is_file()


@requires_musicxml_schema
def test_real_score_has_pinned_merged_note_count(chorale):
    found = synthesize.notes(chorale)
    # 165 notated notes in the chorale; the P1 and P3 tie pairs each merge
    # two notated notes into one sounding span (165 - 2 = 163).
    assert len(found) == 163
    assert {note["part"] for note in found} == {"P1", "P2", "P3", "P4"}


@requires_musicxml_schema
def test_real_score_synthesizes_22_5_seconds(chorale, tmp_path):
    dest = synthesize.synthesize(chorale, tmp_path / "chorale.wav")
    with wave.open(str(dest), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    # 36 sounding quarters at 96 qpm: 22.5 s exactly at the default rate.
    assert frames == 496_125
    assert abs(frames / rate - 22.5) <= 0.1


@requires_musicxml_schema
def test_real_score_preview_draws_four_staves(chorale):
    svg = render_preview.render_preview(chorale)
    assert svg.startswith("<svg")
    assert svg.count('class="staff"') == 4
    assert svg.count('class="staff-line"') == 20
    assert svg.count("<ellipse") == 165
    assert svg.count('class="tie"') == 2


@requires_musicxml_schema
def test_note_stats_cli_writes_the_summary(tmp_path):
    output = tmp_path / "note-stats.xml"
    assert note_stats.main(["--output", str(output)]) == 0
    root = ET.parse(output).getroot()
    assert root.tag == "noteStats"
    assert root.attrib == {
        "totalNotes": "165",
        "totalDuration": "288",
        "rests": "0",
        "parts": "4",
        "measures": "40",
        "lowestPitch": "42",
        "highestPitch": "76",
    }


def test_single_schema_family_rejects_variants():
    from examples.musicxml.common import schemas

    assert not hasattr(schemas, "ENTRIES")
    with pytest.raises(KeyError, match="single schema entry"):
        compile_schema("1.7")
