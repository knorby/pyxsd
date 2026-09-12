"""Parser isolation tests.

Two live PyXSD parsers must not corrupt each other's schema context:
namespace overrides are a snapshot per parser, and re-parsing a tree
re-activates the parser's own component table.
"""

import pyxsd.element_representatives.element_representative as ermod
from pyxsd.binding import ParseModes
from pyxsd.element_representatives.element_representative import (
    set_active_namespace_overrides,
)
from pyxsd.parser import PyXSD

SIMPLE_SCHEMA = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="b" type="xs:string"/></xs:schema>'
)
SIMPLE_INSTANCE = "<b>hello</b>"


def _parse_simple(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "bs.xsd").write_text(SIMPLE_SCHEMA)
    (tmp_path / "bi.xml").write_text(SIMPLE_INSTANCE)
    return PyXSD(
        tmp_path / "bi.xml",
        xsdFile=tmp_path / "bs.xsd",
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
    )


def _namespaces_of(parser):
    return {
        name: entry.getNamespace()
        for name, entries in sorted(parser.components.items())
        for entry in entries
    }


def test_override_install_is_a_snapshot():
    """Installing overrides copies them; later installs rebind."""
    overrides = {1: "urn:a"}
    set_active_namespace_overrides(overrides)
    installed = ermod._ACTIVE_NAMESPACE_OVERRIDES
    assert installed == {1: "urn:a"}

    # Mutating the caller's dict afterwards must not leak into the
    # installed map.
    overrides[2] = "urn:b"
    assert installed == {1: "urn:a"}

    # Installing again rebinds to a new map instead of clearing the
    # previous one in place.
    set_active_namespace_overrides({3: "urn:c"})
    assert ermod._ACTIVE_NAMESPACE_OVERRIDES == {3: "urn:c"}
    assert installed == {1: "urn:a"}


def test_later_parser_cannot_corrupt_earlier_component_namespaces(tmp_path):
    """A second parser's schema run must not change the first parser's
    spliced components' namespaces."""
    from conftest import run_parser

    first = run_parser("compose", mode=ParseModes.NAMESPACED)
    before = _namespaces_of(first)

    _parse_simple(tmp_path / "b")

    assert _namespaces_of(first) == before


def test_parsexml_reactivates_parser_context(tmp_path):
    """Re-parsing an earlier parser re-activates its component table."""
    from conftest import run_parser

    first = run_parser("compose", mode=ParseModes.NAMESPACED)
    _parse_simple(tmp_path / "b")
    assert ermod._ACTIVE_TABLE is not first.components

    first.parseXML()

    assert ermod._ACTIVE_TABLE is first.components
