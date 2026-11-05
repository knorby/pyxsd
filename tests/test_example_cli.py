"""Smoke-test documented CLI entry points without relying on test imports."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RENDERER_CLIS = (
    "examples/bpmn/render_svg.py",
    "examples/citygml/plan_svg.py",
    "examples/citygml/plot_city3d.py",
    "examples/musicxml/note_stats.py",
    "examples/musicxml/render_preview.py",
    "examples/musicxml/synthesize.py",
    "examples/opendrive/plot_profiles.py",
    "examples/opendrive/render_map.py",
    "examples/xrechnung/render_html.py",
    "examples/xrechnung/render_pdf.py",
)


@pytest.mark.parametrize("relative", RENDERER_CLIS)
def test_renderer_cli_help_runs_from_repo_root(relative: str):
    if relative == "examples/xrechnung/render_pdf.py":
        pytest.importorskip("reportlab")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, relative, "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout.lower()
