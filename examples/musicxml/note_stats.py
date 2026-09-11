"""Report basic statistics for a MusicXML partwise score.

Run from this directory so pyxsd can find the module:

    uv run pyxsd -i instance.xml -s schema.xsd -k -o /dev/null \
        -t 'NoteStats()' -o note-stats.xml

The transform replaces the parsed score with a small ``noteStats``
summary tree, so the transformed output is easy to inspect.
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

            pitch = self.find("pitch", note)
            if pitch is None:
                rests += 1
                continue

            step = str(self.find("step", pitch))
            octave = int(self.find("octave", pitch))
            alter = self.find("alter", pitch)
            # MIDI-style numbering: C4 (middle C) is 60.
            semitones = STEP_SEMITONES[step] + 12 * (octave + 1)
            if alter is not None:
                semitones += int(alter)
            pitchValues.append(semitones)

        summary = self.makeElemObj("noteStats")
        summary._attribs_ = {
            "totalNotes": str(len(notes)),
            "totalDuration": str(totalDuration),
            "rests": str(rests),
            "lowestPitch": str(min(pitchValues)) if pitchValues else "",
            "highestPitch": str(max(pitchValues)) if pitchValues else "",
        }
        return summary
