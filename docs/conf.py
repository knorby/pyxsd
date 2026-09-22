"""Sphinx configuration for the pyxsd documentation."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

project = "pyxsd"
copyright = "2026, the pyxsd contributors"
author = "the pyxsd contributors"

from pyxsd import __version__  # noqa: E402

#: Full version (MAJOR.MINOR.MICRO) shown in the docs header and search.
release = __version__
#: Short form (MAJOR.MINOR) Sphinx uses for the version banner.
version = ".".join(release.split(".")[:2])

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
]

templates_path = ["_templates"]
# superpowers/ holds untracked local working notes; excluding it keeps
# local -W builds clean when the directory is present.
exclude_patterns = ["_build", "superpowers/**", "_templates", "_static"]

# --- MyST -----------------------------------------------------------------
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "tasklist",
]
myst_heading_anchors = 3

# --- HTML theme -------------------------------------------------------------
html_theme = "furo"
html_title = "pyxsd"
html_baseurl = "https://pyxsd.knorby.com/"
html_static_path = ["_static"]
# Serve the custom domain from the GitHub Pages artifact.
html_extra_path = ["CNAME"]

# --- Intersphinx ------------------------------------------------------------
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# --- Autodoc ---------------------------------------------------------------
autodoc_member_order = "bysource"
autodoc_typehints = "description"
