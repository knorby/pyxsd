"""Shared fixtures and helpers for the pyxsd test suite."""

import xml.etree.ElementTree as ET
from pathlib import Path

from pyxsd.parser import PyXSD

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
    """Run a full PyXSD parse against a named fixture.

    Written output is suppressed unless overridden through ``kwargs``
    (which are passed straight through to :class:`PyXSD`). Returns the
    parser object; call ``parser.parseXML()`` on it to build a fresh
    instance tree.
    """
    directory = fixture_dir(fixture)
    kwargs.setdefault("xmlFileOutput", False)
    kwargs.setdefault("transformOutputName", None)
    return PyXSD(
        str(directory / "instance.xml"),
        str(directory / "schema.xsd"),
        **kwargs,
    )
