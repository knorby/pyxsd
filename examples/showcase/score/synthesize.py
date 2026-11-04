"""Read a validated MusicXML partwise score into note spans and time them.

1. pyxsd validates the score against the official MusicXML 4.0 schema
   (see :mod:`examples.showcase.common.schemas`).
2. :func:`notes` reads the bound tree into absolute-pitch, absolute-time
   note spans: ties merged, ``<forward>`` advancing time as silence.
3. :func:`schedule` converts quarter-note positions to seconds and MIDI
   numbers to equal-tempered frequencies.

The bundled example score is Bach's *Christ unser Herr zum Jordan kam*,
BWV 66.6, a public-domain work from the BSD-licensed music21 corpus.
"""

from __future__ import annotations

#: Semitone offset of each natural pitch class within an octave (C4 = 60).
STEP_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
#: Diatonic index of each natural pitch class, used for staff placement.
STEP_INDEX = {"C": 0, "D": 1, "E": 2, "F": 3, "G": 4, "A": 5, "B": 6}


def midi_to_freq(midi: int) -> float:
    """Return the equal-tempered frequency of a MIDI note (A4 = 69 = 440 Hz)."""
    return 440.0 * 2 ** ((midi - 69) / 12)


def _local_name(node: object) -> str:
    """Element name without any Clark-notation namespace prefix."""
    return str(getattr(node, "_name_", "")).rsplit("}", 1)[-1]


def _children(node: object, name: str | None = None) -> list:
    """Child elements of *node*, in document order, optionally filtered by name."""
    children = list(getattr(node, "_children_", ()) or ())
    if name is None:
        return children
    return [child for child in children if _local_name(child) == name]


def _first(node: object, name: str):
    """First child element called *name*, or ``None``."""
    matches = _children(node, name)
    return matches[0] if matches else None


def _text(node: object) -> str | None:
    """Stripped character data of an element node, or ``None`` when empty."""
    value = getattr(node, "_value_", None)
    if value is None:
        return None
    text = "".join(str(part) for part in value).strip()
    return text or None


def _int_text(node: object) -> int | None:
    """Integer character data of an element node, or ``None`` when absent."""
    text = _text(node)
    return int(text) if text is not None else None


def _attribute(node: object, name: str) -> str | None:
    """Lexical attribute value, or ``None`` when the attribute is absent."""
    attributes = getattr(node, "_attribs_", None) or {}
    value = attributes.get(name)
    return str(value) if value is not None else None


def _pitch_details(pitch_node: object) -> tuple[str, float, int]:
    """Read ``(step, alter, octave)`` from a ``<pitch>`` element."""
    step = (_text(_first(pitch_node, "step")) or "C").upper()
    if step not in STEP_SEMITONES:
        raise ValueError(f"unknown pitch step {step!r}")
    octave = _int_text(_first(pitch_node, "octave"))
    if octave is None:
        raise ValueError("pitch has no octave")
    alter_node = _first(pitch_node, "alter")
    alter = float(_text(alter_node)) if alter_node is not None else 0.0
    return step, alter, octave


def _midi_number(step: str, alter: float, octave: int) -> int:
    """MIDI note number for a spelled pitch (C4 = 60)."""
    return round(12 * (octave + 1) + STEP_SEMITONES[step] + alter)


def _part_names(root: object) -> dict[str, str]:
    """Map ``score-part`` ids to their ``part-name`` text."""
    names: dict[str, str] = {}
    part_list = _first(root, "part-list")
    if part_list is None:
        return names
    for score_part in _children(part_list, "score-part"):
        part_id = _attribute(score_part, "id")
        name = _text(_first(score_part, "part-name"))
        if part_id and name:
            names[part_id] = name
    return names


def _walk_part(part_node: object) -> dict:
    """Read one ``<part>`` into pitch/time events and measure spans.

    Notes stay unmerged here; :func:`notes` merges ties and
    :mod:`examples.showcase.score.render_preview` needs the notated
    notes so it can draw a notehead at each end of a tie.
    """
    part_id = _attribute(part_node, "id") or ""
    divisions = 1
    cursor = 0.0
    last_onset: float | None = None
    clef = ("G", 2)
    events: list[dict] = []
    measures: list[dict] = []

    for measure in _children(part_node, "measure"):
        start = cursor
        number = _attribute(measure, "number") or str(len(measures) + 1)
        for element in _children(measure):
            name = _local_name(element)
            if name == "attributes":
                divisions_node = _first(element, "divisions")
                if divisions_node is not None:
                    value = _int_text(divisions_node)
                    if value is not None and value > 0:
                        divisions = value
                clef_node = _first(element, "clef")
                if clef_node is not None:
                    sign = (_text(_first(clef_node, "sign")) or "G").upper()
                    line = _int_text(_first(clef_node, "line")) or 2
                    clef = (sign, line)
            elif name == "note":
                duration = _int_text(_first(element, "duration"))
                if duration is None:
                    continue
                dur_q = duration / divisions
                chord = _first(element, "chord") is not None
                if chord and last_onset is not None:
                    onset = last_onset
                else:
                    onset = cursor
                    cursor += dur_q
                    last_onset = onset
                pitch = _first(element, "pitch")
                if pitch is None or _first(element, "rest") is not None:
                    continue
                step, alter, octave = _pitch_details(pitch)
                ties = frozenset(
                    tie_type
                    for tie_type in (_attribute(tie, "type") for tie in _children(element, "tie"))
                    if tie_type is not None
                )
                events.append(
                    {
                        "midi": _midi_number(step, alter, octave),
                        "step": step,
                        "alter": alter,
                        "octave": octave,
                        "onset_q": onset,
                        "dur_q": dur_q,
                        "voice": _text(_first(element, "voice")) or "1",
                        "ties": ties,
                    }
                )
            elif name == "forward":
                duration = _int_text(_first(element, "duration"))
                if duration is not None:
                    cursor += duration / divisions
            elif name == "backup":
                duration = _int_text(_first(element, "duration"))
                if duration is not None:
                    cursor -= duration / divisions
        measures.append({"number": number, "start_q": start, "end_q": cursor})

    return {"part": part_id, "clef": clef, "notes": events, "measures": measures}


def _score_parts(document: object) -> list[dict]:
    """Walk every ``<part>`` of a parsed document, in document order."""
    root = document.root  # type: ignore[attr-defined]
    names = _part_names(root)
    parts: list[dict] = []
    for part_node in _children(root, "part"):
        part = _walk_part(part_node)
        part["name"] = names.get(part["part"], part["part"])
        parts.append(part)
    return parts


def notes(document: object) -> list[dict]:
    """Return sounding note spans from a validated MusicXML partwise score.

    Each entry is ``{part, midi, start_q, dur_q}`` with times in quarter
    notes. Ties (``<tie type="start">`` followed by ``<tie type="stop">``
    on the same voice and pitch) merge into one span, so a tied pair of
    quarters becomes a single two-quarter note with one attack. A
    ``<forward>`` advances the cursor without emitting a note; a
    ``<backup>`` rewinds it. ``divisions`` is re-read from every
    ``<attributes>`` element and ``<type>`` is ignored (``<duration>``
    is authoritative).
    """
    sounding: list[dict] = []
    for part in _score_parts(document):
        pending: dict[tuple[str, int], int] = {}
        for event in part["notes"]:
            key = (event["voice"], event["midi"])
            if "stop" in event["ties"] and key in pending:
                sounding[pending[key]]["dur_q"] += event["dur_q"]
                if "start" not in event["ties"]:
                    del pending[key]
                continue
            sounding.append(
                {
                    "part": part["part"],
                    "midi": event["midi"],
                    "start_q": event["onset_q"],
                    "dur_q": event["dur_q"],
                }
            )
            if "start" in event["ties"]:
                pending[key] = len(sounding) - 1
    return sounding


def schedule(notes: list[dict], *, tempo_qpm: float = 96.0) -> list[tuple[float, float, float]]:
    """Convert note spans to ``(t0_seconds, dur_seconds, freq_hz)``."""
    seconds_per_quarter = 60.0 / tempo_qpm
    return [
        (
            note["start_q"] * seconds_per_quarter,
            note["dur_q"] * seconds_per_quarter,
            midi_to_freq(note["midi"]),
        )
        for note in notes
    ]
