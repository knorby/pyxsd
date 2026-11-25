"""Sphinx configuration for the pyxsd documentation."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from sphinx.domains.python import PythonDomain
from sphinx.ext import viewcode
from sphinx.util.nodes import make_refnode

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

project = "pyxsd"
copyright = "2026, the pyxsd contributors"
author = "the pyxsd contributors"

import pyxsd  # noqa: E402

#: Full version (MAJOR.MINOR.MICRO) shown in the docs header and search.
release = pyxsd.__version__
#: Short form (MAJOR.MINOR) Sphinx uses for the version banner.
version = ".".join(release.split(".")[:2])

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    # Load viewcode after AutoAPI so existing source links use Sphinx's analyzer.
    "autoapi.extension",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinx_llms_txt",
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
# The extension adds capturing groups; ours must not shift its command group.
copybutton_prompt_text = r"^(?:\$ |>>> |\.\.\. )"
copybutton_prompt_is_regexp = True
# Preserve unprompted continuation lines and indentation, including shell '\'.
copybutton_only_copy_prompt_lines = False
copybutton_exclude = ".linenos"

# --- LLM-friendly text --------------------------------------------------------
# llms.txt links to the raw MyST sources copied into _sources/; llms-full.txt
# concatenates those sources. Autodoc output is not expanded in the full file.
llms_txt_summary = (
    "Validate XML against XSD and bind documents to Python objects. Inspect "
    "structured validation reports, query and transform trees, and export data "
    "with optional Pydantic and Arrow/Parquet integrations."
)

# --- External links (scheduled/manual only, never a per-PR gate) -------------
linkcheck_timeout = 15
linkcheck_retries = 2
linkcheck_workers = 5
# ORNL returns 403 to every automated client, including a browser user agent.
linkcheck_ignore = [
    r"https://www\.ornl\.gov/csmd",
    r"https://www\.ornl\.gov/group/cmsg",
]

# --- HTML theme -------------------------------------------------------------
html_theme = "furo"
html_title = "pyxsd: XML Schema validation and Python bindings"
html_short_title = "pyxsd"
html_baseurl = "https://pyxsd.knorby.com/"
html_static_path = ["_static"]
html_css_files = ["docs.css"]
html_context = {"docs_channel": "develop"}
# Serve the custom domain from the GitHub Pages artifact.
html_extra_path = ["CNAME"]

# --- Intersphinx ------------------------------------------------------------
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# --- Autodoc ---------------------------------------------------------------
autodoc_member_order = "bysource"
autodoc_typehints = "description"

# Scan implementation modules so AutoAPI can resolve facade re-exports.
# Publish only supported import paths, not the entire implementation tree.
autoapi_dirs = ["../src/pyxsd"]
autoapi_python_use_implicit_namespaces = True
autoapi_add_toctree_entry = False
autoapi_template_dir = "_templates/autoapi"
# These optional adapters are not needed to resolve the public facades.
autoapi_ignore = ["*/pyxsd/mcp/*", "*/pyxsd/relaxng/pydantic_adapter.py"]
autoapi_options = ["members", "undoc-members", "imported-members", "show-module-summary"]
PUBLIC_API_MODULES = frozenset(
    {
        "pyxsd",
        "pyxsd.batch",
        "pyxsd.integrations",
        "pyxsd.introspection",
        "pyxsd.relaxng",
        "pyxsd.resource_policy",
        "pyxsd.schemaless",
        "pyxsd.stubs",
        "pyxsd.transforms",
        "pyxsd.validation",
        "pyxsd.xdm",
        "pyxsd.xpath",
        "pyxsd.xslt",
    }
)


def skip_api_member(app, what, name, obj, skip, options):
    if what in {"module", "package"} and name not in PUBLIC_API_MODULES:
        return True
    return None


def is_generated_api(docname):
    return docname.startswith("autoapi/")


class ReferencePythonDomain(PythonDomain):
    """Keep both pages' anchors, but prefer hand-authored cross-reference targets."""

    def note_object(self, name, objtype, node_id, aliased=False, location=None):
        generated = is_generated_api(self.env.current_document.docname)
        if previous := self.objects.get(name):
            was_generated = is_generated_api(previous.docname)
            if generated and not was_generated:
                return
            if was_generated and not generated:
                del self.objects[name]
        # Autodoc's short annotation names would otherwise match both public
        # paths for these classes, which the guide already documents by module.
        if generated and name in {"pyxsd.XmlLimits", "pyxsd.XmlParserPolicy"}:
            aliased = True
        super().note_object(name, objtype, node_id, aliased, location)

    def note_module(self, name, node_id, synopsis, platform, deprecated):
        previous = self.modules.get(name)
        if not (
            is_generated_api(self.env.current_document.docname)
            and previous
            and not is_generated_api(previous.docname)
        ):
            super().note_module(name, node_id, synopsis, platform, deprecated)


class StaticImportFilter(logging.Filter):
    """AutoAPI follows type-only imports through the internal XSD registry."""

    def filter(self, record):
        return not (
            getattr(record, "subtype", None) == "python_import_resolution"
            and record.getMessage().startswith(
                "Cannot resolve cyclic import: pyxsd.schema_checks, "
                "pyxsd.element_representatives.element_representative, "
            )
        )


def prepare_api_templates(env):
    # AutoAPI cannot see imports guarded by TYPE_CHECKING. Autodoc can
    # resolve these exports directly through the package's lazy __getattr__.
    env.globals["lazy_exports"] = [
        (name, "class" if isinstance(getattr(pyxsd, name), type) else "function")
        for name in pyxsd.__all__
        if not name.startswith("_")
    ]


autoapi_prepare_jinja_env = prepare_api_templates


def resolve_generated_type(app, env, node, content):
    """Link implementation names to their public re-exports where possible."""
    if node.get("refdomain") != "py":
        return None

    docname = node["refdoc"]
    target = node["reftarget"]
    domain = env.get_domain("py")
    parent, _, member = target.rpartition(".")
    for symbol in getattr(env, "autoapi_all_objects", {}).values():
        origin = symbol.obj.get("original_path")
        if origin and origin in (target, parent):
            public_name = symbol.id if origin == target else f"{symbol.id}.{member}"
            if entry := domain.objects.get(public_name):
                return make_refnode(app.builder, docname, entry.docname, entry.node_id, content)

    # AutoAPI signatures also describe types that have no public reference
    # page. Keep inferred annotations readable; explicit roles stay checked.
    if is_generated_api(docname) and (
        "refexplicit" not in node or (node.get("reftype") == "class" and not node.get("refwarn"))
    ):
        return content
    return None


def isolate_api_annotations(app, docname, source):
    # AutoAPI's type fields must not replace autodoc's on the existing pages.
    if not is_generated_api(docname):
        app.env.temp_data.pop("annotations", None)


def reference_viewcode(app, doctree):
    # Viewcode builds backlinks using defining modules, not the public
    # aliases used by AutoAPI. Keep source links on the existing pages.
    if not is_generated_api(app.env.current_document.docname):
        viewcode.doctree_read(app, doctree)


def finish_api_build(app, exception):
    logging.getLogger("sphinx.autoapi._mapper").removeFilter(app._static_import_filter)


def setup(app):
    app.add_domain(ReferencePythonDomain, override=True)
    for listener in app.events.listeners["doctree-read"]:
        if listener.handler is viewcode.doctree_read:
            app.disconnect(listener.id)
            app.connect("doctree-read", reference_viewcode, priority=listener.priority)
            break
    app.connect("autoapi-skip-member", skip_api_member)
    app.connect("source-read", isolate_api_annotations, priority=600)
    app.connect("missing-reference", resolve_generated_type)
    app._static_import_filter = StaticImportFilter()
    logging.getLogger("sphinx.autoapi._mapper").addFilter(app._static_import_filter)
    app.connect("build-finished", finish_api_build)


# Strict cross-references: repair public targets in the docs, not broad ignores.
nitpicky = True
nitpick_ignore = [
    ("py:class", "pyxsd.schema_context.SchemaContext"),  # Internal compilation state.
    ("py:class", "pyxsd.namespaces.NamespaceContext"),  # Internal parser namespace state.
    ("py:class", "ParseProvenance"),  # Internal input provenance, not a caller API.
    ("py:class", "DocumentDeclarations"),  # Internal retained DTD evidence.
    ("py:class", "AddressResolver"),  # Callable type alias, not a class.
    ("py:class", "Path"),  # Short annotation spelling of pathlib.Path.
    ("py:class", "ET.Element"),  # Short annotation spelling of ElementTree.Element.
    ("py:class", "The merged report"),  # Napoleon misreads the report property's prose as a type.
    ("py:class", "pyxsd.extensions.\nExtensions"),  # Wrapped role in the library docstring.
    ("py:class", "pyxsd.xml_policy.\nXmlParserPolicy"),  # Wrapped role in the library docstring.
    ("py:class", "pyxsd.xml_policy.\nXmlLimits"),  # Wrapped role in the library docstring.
    ("py:func", "_render_compact"),  # Private stub-rendering implementation.
    ("py:attr", "outcome"),  # Instance attribute described on BatchParseError, not class metadata.
    ("py:data", "ResourcePurpose"),  # Literal type alias, not a documented data object.
]
