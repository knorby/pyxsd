"""Report summary statistics for a MusicXML partwise score.

The instance is Johann Sebastian Bach's four-part chorale *Christ unser
Herr zum Jordan kam*, BWV 66.6, a public-domain work distributed with the
BSD-licensed music21 corpus. It is validated against the real MusicXML 4.0
schema (installed by ``download_sources.py``) in namespaced mode.

Run from the repository root (the schema is fetched first):

    uv run python examples/musicxml/download_sources.py
    uv run python examples/musicxml/note_stats.py

The transform replaces the parsed score with a small ``noteStats`` summary
tree, so the transformed output is easy to inspect.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pyxsd.transforms import Transform

_DEFAULT_INPUT = Path(__file__).resolve().parent / "instance.xml"
_DEFAULT_OUTPUT = Path(__file__).resolve().parent / "out" / "note-stats.xml"

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


def main(argv: list[str] | None = None) -> int:
    """Validate the bundled score and write its ``NoteStats`` summary tree."""
    import argparse

    from examples.musicxml.common.schemas import compile_schema

    from pyxsd.document import write_tree

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-i", "--input", type=Path, default=_DEFAULT_INPUT)
    parser.add_argument("-o", "--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    document = compile_schema().parse(args.input)
    result = document.transform(lambda root: NoteStats(root)())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_tree(result, args.output)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
