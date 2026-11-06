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
    "sphinx_copybutton",
]

templates_path = ["_templates"]
# superpowers/ holds untracked local working notes; excluding it keeps
# local -W builds clean when the directory is present.
# generated/ contains include-only fragments, not standalone source documents.
exclude_patterns = ["_build", "superpowers/**", "generated/**", "_templates", "_static"]

# --- MyST -----------------------------------------------------------------
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "tasklist",
]
myst_heading_anchors = 3

# --- Copy buttons ------------------------------------------------------------
# MyST puts no-copy on the outer wrapper, not the inner highlight div.
copybutton_selector = "div:not(.no-copy) > div.highlight > pre"
copybutton_prompt_text = r"^(\$ |>>> |\.\.\. )"
copybutton_prompt_is_regexp = True
# Preserve unprompted continuation lines and indentation, including shell '\'.
copybutton_only_copy_prompt_lines = False
copybutton_exclude = ".linenos"

# --- HTML theme -------------------------------------------------------------
html_theme = "furo"
html_title = "pyxsd: XML Schema validation and Python bindings"
html_short_title = "pyxsd"
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
