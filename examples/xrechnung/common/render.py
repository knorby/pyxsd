"""Tiny stdlib-only text helpers for the invoice renderers."""

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
