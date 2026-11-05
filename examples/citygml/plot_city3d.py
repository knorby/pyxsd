"""Render the surfaces of a CityGML model with Matplotlib's 3D toolkit.

Faces come from :func:`examples.citygml.plan_svg.collect_surfaces`,
so the same reference-following walk feeds both renderers. Every closed
surface ring becomes one ``Poly3DCollection`` face, coloured by a mix of
absolute elevation and the building's ``measuredHeight``; walls and roofs
then read as one shaded mass.

matplotlib is imported inside :func:`plot_city_3d` so importing this module
(and the SVG plan renderer) never needs the ``examples-matplotlib`` group.

Run it directly::

    uv run python examples/citygml/plot_city3d.py <sample.gml> [-o out.png]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.citygml.plan_svg import (
    DEFAULT_SAMPLE,
    OUT_DIR,
    SETUP_HINT,
    collect_surfaces,
)

#: Ground tones for low, flat faces; slate for tall ones.
_RAMP_LOW = (0.94, 0.89, 0.79)
_RAMP_HIGH = (0.24, 0.33, 0.55)


def _fraction(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.5
    return min(1.0, max(0.0, (value - low) / (high - low)))


def _face_color(
    elevation: float, height: float, z_low: float, z_high: float, h_low: float, h_high: float
):
    shade = 0.6 * _fraction(elevation, z_low, z_high) + 0.4 * _fraction(height, h_low, h_high)
    return tuple(
        low + (high - low) * shade for low, high in zip(_RAMP_LOW, _RAMP_HIGH, strict=True)
    )


def plot_city_3d(document: Any, dest: Path) -> Path:
    """Write a shaded 3D PNG of every building surface to ``dest``."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    surfaces = collect_surfaces(document)
    objects = [surface for surface in surfaces if surface["rings"]]
    heights = [
        surface["measured_height"] for surface in surfaces if surface["measured_height"] is not None
    ]
    h_low, h_high = (min(heights), max(heights)) if heights else (0.0, 0.0)
    elevations = [point[2] for surface in objects for ring in surface["rings"] for point in ring]
    z_low, z_high = (min(elevations), max(elevations)) if elevations else (0.0, 0.0)

    figure = Figure(figsize=(7.4, 6.0), dpi=110)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(1, 1, 1, projection="3d")
    axes.set_facecolor("#f8fafc")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not objects:
        axes.text2D(
            0.5,
            0.5,
            "no building geometry",
            transform=axes.transAxes,
            ha="center",
            va="center",
            fontsize=14,
            color="#334155",
        )
        figure.savefig(dest, format="png")
        return dest
    faces = []
    colors = []
    for surface in sorted(objects, key=lambda entry: entry["id"]):
        height = surface["measured_height"] if surface["measured_height"] is not None else h_low
        for ring in surface["rings"]:
            faces.append(ring)
            center = sum(point[2] for point in ring) / len(ring)
            colors.append(_face_color(center, height, z_low, z_high, h_low, h_high))

    axes.add_collection3d(
        Poly3DCollection(faces, facecolors=colors, edgecolors="#1f2937", linewidths=0.35)
    )
    xs = [point[0] for ring in faces for point in ring]
    ys = [point[1] for ring in faces for point in ring]
    zs = [point[2] for ring in faces for point in ring]
    centers = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)
    spans = (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    radii = tuple(max(span / 2, 1.0) for span in spans)
    axes.set_xlim(centers[0] - radii[0], centers[0] + radii[0])
    axes.set_ylim(centers[1] - radii[1], centers[1] + radii[1])
    axes.set_zlim(centers[2] - radii[2], centers[2] + radii[2])
    axes.set_box_aspect(spans)
    axes.set_xlabel("x (m)")
    axes.set_ylabel("y (m)")
    axes.set_zlabel("z (m)")
    axes.set_title(f"CityGML buildings: {len(objects)} object(s), {len(faces)} surfaces")
    axes.view_init(elev=28, azim=-58)

    dest.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(dest, format="png")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sample", nargs="?", default=str(DEFAULT_SAMPLE), help="CityGML .gml file")
    parser.add_argument(
        "-o",
        "--output",
        default=str(OUT_DIR / "citygml-city3d.png"),
        help="output PNG path (default: examples/citygml/out/citygml-city3d.png)",
    )
    args = parser.parse_args(argv)
    sample = Path(args.sample)
    if not sample.is_file():
        print(f"missing CityGML sample: {sample}\nrun: {SETUP_HINT}", file=sys.stderr)
        return 2
    from examples.citygml.common.schemas import compile_schema

    document = compile_schema().parse(sample)
    output = plot_city_3d(document, Path(args.output))
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
