"""Plot elevation and superelevation profiles from an OpenDRIVE document.

OpenDRIVE stores vertical alignment as cubic polynomials in ``s`` on
``elevationProfile/elevation``; ``lateralProfile/superelevation`` uses
the same piecewise-cubic shape for the road's bank angle. Both restart
at ``s = 0`` for every road, so the plot draws one line per road rather
than pretending a route is continuous.

Matplotlib is imported inside :func:`plot_profiles` so the rest of the
OpenDRIVE workflow (and the FastAPI showcase) works with only pyxsd
installed; the example-only dependency group is
``uv sync --group examples-matplotlib``.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.opendrive.common.schemas import compile_schema
from examples.opendrive.render_map import (
    _as_list,
    _node,
    _number,
    _piecewise_poly,
    _schema_variant,
    _value,
)

_COLORS = (
    "#2563eb",
    "#dc2626",
    "#16a34a",
    "#9333ea",
    "#ea580c",
    "#0891b2",
    "#ca8a04",
    "#be185d",
)


def _sample_s(length: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("step must be positive")
    count = max(1, math.ceil(length / step))
    values = [index * length / count for index in range(count)]
    values.append(length)
    return values


def _road_id(road: dict) -> str:
    value = _value(road, "id")
    return "" if value is None else str(value)


def profile_samples(document, *, step: float = 1.0) -> list[dict]:
    """Return per-road profile samples for roads that carry a profile.

    Each entry is ``{"road_id", "elevation": [(s, z), ...],
    "superelevation": [(s, rate), ...]}``. A road is included when it
    has at least one of the two profiles; missing profiles stay empty.
    """
    data = document.to_dict()
    result = []
    for road in _as_list(data.get("road")):
        if not isinstance(road, dict):
            continue
        elevation_records = _as_list(_node(road, "elevationProfile").get("elevation"))
        superelevation_records = _as_list(_node(road, "lateralProfile").get("superelevation"))
        if not elevation_records and not superelevation_records:
            continue
        start = 0.0
        stop = _number(road, "length")
        if stop <= 0:
            stops = [_number(record, "s") for record in elevation_records + superelevation_records]
            stop = max(stops, default=0.0)
        s_values = _sample_s(stop, step) if stop > start else [start]
        result.append(
            {
                "road_id": _road_id(road),
                "elevation": [(s, _piecewise_poly(elevation_records, s)) for s in s_values]
                if elevation_records
                else [],
                "superelevation": [
                    (s, _piecewise_poly(superelevation_records, s)) for s in s_values
                ]
                if superelevation_records
                else [],
            }
        )
    return result


def plot_profiles(document, dest: Path) -> Path:
    """Write elevation (and, when present, superelevation) profiles as PNG."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    series = profile_samples(document)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    has_superelevation = any(item["superelevation"] for item in series)
    rows = 2 if has_superelevation else 1
    figure, axes = plt.subplots(rows, 1, figsize=(10, 4 * rows + 1), squeeze=False)
    elevation_axis = axes[0][0]
    labelled = len(series) <= 8

    for index, item in enumerate(series):
        if not item["elevation"]:
            continue
        s_values = [sample[0] for sample in item["elevation"]]
        z_values = [sample[1] for sample in item["elevation"]]
        color = _COLORS[index % len(_COLORS)]
        elevation_axis.plot(
            s_values,
            z_values,
            color=color,
            linewidth=1.2,
            label=f"road {item['road_id']}" if labelled else None,
        )

    elevation_axis.set_title("Elevation profile")
    elevation_axis.set_xlabel("s (m)")
    elevation_axis.set_ylabel("z (m)")
    elevation_axis.grid(True, color="#e2e8f0", linewidth=0.6)
    if not any(item["elevation"] for item in series):
        elevation_axis.text(
            0.5,
            0.5,
            "No elevation profile in this document",
            transform=elevation_axis.transAxes,
            ha="center",
            va="center",
            color="#64748b",
        )
    if labelled and any(item["elevation"] for item in series):
        elevation_axis.legend(fontsize="small", frameon=False)

    if has_superelevation:
        superelevation_axis = axes[1][0]
        for index, item in enumerate(series):
            if not item["superelevation"]:
                continue
            s_values = [sample[0] for sample in item["superelevation"]]
            rates = [sample[1] for sample in item["superelevation"]]
            superelevation_axis.plot(
                s_values,
                rates,
                color=_COLORS[index % len(_COLORS)],
                linewidth=1.2,
            )
        superelevation_axis.set_title("Superelevation")
        superelevation_axis.set_xlabel("s (m)")
        superelevation_axis.set_ylabel("rate (rad)")
        superelevation_axis.grid(True, color="#e2e8f0", linewidth=0.6)

    figure.tight_layout()
    figure.savefig(dest, dpi=120)
    plt.close(figure)
    return dest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Plot OpenDRIVE elevation profiles as PNG.")
    parser.add_argument("document", type=Path, help="path to an .xodr file")
    parser.add_argument("-o", "--output", type=Path, help="PNG destination (default: ../out/)")
    args = parser.parse_args(argv)

    schema = compile_schema(_schema_variant(args.document))
    document = schema.parse(args.document)
    output = args.output or Path(__file__).resolve().parents[1] / "out" / (
        f"{args.document.stem}-profiles.png"
    )
    plot_profiles(document, output)
    print(f"wrote {output}")
    if document.report.has_errors:
        print(
            f"{document.report.error_count} validation error(s) in {args.document}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
