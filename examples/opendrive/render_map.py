"""Render an ASAM OpenDRIVE road network as an SVG lane map.

OpenDRIVE describes a road by a reference line in a piecewise geometry
list (``line``, ``arc``, ``spiral``, ``poly3``, ``paramPoly3``) plus a
cross-section of lanes. This module samples each geometry primitive into
inertial ``(x, y)`` points, offsets lane boundaries along the local
normal, and draws the result with the stdlib-only SVG helpers from
``common.render``.

The geometry sampler accepts a plain, JSON-able dict::

    {
        "primitive": "arc",
        "x": 0.0, "y": 0.0, "hdg": 0.0,
        "length": 10.0,
        "curvature": 0.02,
    }

Element attributes are read with or without OpenDRIVE's ``@`` prefix,
so a flattened ``to_dict`` geometry record works unchanged once the
caller adds ``primitive``: that key names the declared child element.
The remaining primitive attributes (``curvature``, ``curvStart``/``curvEnd``,
``a``/``b``/``c``/``d``, or ``uA``…``vD``/``pRange``) sit alongside the
geometry attributes.

Run the map renderer from a checkout::

    uv run python examples/opendrive/render_map.py \\
        examples/opendrive/.cache/opendrive-data/Town01.xodr

Sources are fetched with ``examples/opendrive/download_sources.py``; see
``SOURCES.md`` for provenance and licenses.
"""

from __future__ import annotations

import argparse
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.opendrive.common import render
from examples.opendrive.common.schemas import compile_schema

_PRIMITIVES = ("line", "arc", "spiral", "poly3", "paramPoly3")
_PALETTE = (
    "#2563eb",
    "#dc2626",
    "#16a34a",
    "#9333ea",
    "#ea580c",
    "#0891b2",
    "#ca8a04",
    "#be185d",
)
_EPSILON = 1e-9


def _as_list(value: object) -> list:
    """Normalize ``to_dict``'s single-or-list convention to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _value(attrs: dict, name: str) -> object:
    """Read an attribute with or without ``to_dict``'s ``@`` prefix."""
    if name in attrs:
        return attrs[name]
    return attrs.get(f"@{name}")


def _number(attrs: dict, name: str, default: float = 0.0) -> float:
    try:
        return float(_value(attrs, name))
    except (TypeError, ValueError):
        return default


def _poly(attrs: dict, ds: float, prefix: str = "") -> float:
    """Evaluate ``a + b·ds + c·ds² + d·ds³`` from an attribute mapping."""
    return sum(_number(attrs, f"{prefix}{key}") * ds**power for power, key in enumerate("abcd"))


def _poly_uv(attrs: dict, value: float, prefix: str) -> float:
    """Evaluate ``uA + uB·p + uC·p² + uD·p³`` (or the ``v`` equivalent)."""
    return sum(_number(attrs, f"{prefix}{key}") * value**power for power, key in enumerate("ABCD"))


def _piecewise_poly(records: list, s: float) -> float:
    """Evaluate the last cubic record whose ``s`` is at or before ``s``."""
    ordered = sorted(
        (record for record in records if isinstance(record, dict)),
        key=lambda record: _number(record, "s"),
    )
    if not ordered:
        return 0.0
    chosen = ordered[0]
    for record in ordered:
        if _number(record, "s") <= s + _EPSILON:
            chosen = record
        else:
            break
    return _poly(chosen, s - _number(chosen, "s"))


def _integrate(function, start: float, end: float, panels: int = 8) -> float:
    """Composite Simpson integration over one short interval."""
    if end <= start:
        return 0.0
    panels = max(2, panels + panels % 2)
    width = (end - start) / panels
    total = function(start) + function(end)
    for index in range(1, panels):
        total += (4 if index % 2 else 2) * function(start + index * width)
    return total * width / 3.0


def _sample_geometry(
    geom: dict, length: float, step: float = 1.0
) -> list[tuple[float, float, float, float]]:
    """Sample a geometry into ``(s, x, y, hdg)`` points, ending at ``length``."""
    if step <= 0:
        raise ValueError("step must be positive")
    x0 = _number(geom, "x")
    y0 = _number(geom, "y")
    hdg0 = _number(geom, "hdg")
    total = float(length)
    if total <= 0:
        return [(0.0, x0, y0, hdg0)]

    count = max(1, math.ceil(total / step))
    s_values = [index * total / count for index in range(count)]
    s_values.append(total)
    primitive = str(_value(geom, "primitive") or "line").strip()

    if primitive == "line":
        return [(s, x0 + s * math.cos(hdg0), y0 + s * math.sin(hdg0), hdg0) for s in s_values]

    if primitive == "arc":
        curvature = _number(geom, "curvature")
        if abs(curvature) < _EPSILON:
            return [(s, x0 + s * math.cos(hdg0), y0 + s * math.sin(hdg0), hdg0) for s in s_values]
        points = []
        cos_start, sin_start = math.cos(hdg0), math.sin(hdg0)
        for s in s_values:
            heading = hdg0 + curvature * s
            x = x0 + (math.sin(heading) - sin_start) / curvature
            y = y0 - (math.cos(heading) - cos_start) / curvature
            points.append((s, x, y, heading))
        return points

    if primitive == "spiral":
        curv_start = _number(geom, "curvStart")
        curv_end = _number(geom, "curvEnd", curv_start)

        def heading(s: float) -> float:
            return hdg0 + curv_start * s + (curv_end - curv_start) * s * s / (2.0 * total)

        points = [(0.0, x0, y0, hdg0)]
        x, y = x0, y0
        previous = 0.0
        for s in s_values[1:]:
            x += _integrate(lambda value: math.cos(heading(value)), previous, s)
            y += _integrate(lambda value: math.sin(heading(value)), previous, s)
            points.append((s, x, y, heading(s)))
            previous = s
        return points

    cos_hdg, sin_hdg = math.cos(hdg0), math.sin(hdg0)
    points = []
    if primitive == "poly3":
        for s in s_values:
            lateral = _poly(geom, s)
            tangent = math.atan2(
                _number(geom, "b") + 2 * _number(geom, "c") * s + 3 * _number(geom, "d") * s * s,
                1.0,
            )
            points.append(
                (
                    s,
                    x0 + s * cos_hdg - lateral * sin_hdg,
                    y0 + s * sin_hdg + lateral * cos_hdg,
                    hdg0 + tangent,
                )
            )
        return points

    if primitive == "paramPoly3":
        range_name = str(_value(geom, "pRange") or "arcLength").strip().lower()
        normalized = range_name in {"normalized", "1", "true"}
        for s in s_values:
            parameter = s / total if normalized else s
            along = _poly_uv(geom, parameter, "u")
            lateral = _poly_uv(geom, parameter, "v")
            du = (
                _number(geom, "uB")
                + 2 * _number(geom, "uC") * parameter
                + 3 * _number(geom, "uD") * parameter * parameter
            )
            dv = (
                _number(geom, "vB")
                + 2 * _number(geom, "vC") * parameter
                + 3 * _number(geom, "vD") * parameter * parameter
            )
            points.append(
                (
                    s,
                    x0 + along * cos_hdg - lateral * sin_hdg,
                    y0 + along * sin_hdg + lateral * cos_hdg,
                    hdg0 + math.atan2(dv, du),
                )
            )
        return points

    raise ValueError(f"unsupported OpenDRIVE geometry primitive: {primitive!r}")


def sample_geometry(geom: dict, length: float, step: float = 1.0) -> list[tuple[float, float]]:
    """Sample one OpenDRIVE geometry primitive into inertial ``(x, y)`` points.

    ``geom`` is a JSON-able dict with the geometry element's attributes
    and a ``primitive`` key naming its child (see the module docstring).
    ``length`` is the geometry's arc length in metres; sampling starts at
    the declared ``x``/``y``/``hdg`` and always includes the endpoint.
    """
    return [(x, y) for _, x, y, _ in _sample_geometry(geom, length, step)]


def _geometry(raw: dict) -> dict:
    """Flatten one ``to_dict`` geometry record into the sampler's dict shape."""
    geom: dict[str, object] = {
        "primitive": "line",
        "s": _number(raw, "s"),
        "x": _number(raw, "x"),
        "y": _number(raw, "y"),
        "hdg": _number(raw, "hdg"),
        "length": _number(raw, "length"),
    }
    for name in _PRIMITIVES:
        if name not in raw:
            continue
        geom["primitive"] = name
        child = raw.get(name)
        if isinstance(child, dict):
            for key, value in child.items():
                geom[key.lstrip("@")] = value
        break
    return geom


def _node(record: dict, name: str) -> dict:
    value = record.get(name)
    return value if isinstance(value, dict) else {}


def _road_samples(road: dict) -> list[tuple[float, float, float, float]]:
    """Concatenate a road's geometries, dropping shared boundary samples."""
    geometries = sorted(
        _as_list(_node(road, "planView").get("geometry")),
        key=lambda geometry: _number(geometry, "s"),
    )
    samples: list[tuple[float, float, float, float]] = []
    for raw in geometries:
        geom = _geometry(raw)
        for point in _sample_geometry(geom, _number(geom, "length")):
            if samples and abs(point[0] - samples[-1][0]) < _EPSILON:
                continue
            samples.append(point)
    return samples


def _section_at(sections: list[dict], s: float) -> dict | None:
    if not sections:
        return None
    chosen = sections[0]
    for section in sections:
        if _number(section, "s") <= s + _EPSILON:
            chosen = section
        else:
            break
    return chosen


def _lane_width(lane: dict, s: float, section_s: float) -> float:
    widths = sorted(
        (width for width in _as_list(lane.get("width")) if isinstance(width, dict)),
        key=lambda width: _number(width, "sOffset"),
    )
    if not widths:
        return 0.0
    chosen = None
    for width in widths:
        if section_s + _number(width, "sOffset") <= s + _EPSILON:
            chosen = width
        else:
            break
    if chosen is None:
        chosen = widths[0]
    ds = s - (section_s + _number(chosen, "sOffset"))
    return _poly(chosen, ds)


def _offset_point(x: float, y: float, hdg: float, offset: float) -> tuple[float, float]:
    """Move a centerline point along its left normal by ``offset`` metres."""
    return (x - offset * math.sin(hdg), y + offset * math.cos(hdg))


def _lane_edges(road: dict, samples: list[tuple[float, float, float, float]]) -> list[dict]:
    lanes = _node(road, "lanes")
    sections = sorted(_as_list(lanes.get("laneSection")), key=lambda section: _number(section, "s"))
    if not sections or not samples:
        return []

    edges: dict[int, list[tuple[float, float]]] = {}
    for s, x, y, hdg in samples:
        section = _section_at(sections, s)
        if section is None:
            continue
        section_s = _number(section, "s")
        lane_offset = _piecewise_poly(_as_list(lanes.get("laneOffset")), s)
        for side, sign in (("left", 1.0), ("right", -1.0)):
            cumulative = 0.0
            side_lanes = sorted(
                (
                    lane
                    for lane in _as_list(_node(section, side).get("lane"))
                    if isinstance(lane, dict)
                ),
                key=lambda lane: abs(_number(lane, "id")),
            )
            for lane in side_lanes:
                lane_id = int(_number(lane, "id"))
                if (sign > 0 and lane_id <= 0) or (sign < 0 and lane_id >= 0):
                    continue
                cumulative += _lane_width(lane, s, section_s)
                offset = sign * cumulative + lane_offset
                edges.setdefault(lane_id, []).append(_offset_point(x, y, hdg, offset))
    return [{"id": lane_id, "points": edges[lane_id]} for lane_id in sorted(edges)]


def road_paths(document) -> list[dict]:
    """Walk an OpenDRIVE document into drawable road and lane polylines.

    Returns one dict per road in document order::

        {
            "road_id": str,
            "length": float,
            "centerline": [(x, y), ...],
            "lanes": [{"id": int, "points": [(x, y), ...]}, ...],
        }

    Lane points are the outer boundary of each lane: the cumulative lane
    widths offset along the local left normal, plus the road-level
    ``laneOffset`` polynomial. The center lane (id 0) is not drawn.
    """
    data = document.to_dict()
    roads = _as_list(data.get("road"))
    result = []
    for road in roads:
        if not isinstance(road, dict):
            continue
        samples = _road_samples(road)
        length = _number(road, "length")
        if length <= 0 and samples:
            length = samples[-1][0]
        result.append(
            {
                "road_id": str(_value(road, "id")),
                "length": length,
                "centerline": [(x, y) for _, x, y, _ in samples],
                "lanes": _lane_edges(road, samples),
            }
        )
    return result


def _path_data(points: list[tuple[float, float]], project) -> str:
    first_x, first_y = project(*points[0])
    parts = [f"M{render.num(first_x)} {render.num(first_y)}"]
    parts.extend(f"L{render.num(x)} {render.num(y)}" for x, y in (project(*p) for p in points[1:]))
    return "".join(parts)


def render_map(document, *, width: int = 1000) -> str:
    """Render the reference lines and lane boundaries as a standalone SVG."""
    paths = road_paths(document)
    all_points = [point for path in paths for point in path["centerline"]]
    all_points.extend(point for path in paths for lane in path["lanes"] for point in lane["points"])
    if not all_points:
        return render.svg("", width=width, height=120)

    min_x = min(x for x, _ in all_points)
    max_x = max(x for x, _ in all_points)
    min_y = min(y for _, y in all_points)
    max_y = max(y for _, y in all_points)
    span_x = max(max_x - min_x, _EPSILON)
    span_y = max(max_y - min_y, _EPSILON)

    margin = 24.0
    scale = (width - 2 * margin) / span_x
    if span_y * scale + 2 * margin > 4 * width:
        scale = (4 * width - 2 * margin) / span_y
    height = max(140, round(span_y * scale + 2 * margin))
    offset_x = margin + ((width - 2 * margin) - span_x * scale) / 2
    offset_y = margin + ((height - 2 * margin) - span_y * scale) / 2

    def project(x: float, y: float) -> tuple[float, float]:
        return (offset_x + (x - min_x) * scale, offset_y + (max_y - y) * scale)

    body = [
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#f8fafc"/>',
        '<g fill="none" stroke-linecap="round" stroke-linejoin="round">',
    ]
    for path in paths:
        for lane in path["lanes"]:
            if lane["points"]:
                body.append(
                    f'<path class="lane" data-road="{render.esc(path["road_id"])}" '
                    f'stroke="#cbd5e1" stroke-width="0.6" d="{_path_data(lane["points"], project)}"/>'
                )
    for color, path in zip(_PALETTE * ((len(paths) // len(_PALETTE)) + 1), paths, strict=False):
        if path["centerline"]:
            body.append(
                f'<path class="centerline" data-road="{render.esc(path["road_id"])}" '
                f'stroke="{color}" stroke-width="1.1" d="{_path_data(path["centerline"], project)}"/>'
            )
    body.append("</g>")

    font_size = max(8, width // 80)
    for color, path in zip(_PALETTE * ((len(paths) // len(_PALETTE)) + 1), paths, strict=False):
        if not path["centerline"]:
            continue
        label_x, label_y = project(*path["centerline"][0])
        body.append(
            f'<text class="road-label" x="{render.num(label_x)}" y="{render.num(label_y)}" '
            f'font-family="sans-serif" font-size="{font_size}" fill="{color}">'
            f"{render.esc(path['road_id'])}</text>"
        )
    return render.svg("".join(body), width=width, height=height)


def _schema_variant(path: Path) -> str:
    """Pick the pinned schema entry from the instance header's revMinor."""
    root = ET.parse(path).getroot()
    header = next((child for child in root if child.tag.rsplit("}", 1)[-1] == "header"), None)
    minor = "4" if header is None else header.get("revMinor", "4")
    return {"4": "1.4", "5": "1.5"}.get(minor, "1.7")


def _default_output(document: Path) -> Path:
    return Path(__file__).resolve().parent / "out" / f"{document.stem}.svg"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render an OpenDRIVE .xodr file as SVG.")
    parser.add_argument("document", type=Path, help="path to an .xodr file")
    parser.add_argument("-o", "--output", type=Path, help="SVG destination (default: ../out/)")
    args = parser.parse_args(argv)

    schema = compile_schema(_schema_variant(args.document))
    document = schema.parse(args.document)
    output = args.output or _default_output(args.document)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_map(document), encoding="utf-8")
    print(f"wrote {output}")
    if document.report.has_errors:
        print(
            f"{document.report.error_count} validation error(s) in {args.document}", file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
