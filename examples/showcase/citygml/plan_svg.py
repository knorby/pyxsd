"""Render a CityGML 2.0 city model as a top-down SVG plan.

The walk is deliberately namespace-agnostic: it selects ``cityObjectMember``
children whose element is a ``Building`` and then descends the GML geometry
containers by local name. That keeps the renderer working against the stock
OGC schema closure (whose generated classes expose no child accessors for
these elements) and against the small synthetic subset used by the tests.

Geometry comes from the lowest solid the building carries (``lod2Solid``,
then ``lod1Solid``); semantic ``boundedBy`` surfaces are the fallback for
models without a solid. ``surfaceMember`` chains are followed generically,
including ``xlink:href`` references (the OGC LOD2 examples declare their
solids by reference into the semantic surfaces) and
``gml:OrientableSurface`` wrappers. Only the outer ring of each polygon is
kept; the plan view drops the third ordinate.

Run it directly::

    uv run python examples/showcase/citygml/plan_svg.py <sample.gml> [-o out.svg]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.showcase.common import render

SHOWCASE_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = SHOWCASE_ROOT / "out"
DEFAULT_SAMPLE = (
    SHOWCASE_ROOT
    / ".cache"
    / "citygml-schemas"
    / "citygml"
    / "examples"
    / "2.0"
    / "building"
    / "Building_LOD2-EPSG25832.gml"
)
SETUP_HINT = "uv run python examples/showcase/download_sources.py"

#: Light-to-dark ramp: low buildings pale, tall buildings deep blue.
_RAMP_START = (219, 234, 254)
_RAMP_END = (30, 58, 138)

Ring3D = list[tuple[float, float, float]]
Ring2D = list[tuple[float, float]]


def _children(node: Any) -> list[Any]:
    return list(getattr(node, "_children_", ()) or ())


def _local_name(node: Any) -> str | None:
    """Local element name from the bound node's matching name."""
    name = getattr(node, "_name_", None)
    if not isinstance(name, str) or not name:
        return None
    return name.rsplit("}", 1)[-1]


def _find_children(node: Any, local: str) -> list[Any]:
    return [child for child in _children(node) if _local_name(child) == local]


def _find_descendant(node: Any, local: str) -> Any | None:
    for child in _children(node):
        if _local_name(child) == local:
            return child
        found = _find_descendant(child, local)
        if found is not None:
            return found
    return None


def _attribute(node: Any, local: str) -> str | None:
    """Read an attribute by local name (Clark notation or plain key)."""
    for key, value in (getattr(node, "_attribs_", None) or {}).items():
        if isinstance(key, str) and key.rsplit("}", 1)[-1] == local:
            return str(value)
    return None


def _text(node: Any) -> str | None:
    raw = getattr(node, "_value_", None)
    if raw is None:
        return None
    pieces = [str(piece) for piece in raw] if isinstance(raw, (list, tuple)) else [str(raw)]
    text = " ".join(pieces).strip()
    return text or None


def _numbers(node: Any) -> list[float]:
    """Numeric tokens of a simple-content node, whatever its bound type."""
    tokens = getattr(node, "tokens", None)
    if isinstance(tokens, (list, tuple)) and tokens:
        pieces = [str(token) for token in tokens]
    else:
        text = _text(node) or ""
        pieces = text.replace("[", " ").replace("]", " ").replace(",", " ").split()
    values: list[float] = []
    for piece in pieces:
        try:
            values.append(float(piece))
        except ValueError:
            continue
    return values


def _positions(node: Any) -> list[tuple[float, float, float]]:
    """Space-separated ordinates as points; ``srsDimension`` decides the stride."""
    values = _numbers(node)
    dimension = _attribute(node, "srsDimension")
    stride = 3
    if dimension is not None:
        try:
            stride = int(dimension)
        except ValueError:
            stride = 3
    elif len(values) % 3 and len(values) % 2 == 0:
        stride = 2
    if stride not in (2, 3):
        return []
    usable = len(values) - len(values) % stride
    if stride == 2:
        return [(values[index], values[index + 1], 0.0) for index in range(0, usable, 2)]
    return [(values[index], values[index + 1], values[index + 2]) for index in range(0, usable, 3)]


def _closed(ring: Ring3D) -> Ring3D | None:
    if len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        ring = [*ring, ring[0]]
    return ring


def _ring_of(node: Any) -> Ring3D | None:
    """Outer ring of a polygon or triangle node."""
    exterior = _find_descendant(node, "exterior")
    ring_node = exterior if exterior is not None else node
    pos_list = _find_descendant(ring_node, "posList")
    if pos_list is None:
        return None
    return _closed(_positions(pos_list))


def _id_index(root: Any) -> dict[str, Any]:
    """Every ``gml:id`` in the document, for ``xlink:href`` surface members."""
    index: dict[str, Any] = {}

    def visit(node: Any) -> None:
        identifier = _attribute(node, "id")
        if identifier and identifier not in index:
            index[identifier] = node
        for child in _children(node):
            visit(child)

    visit(root)
    return index


def _rings_under(
    node: Any | None, index: dict[str, Any], seen: set[int], depth: int = 0
) -> list[Ring3D]:
    """Rings of every polygon under a geometry container, following references."""
    if node is None or depth > 64:
        return []
    name = _local_name(node) or ""
    if name in ("Polygon", "Triangle"):
        ring = _ring_of(node)
        return [ring] if ring else []
    children = _children(node)
    if children:
        rings: list[Ring3D] = []
        for child in children:
            rings.extend(_rings_under(child, index, seen, depth + 1))
        return rings
    href = _attribute(node, "href")
    if href and href.startswith("#"):
        target = index.get(href[1:])
        if target is not None and id(target) not in seen:
            seen.add(id(target))
            return _rings_under(target, index, seen, depth + 1)
    return []


def _solid_rings(building: Any, index: dict[str, Any]) -> list[Ring3D]:
    for local in ("lod2Solid", "lod1Solid"):
        for solid in _find_children(building, local):
            rings = _rings_under(solid, index, set())
            if rings:
                return rings
    return []


def _semantic_rings(building: Any, index: dict[str, Any]) -> list[Ring3D]:
    """Fallback: polygons hanging off ``boundedBy`` wall/roof/ground surfaces."""
    rings: list[Ring3D] = []
    for prop in _find_children(building, "boundedBy"):
        for surface in _children(prop):
            if not (_local_name(surface) or "").endswith("Surface"):
                continue
            for child in _children(surface):
                if (_local_name(child) or "").endswith("MultiSurface"):
                    rings.extend(_rings_under(child, index, set()))
    return rings


def collect_surfaces(document: Any) -> list[dict]:
    """Buildings with their closed 3D surface rings, in document order.

    Each entry is ``{"id", "rings", "measured_height"}`` where ``rings`` is a
    list of closed ``[(x, y, z), ...]`` lists (outer ring of every surface).
    """
    index = _id_index(document.root)
    buildings = document.xpath("//*[local-name()='cityObjectMember']/*[local-name()='Building']")
    # A BuildingPart is modelled as its own footprint with its own solid and
    # height (the OGC garage example nests one inside a Building); listing
    # both keeps the plan honest instead of merging two heights into one.
    buildings = [*buildings, *document.xpath("//*[local-name()='BuildingPart']")]
    collected: list[dict] = []
    for position, building in enumerate(buildings):
        identifier = _attribute(building, "id") or f"building-{position + 1}"
        measured_height = _numbers(_find_descendant(building, "measuredHeight") or building)
        rings = _solid_rings(building, index) or _semantic_rings(building, index)
        collected.append(
            {
                "id": identifier,
                "rings": rings,
                "measured_height": measured_height[0] if measured_height else None,
            }
        )
    return collected


def collect_buildings(document: Any) -> list[dict]:
    """Buildings projected to plan view: ``id``, closed ``(x, y)`` rings, height."""
    return [
        {
            "id": surface["id"],
            "rings": [[(x, y) for x, y, _z in ring] for ring in surface["rings"]],
            "measured_height": surface["measured_height"],
        }
        for surface in collect_surfaces(document)
    ]


def _height_fraction(value: float | None, low: float | None, high: float | None) -> float:
    if value is None or low is None or high is None or high <= low:
        return 0.5
    return min(1.0, max(0.0, (value - low) / (high - low)))


def _height_color(fraction: float) -> str:
    channels = (
        round(start + (end - start) * fraction)
        for start, end in zip(_RAMP_START, _RAMP_END, strict=True)
    )
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def render_plan(document: Any, *, width: int = 900) -> str:
    """Top-down SVG: every surface ring, filled by normalized measured height."""
    buildings = collect_buildings(document)
    points: list[tuple[float, float]] = [
        point for building in buildings for ring in building["rings"] for point in ring
    ]
    heights = [
        building["measured_height"]
        for building in buildings
        if building["measured_height"] is not None
    ]
    low, high = (min(heights), max(heights)) if heights else (None, None)
    margin = 24
    if not points:
        body = (
            '<rect x="0" y="0" width="100%" height="100%" fill="#f8fafc"/>'
            f'<text x="{margin}" y="{margin + 16}" font-family="sans-serif" '
            'font-size="14" fill="#334155">no building geometry</text>'
        )
        return render.svg(body, width=width, height=160)

    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)
    inner_width = max(width - 2 * margin, 1)
    inner_height = inner_width * span_y / span_x
    height = max(160, round(inner_height + 2 * margin))
    inner_height = height - 2 * margin
    scale = min(inner_width / span_x, inner_height / span_y)
    offset_x = margin + (inner_width - span_x * scale) / 2
    offset_y = margin + (inner_height - span_y * scale) / 2

    parts = ['<rect x="0" y="0" width="100%" height="100%" fill="#f8fafc"/>']
    for building in sorted(buildings, key=lambda entry: entry["id"]):
        fill = _height_color(_height_fraction(building["measured_height"], low, high))
        for ring in building["rings"]:
            coordinates = " ".join(
                f"{render.num(offset_x + (x - min_x) * scale)},"
                f"{render.num(offset_y + (max_y - y) * scale)}"
                for x, y in ring
            )
            parts.append(
                f'<polygon points="{coordinates}" fill="{fill}" stroke="#000000" '
                'stroke-width="1" fill-rule="evenodd" '
                f'data-building="{render.esc(building["id"])}"/>'
            )
    return render.svg("".join(parts), width=width, height=height)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sample", nargs="?", default=str(DEFAULT_SAMPLE), help="CityGML .gml file")
    parser.add_argument(
        "-o",
        "--output",
        default=str(OUT_DIR / "citygml-plan.svg"),
        help="output SVG path (default: examples/showcase/out/citygml-plan.svg)",
    )
    args = parser.parse_args(argv)
    sample = Path(args.sample)
    if not sample.is_file():
        print(f"missing CityGML sample: {sample}\nrun: {SETUP_HINT}", file=sys.stderr)
        return 2
    from examples.showcase.common.schemas import compile_schema

    document = compile_schema("citygml").parse(sample)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_plan(document), encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
