"""Draw a compact score preview from a validated MusicXML partwise score.

One five-line staff per part, measure bars, noteheads at their diatonic
staff positions, written accidentals (``♯``/``♭``) and tie arcs. The
preview reads the same pitch/time walk as the WAV renderer in
:mod:`examples.musicxml.synthesize`, so what you see and what you
hear cannot disagree.

The bundled example score is Bach's *Christ unser Herr zum Jordan kam*,
BWV 66.6, a public-domain work from the BSD-licensed music21 corpus.

Run it from the repository root::

    uv run python examples/musicxml/render_preview.py [-o out.svg]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.musicxml.common.render import esc, num, svg
from examples.musicxml.common.schemas import compile_schema
from examples.musicxml.synthesize import STEP_INDEX, _score_parts

_DEFAULT_INPUT = Path(__file__).resolve().parents[1] / "instance.xml"
_DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "out" / "score-preview.svg"

_LINE_GAP = 8.0
_STAFF_HEIGHT = _LINE_GAP * 4
_STAFF_GAP = 44.0
_MARGIN_LEFT = 120.0
_TIME_INSET = 34.0
_MARGIN_RIGHT = 24.0
_MARGIN_TOP = 34.0
_MARGIN_BOTTOM = 26.0
_STEP = _LINE_GAP / 2

#: Clef sign -> (step, octave) of the line the clef names.
_CLEF_ANCHORS = {"G": ("G", 4), "F": ("F", 3), "C": ("C", 4)}
_ACCIDENTALS = {1.0: "♯", 2.0: "𝄪", -1.0: "♭", -2.0: "𝄫"}
_STYLE = (
    "<style>"
    "text{font-family:Georgia,'Times New Roman',serif}"
    ".staff-line{stroke:#444;stroke-width:1}"
    ".bar{stroke:#777;stroke-width:.9}"
    ".ledger{stroke:#444;stroke-width:1}"
    ".notehead{fill:#111}"
    ".accidental{font-size:13px}"
    ".tie{stroke:#111;stroke-width:1;fill:none}"
    ".part-label{font-size:13px}"
    ".clef{font-size:15px;font-weight:bold}"
    "</style>"
)


def _center_diatonic(clef: tuple[str, int]) -> int:
    """Diatonic index of the staff's middle line for a clef."""
    sign, line = clef
    step, octave = _CLEF_ANCHORS.get(sign, _CLEF_ANCHORS["G"])
    anchor = octave * 7 + STEP_INDEX[step]
    return anchor + (3 - line) * 2


def _note_y(step: str, octave: int, center: int, staff_top: float) -> float:
    """Vertical position of a pitch on a staff whose middle line is *center*."""
    offset = (octave * 7 + STEP_INDEX[step]) - center
    return staff_top + _STAFF_HEIGHT / 2 - offset * _STEP


def _staff_body(part: dict, staff_top: float, x_for, total_q: float) -> list[str]:
    """SVG elements for one part's staff, bars, noteheads and ties."""
    body: list[str] = []
    staff_bottom = staff_top + _STAFF_HEIGHT
    x_start = _MARGIN_LEFT
    x_end = x_for(total_q)

    body.append(f'<g class="staff" data-part="{esc(part["part"])}">')
    for line in range(5):
        y = staff_top + line * _LINE_GAP
        body.append(
            f'<line class="staff-line" x1="{num(x_start)}" y1="{num(y)}" '
            f'x2="{num(x_end)}" y2="{num(y)}"/>'
        )
    label = part.get("name") or part["part"]
    body.append(
        f'<text class="part-label" x="8" y="{num(staff_top + _STAFF_HEIGHT / 2 + 4)}">'
        f"{esc(label)}</text>"
    )
    sign, line = part["clef"]
    body.append(
        f'<text class="clef" x="{num(_MARGIN_LEFT + 6)}" '
        f'y="{num(staff_top + _STAFF_HEIGHT / 2 + 5)}">{esc(f"{sign}{line}")}</text>'
    )

    for measure in part["measures"]:
        x = x_for(measure["start_q"])
        body.append(
            f'<line class="bar" x1="{num(x)}" y1="{num(staff_top)}" '
            f'x2="{num(x)}" y2="{num(staff_bottom)}"/>'
        )
    body.append(
        f'<line class="bar" x1="{num(x_end)}" y1="{num(staff_top)}" '
        f'x2="{num(x_end)}" y2="{num(staff_bottom)}"/>'
    )

    center = _center_diatonic(part["clef"])
    pending_ties: dict[tuple[str, int], tuple[float, float]] = {}
    for event in part["notes"]:
        x = x_for(event["onset_q"])
        y = _note_y(event["step"], event["octave"], center, staff_top)
        offset = (event["octave"] * 7 + STEP_INDEX[event["step"]]) - center
        if abs(offset) > 4:
            direction = 1 if offset > 0 else -1
            limit = abs(offset) if abs(offset) % 2 == 0 else abs(offset) - 1
            for ledger in range(6, limit + 1, 2):
                ledger_y = staff_top + _STAFF_HEIGHT / 2 - direction * ledger * _STEP
                body.append(
                    f'<line class="ledger" x1="{num(x - 7)}" y1="{num(ledger_y)}" '
                    f'x2="{num(x + 7)}" y2="{num(ledger_y)}"/>'
                )
        if event["alter"]:
            mark = _ACCIDENTALS.get(event["alter"], num(event["alter"]))
            body.append(
                f'<text class="accidental" x="{num(x - 12)}" y="{num(y + 3.5)}">{esc(mark)}</text>'
            )
        body.append(f'<ellipse class="notehead" cx="{num(x)}" cy="{num(y)}" rx="4.6" ry="3.4"/>')
        key = (event["voice"], event["midi"])
        if "stop" in event["ties"] and key in pending_ties:
            start_x, start_y = pending_ties.pop(key)
            end_x = x - 4.6
            curve = min(start_y, y) - 8
            body.append(
                f'<path class="tie" d="M {num(start_x)} {num(start_y)} '
                f"Q {num((start_x + end_x) / 2)} {num(curve)} "
                f'{num(end_x)} {num(y)}"/>'
            )
        if "start" in event["ties"]:
            pending_ties[key] = (x + 4.6, y)
    body.append("</g>")
    return body


def render_preview(document: object, *, width: int = 1000) -> str:
    """Return an SVG score preview with one staff per part.

    *width* is the total image width; staves keep their fixed vertical
    layout, so the image gets wider, not taller, as the score lengthens.
    """
    if width <= 0:
        raise ValueError("width must be positive")
    parts = _score_parts(document)
    plot_left = _MARGIN_LEFT + _TIME_INSET
    plot_width = width - plot_left - _MARGIN_RIGHT
    total_q = max(
        (part["measures"][-1]["end_q"] for part in parts if part["measures"]),
        default=0.0,
    )
    total_q = total_q or 1.0

    def x_for(position_q: float) -> float:
        return plot_left + position_q / total_q * plot_width

    height = (
        _MARGIN_TOP
        + len(parts) * _STAFF_HEIGHT
        + max(0, len(parts) - 1) * _STAFF_GAP
        + _MARGIN_BOTTOM
    )
    body = [_STYLE]
    for index, part in enumerate(parts):
        staff_top = _MARGIN_TOP + index * (_STAFF_HEIGHT + _STAFF_GAP)
        body.extend(_staff_body(part, staff_top, x_for, total_q))
    return svg("".join(body), width=width, height=round(height))


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point: validate a score, then draw its preview."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=_DEFAULT_INPUT,
        help="MusicXML partwise score (default: the bundled Bach chorale)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="destination SVG (default: examples/musicxml/out/score-preview.svg)",
    )
    parser.add_argument("--width", type=int, default=1000, help="image width in pixels")
    args = parser.parse_args(argv)

    try:
        schema = compile_schema()
    except FileNotFoundError as exc:
        parser.error(str(exc))
    document = schema.parse(args.input)
    report = document.report
    print(
        f"validated {args.input}: {report.error_count} error(s), {report.warning_count} warning(s)"
    )
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_preview(document, width=args.width) + "\n", encoding="utf-8")
    print(f"wrote {destination} ({destination.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
