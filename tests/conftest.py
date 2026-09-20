"""Shared fixtures and helpers for the pyxsd test suite."""

import xml.etree.ElementTree as ET
from pathlib import Path

# Imported eagerly (not through the lazy ``pyxsd`` __getattr__): this
# loads the element-representative stack in its safe order for every
# test module, mirroring what the old ``pyxsd.parser`` import did.
from pyxsd.schema import Schema

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: Every fixture corpus shipped with the test suite. Each entry is a
#: directory containing a ``schema.xsd`` and an ``instance.xml`` that is
#: valid against that schema.
ALL_FIXTURES = [
    "inventory",
    "primitives",
    "choice",
    "nested",
    "datatypes",
    "all",
    "groups",
    "unions",
    "wildcards",
    "defaults",
    "nillable",
    "substitution",
    "xsi_type",
    "compose",
    "identity",
]


def fixture_dir(name):
    """Return the path to a named fixture directory."""
    return FIXTURES_DIR / name


def canonicalize(element):
    """Reduce an ElementTree element to a comparable structure.

    Comments are skipped (ElementTree drops them when parsing unless a
    custom parser is installed, but be defensive), and attributes are
    sorted. The text of a leaf element is kept exactly as parsed -- the
    writer must reproduce it verbatim -- while the whitespace between a
    parent element's tags is treated as layout and stripped.
    """
    children = [canonicalize(child) for child in element]
    text = (element.text or "").strip() if children else element.text or ""
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        text,
        children,
    )


def canonical_parse(source):
    """Parse an XML file (path or file object) into canonical form."""
    return canonicalize(ET.parse(source).getroot())


def assert_xml_canonically_equal(source_a, source_b):
    """Assert two XML documents are equal after canonicalization."""
    canon_a = canonical_parse(source_a)
    canon_b = canonical_parse(source_b)
    assert canon_a == canon_b


def run_parser(fixture, **kwargs):
    """Compile a fixture's schema and bind its instance document.

    Returns the :class:`pyxsd.document.Document`; call ``document.root``
    for the bound tree and ``document.report`` for the merged report.
    Extra keyword arguments are forwarded to ``Schema.compile``
    (``mode``, ``namespace_schemas``, ...). Pass
    ``xmlFileOutput=<path>`` to also write the bound tree out.
    """
    directory = fixture_dir(fixture)
    output = kwargs.pop("xmlFileOutput", False)
    schema = Schema.compile(str(directory / "schema.xsd"), **kwargs)
    document = schema.parse(str(directory / "instance.xml"))
    if output:
        document.write(output)
    return document
