"""Tiny stdlib-only SVG helpers shared by the CityGML renderers.

The renderers build SVG as text: no XML library is needed for output, and
keeping it dependency-free means the base-install examples (BPMN, CityGML
plan view, OpenDRIVE map, score preview) run with only pyxsd installed.
"""

from __future__ import annotations


def esc(text: object) -> str:
    """Escape text for XML character data and attribute values."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def num(value: float, digits: int = 2) -> str:
    """Format a coordinate compactly: integral values drop the decimal."""
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def svg(body: str, *, width: int, height: int, view_box: str | None = None) -> str:
    """Wrap rendered elements in a standalone ``<svg>`` root."""
    box = view_box if view_box is not None else f"0 0 {width} {height}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="{box}">{body}</svg>'
    )
