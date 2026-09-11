"""Report summary statistics for a MusicXML partwise score.

The instance is Johann Sebastian Bach's four-part chorale *Christ unser
Herr zum Jordan kam*, BWV 66.6, a public-domain work distributed with the
BSD-licensed music21 corpus. It is validated against the real MusicXML 4.0
schema (fetched by ``download_schemas.py``) in namespaced mode.

Run from the repository root (the schema is fetched first):

    uv run python examples/musicxml/download_schemas.py
    uv run pyxsd -i examples/musicxml/instance.xml \
        -s examples/musicxml/schemas/musicxml.xsd -k -o /dev/null \
        --namespaces strict -t 'NoteStats()' -o note-stats.xml

The transform replaces the parsed score with a small ``noteStats`` summary
tree, so the transformed output is easy to inspect.
"""

from pyxsd.transforms import Transform

# Semitone offset of each natural pitch class within an octave.
STEP_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


class NoteStats(Transform):
    """Counts notes/rests and computes the sounding range of a score."""

    def __init__(self, root):
        super().__init__(root)

    def __call__(self):
        notes = self.getElementsByName(self.root, "note")
        totalDuration = 0
        rests = 0
        pitchValues = []

        for note in notes:
            duration = self.find("duration", note)
            if duration is not None:
                totalDuration += int(duration)

            # A <rest> child marks a rest; an <unpitched> or <cue> note is
            # neither a rest nor a pitched note, so it contributes no range.
            if self.find("rest", note) is not None:
                rests += 1
                continue

            pitch = self.find("pitch", note)
            if pitch is None:
                continue

            step = str(self.find("step", pitch))
            octave = int(self.find("octave", pitch))
            # MIDI-style numbering: C4 (middle C) is 60.
            semitones = STEP_SEMITONES[step] + 12 * (octave + 1)
            alter = self.find("alter", pitch)
            if alter is not None:
                semitones += int(alter)
            pitchValues.append(semitones)

        summary = self.makeElemObj("noteStats")
        summary._attribs_ = {
            "totalNotes": str(len(notes)),
            "totalDuration": str(totalDuration),
            "rests": str(rests),
            "parts": str(len(self.getElementsByName(self.root, "part"))),
            "measures": str(len(self.getElementsByName(self.root, "measure"))),
            "lowestPitch": str(min(pitchValues)) if pitchValues else "",
            "highestPitch": str(max(pitchValues)) if pitchValues else "",
        }
        return summary
