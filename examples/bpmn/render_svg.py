"""Render a BPMN 2.0 diagram from its interchange (DI) geometry.

The semantic model says *what* a shape is; the ``BPMNDiagram`` section says
*where* it goes. The renderer therefore reads the document twice: the
semantic tree supplies each element's tag (task, gateway, event) and name,
and the DI tree supplies ``dc:Bounds`` and ``di:waypoint`` coordinates.
BPMN DI is authoritative -- no auto-layout is attempted.

Run from a pyxsd checkout after installing the pinned sources::

    uv run python examples/bpmn/render_svg.py <file.bpmn> [-o out.svg]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.bpmn.common import render

BPMN_MODEL = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMN_DI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC = "http://www.omg.org/spec/DD/20100524/DC"
DD_DI = "http://www.omg.org/spec/DD/20100524/DI"

NAMESPACES = {"bpmn": BPMN_MODEL, "bpmndi": BPMN_DI, "dc": DC, "di": DD_DI}

#: Element tags that render as rounded rectangles.
_TASK_TAGS = frozenset(
    {
        "task",
        "userTask",
        "serviceTask",
        "sendTask",
        "receiveTask",
        "manualTask",
        "scriptTask",
        "businessRuleTask",
        "callActivity",
    }
)
_SUBPROCESS_TAGS = frozenset({"subProcess", "adHocSubProcess", "transaction"})

_PAD = 12.0


def _kind_for(tag: str) -> str:
    """Map a semantic element tag to the glyph it should render as."""
    if tag in _TASK_TAGS:
        return "task"
    if tag in _SUBPROCESS_TAGS:
        return "subprocess"
    if tag.endswith("Gateway"):
        return "gateway"
    if tag == "startEvent":
        return "start_event"
    if tag == "endEvent":
        return "end_event"
    if tag.endswith("Event"):
        return "event"
    if tag == "lane":
        return "lane"
    if tag == "participant":
        return "participant"
    return "other"


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _semantic_index(document) -> dict[str, dict[str, str]]:
    """Index semantic elements by id: ``{id: {"kind": ..., "name": ...}}``.

    ``document.to_dict()`` uses Clark-notation keys. Child attribute access
    on this ref-based schema returns ``None``, so the index is built from the
    exported mapping rather than from bound attribute descriptors.
    """
    index: dict[str, dict[str, str]] = {}

    def walk(node: dict) -> None:
        for key, value in node.items():
            if not key.startswith("{"):
                continue
            namespace, _, tag = key[1:].partition("}")
            if namespace != BPMN_MODEL:
                continue
            for child in _as_list(value):
                if not isinstance(child, dict):
                    continue
                identifier = child.get("@id")
                if isinstance(identifier, str):
                    index[identifier] = {
                        "kind": _kind_for(tag),
                        "name": str(child.get("@name") or ""),
                    }
                walk(child)

    walk(document.to_dict())
    return index


def _attr(element: object, name: str) -> str:
    value = element._attribs_.get(name)  # type: ignore[attr-defined]
    return "" if value is None else str(value)


def _bounds(shape: object) -> tuple[float, float, float, float] | None:
    for child in shape._children_:  # type: ignore[attr-defined]
        if child._name_ != f"{{{DC}}}Bounds":
            continue
        attributes = child._attribs_
        try:
            return (
                float(attributes["x"]),
                float(attributes["y"]),
                float(attributes["width"]),
                float(attributes["height"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


def di_shapes(document) -> list[dict]:
    """Return one record per ``BPMNDiagram`` shape, in document order.

    ``element_id`` is the semantic id referenced by ``BPMNShape/@bpmnElement``
    (not the shape's own DI id), so callers can join geometry back to the
    process model. ``kind`` classifies the referenced element.
    """
    index = _semantic_index(document)
    shapes: list[dict] = []
    for shape in document.xpath("//bpmndi:BPMNShape", namespaces=NAMESPACES):
        element_id = _attr(shape, "bpmnElement") or _attr(shape, "id")
        bounds = _bounds(shape)
        if bounds is None:
            continue
        semantic = index.get(element_id, {})
        x, y, width, height = bounds
        shapes.append(
            {
                "element_id": element_id,
                "kind": semantic.get("kind", "other"),
                "x": x,
                "y": y,
                "w": width,
                "h": height,
                "is_horizontal": _attr(shape, "isHorizontal").lower() in {"true", "1"},
                "label": semantic.get("name", ""),
            }
        )
    return shapes


def di_edges(document) -> list[dict]:
    """Return one record per ``BPMNEdge`` with its absolute waypoints."""
    index = _semantic_index(document)
    edges: list[dict] = []
    for edge in document.xpath("//bpmndi:BPMNEdge", namespaces=NAMESPACES):
        element_id = _attr(edge, "bpmnElement") or _attr(edge, "id")
        waypoints: list[tuple[float, float]] = []
        for child in edge._children_:
            if child._name_ != f"{{{DD_DI}}}waypoint":
                continue
            attributes = child._attribs_
            try:
                waypoints.append((float(attributes["x"]), float(attributes["y"])))
            except (KeyError, TypeError, ValueError):
                continue
        edges.append(
            {
                "element_id": element_id,
                "waypoints": waypoints,
                "label": index.get(element_id, {}).get("name", ""),
            }
        )
    return edges


_FONT_FAMILY = "Helvetica, Arial, sans-serif"
_STROKE = "#3b3b3b"

_ARROW_DEFS = (
    '<defs><marker id="bpmn-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
    'markerWidth="8" markerHeight="8" orient="auto">'
    '<path d="M 0 0 L 10 5 L 0 10 z" fill="#3b3b3b"/></marker></defs>'
)


def _text_block(
    label: str,
    x: float,
    y: float,
    *,
    font: float,
    anchor: str = "middle",
    fill: str = "#1f2328",
) -> str:
    """Render ``label`` as centred lines of ``<text>``, or ``""`` when empty."""
    lines = [line.strip() for line in label.splitlines() if line.strip()]
    if not lines:
        return ""
    line_height = font * 1.15
    top = y - (len(lines) - 1) * line_height / 2
    return "".join(
        f'<text x="{render.num(x)}" y="{render.num(top + index * line_height)}" '
        f'font-family="{_FONT_FAMILY}" font-size="{render.num(font, 1)}" '
        f'text-anchor="{anchor}" fill="{fill}">{render.esc(line)}</text>'
        for index, line in enumerate(lines)
    )


def _plus_marker(cx: float, bottom: float, size: float) -> str:
    """Small ``[+]`` badge marking a collapsed/expanded subprocess."""
    my = bottom - size * 0.8 - 2
    half = size / 2
    arm = size * 0.3
    return (
        f'<rect x="{render.num(cx - half)}" y="{render.num(my - half)}" '
        f'width="{render.num(size)}" height="{render.num(size)}" fill="#ffffff" '
        f'stroke="{_STROKE}" stroke-width="1"/>'
        f'<line x1="{render.num(cx - arm)}" y1="{render.num(my)}" '
        f'x2="{render.num(cx + arm)}" y2="{render.num(my)}" '
        f'stroke="{_STROKE}" stroke-width="1"/>'
        f'<line x1="{render.num(cx)}" y1="{render.num(my - arm)}" '
        f'x2="{render.num(cx)}" y2="{render.num(my + arm)}" '
        f'stroke="{_STROKE}" stroke-width="1"/>'
    )


def _shape_svg(shape: dict, sx, sy, scale: float, font: float) -> str:
    left, right = sx(shape["x"]), sx(shape["x"] + shape["w"])
    bottom, top = sy(shape["y"]), sy(shape["y"] + shape["h"])
    width, height = right - left, bottom - top
    cx, cy = (left + right) / 2, (top + bottom) / 2
    kind = shape["kind"]
    data = f' data-element="{render.esc(shape["element_id"])}" data-kind="{render.esc(kind)}"'

    if kind == "participant":
        markup = (
            f'<rect x="{render.num(left)}" y="{render.num(top)}" '
            f'width="{render.num(width)}" height="{render.num(height)}" '
            f'fill="#f7f7f7" stroke="#8c8c8c" stroke-width="1.3"{data}/>'
        )
        name = " ".join(shape["label"].split())
        if name:
            markup += (
                f'<text x="0" y="0" transform="translate({render.num(left + font * 0.9)},'
                f'{render.num(cy)}) rotate(-90)" font-family="{_FONT_FAMILY}" '
                f'font-size="{render.num(font, 1)}" text-anchor="middle" '
                f'fill="#333333">{render.esc(name)}</text>'
            )
        return markup

    if kind == "lane":
        return (
            f'<rect x="{render.num(left)}" y="{render.num(top)}" '
            f'width="{render.num(width)}" height="{render.num(height)}" '
            f'fill="none" stroke="#b9b9b9" stroke-width="1"{data}/>'
            + _text_block(
                shape["label"],
                left + 6,
                top + font * 0.95,
                font=font * 0.9,
                anchor="start",
                fill="#4a4a4a",
            )
        )

    if kind in {"task", "subprocess"}:
        radius = min(10 * scale, width / 4, height / 4)
        markup = (
            f'<rect x="{render.num(left)}" y="{render.num(top)}" '
            f'width="{render.num(width)}" height="{render.num(height)}" '
            f'rx="{render.num(radius)}" ry="{render.num(radius)}" fill="#ffffff" '
            f'stroke="#3b6ea5" stroke-width="1.2"{data}/>'
            + _text_block(shape["label"], cx, cy, font=font)
        )
        if kind == "subprocess":
            markup += _plus_marker(cx, bottom, min(width, height) * 0.18)
        return markup

    if kind == "gateway":
        points = (
            f"{render.num(cx)},{render.num(top)} {render.num(right)},{render.num(cy)} "
            f"{render.num(cx)},{render.num(bottom)} {render.num(left)},{render.num(cy)}"
        )
        return (
            f'<polygon points="{points}" fill="#ffffff" stroke="#8a6d3b" '
            f'stroke-width="1.2"{data}/>'
            + _text_block(shape["label"], cx, bottom + font * 1.15, font=font, fill="#4a4a4a")
        )

    if kind in {"start_event", "end_event", "event"}:
        radius = min(width, height) / 2
        stroke_width = 3 if kind == "end_event" else 1.4
        fills = {"start_event": "#eef6ee", "end_event": "#fdeeee", "event": "#eef1fa"}
        return (
            f'<circle cx="{render.num(cx)}" cy="{render.num(cy)}" '
            f'r="{render.num(radius)}" fill="{fills[kind]}" stroke="{_STROKE}" '
            f'stroke-width="{stroke_width}"{data}/>'
            + _text_block(shape["label"], cx, bottom + font * 1.15, font=font, fill="#4a4a4a")
        )

    return (
        f'<rect x="{render.num(left)}" y="{render.num(top)}" '
        f'width="{render.num(width)}" height="{render.num(height)}" fill="#ffffff" '
        f'stroke="#a0a0a0" stroke-width="1" stroke-dasharray="4 3"{data}/>'
        + _text_block(shape["label"], cx, cy, font=font, fill="#4a4a4a")
    )


def _edge_svg(edge: dict, sx, sy, font: float) -> str:
    waypoints = edge["waypoints"]
    if len(waypoints) < 2:
        return ""
    points = " ".join(f"{render.num(sx(x))},{render.num(sy(y))}" for x, y in waypoints)
    markup = (
        f'<polyline points="{points}" fill="none" stroke="{_STROKE}" '
        f'stroke-width="1.4" marker-end="url(#bpmn-arrow)" '
        f'data-element="{render.esc(edge["element_id"])}" data-kind="edge"/>'
    )
    if not edge["label"]:
        return markup
    # Anchor the label on the midpoint of the longest segment: always on the
    # path, and stable for edges with many bends.
    longest = max(
        range(len(waypoints) - 1),
        key=lambda index: (
            (waypoints[index + 1][0] - waypoints[index][0]) ** 2
            + (waypoints[index + 1][1] - waypoints[index][1]) ** 2
        ),
    )
    start, end = waypoints[longest], waypoints[longest + 1]
    return markup + _text_block(
        edge["label"],
        sx((start[0] + end[0]) / 2),
        sy((start[1] + end[1]) / 2) - font * 0.7,
        font=font * 0.9,
        fill="#4a4a4a",
    )


def render_svg(document, *, width: int = 1000) -> str:
    """Render a BPMN document's DI geometry as a standalone SVG string.

    The drawing box comes from the DI coordinates, scaled uniformly to
    ``width``. BPMN DI's y axis grows up while SVG's grows down, so every
    y coordinate is flipped. Output is deterministic: no timestamps and no
    iteration over unordered collections.
    """
    shapes = di_shapes(document)
    edges = di_edges(document)
    if not shapes and not edges:
        return render.svg(_ARROW_DEFS, width=width, height=max(1, round(width / 4)))

    x_values = [shape["x"] for shape in shapes]
    x_values += [shape["x"] + shape["w"] for shape in shapes]
    y_values = [shape["y"] for shape in shapes]
    y_values += [shape["y"] + shape["h"] for shape in shapes]
    for edge in edges:
        if len(edge["waypoints"]) < 2:
            continue  # not rendered, so it must not widen the canvas
        for x, y in edge["waypoints"]:
            x_values.append(x)
            y_values.append(y)
    min_x, max_x = min(x_values), max(x_values)
    min_y, max_y = min(y_values), max(y_values)

    pad = min(_PAD, width / 4)
    scale = (width - 2 * pad) / max(max_x - min_x, 1.0)
    height = max(1, round((max_y - min_y) * scale + 2 * pad))
    font = min(14.0, max(9.0, 12.0 * scale))

    def sx(value: float) -> float:
        return pad + (value - min_x) * scale

    def sy(value: float) -> float:
        return pad + (max_y - value) * scale

    body = [_ARROW_DEFS]
    # Pools and lanes are backdrops: draw them before the flow elements so
    # their frames never cover a task or an edge.
    backdrops = [shape for shape in shapes if shape["kind"] in {"participant", "lane"}]
    flow_shapes = [shape for shape in shapes if shape["kind"] not in {"participant", "lane"}]
    body.extend(_shape_svg(shape, sx, sy, scale, font) for shape in backdrops)
    body.extend(_shape_svg(shape, sx, sy, scale, font) for shape in flow_shapes)
    body.extend(_edge_svg(edge, sx, sy, font) for edge in edges)
    return render.svg("".join(body), width=width, height=height)


def main(argv: list[str] | None = None) -> int:
    from examples.bpmn.common.schemas import compile_schema
    from examples.bpmn.common.sources import FAMILY_ROOT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path, help="BPMN 2.0 XML file to render")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output SVG path (default: examples/bpmn/out/<stem>.svg)",
    )
    parser.add_argument("--width", type=int, default=1000, help="SVG width in pixels")
    args = parser.parse_args(argv)

    schema = compile_schema()
    document = schema.parse(args.document)
    if document.report.has_errors:
        print(
            f"{args.document}: {document.report.error_count} validation error(s)", file=sys.stderr
        )
        for issue in document.report.issues[:10]:
            print(f"  {issue}", file=sys.stderr)
        return 1

    output = args.output or FAMILY_ROOT / "out" / f"{args.document.stem}.svg"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_svg(document, width=args.width), encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
