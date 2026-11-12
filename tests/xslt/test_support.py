"""Focused support behavior for shared XSLT helpers.

These tests pin the contracts that the consolidation relies on: import
precedence ordering, namespace-name splitting, error-code extraction, XDM
numeric helpers, and the shared test-support helpers themselves.
"""

from __future__ import annotations

import math
from decimal import Decimal

from tests.xslt.support import element_children, stylesheet, transform

from pyxsd.namespaces import local_name, namespace_of
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xdm.values import _as_float, _as_float32, _is_nan
from pyxsd.xslt.errors import error_code_from_exception
from pyxsd.xslt.precedence import compare_precedence


def test_compare_precedence_pins_equal_prefix_and_later_import_order() -> None:
    assert compare_precedence((), ()) == 0
    assert compare_precedence((1, 2), (1, 2)) == 0
    assert compare_precedence((), (1,)) > 0
    assert compare_precedence((1,), ()) < 0
    assert compare_precedence((1,), (1, 2)) > 0
    assert compare_precedence((1, 2), (1,)) < 0
    assert compare_precedence((1, 2), (1, 3)) < 0
    assert compare_precedence((1, 3), (1, 2)) > 0
    assert compare_precedence((2,), (1,)) > 0
    assert compare_precedence((1,), (2,)) < 0


def test_namespace_utilities_split_expanded_and_plain_names() -> None:
    assert local_name("{urn:example}a") == "a"
    assert local_name("a") == "a"
    assert namespace_of("{urn:example}a") == "urn:example"
    assert namespace_of("a") is None
    # Malformed Clark names stay unchanged / namespace-less instead of raising.
    assert local_name("{unterminated") == "{unterminated"
    assert namespace_of("{unterminated") is None


def test_error_code_from_exception_uses_default_and_preserves_custom_names() -> None:
    class StandardCode(Exception):
        code = "err:XPST0017"

    class UnprefixedCode(Exception):
        code = "XTDE0420"

    class CustomCode(Exception):
        code = "Q{urn:example}EXT0001"

    class OtherPrefix(Exception):
        code = "other:CODE"

    assert error_code_from_exception(StandardCode(), "XTSE0010") == "XPST0017"
    assert error_code_from_exception(UnprefixedCode(), "XTSE0010") == "XTDE0420"
    assert error_code_from_exception(CustomCode(), "XTSE0010") == "{urn:example}EXT0001"
    assert error_code_from_exception(OtherPrefix(), "XTSE0010") == "other:CODE"
    assert error_code_from_exception(RuntimeError("[err:XTSE0010] bad")) == "XTSE0010"
    assert error_code_from_exception(RuntimeError("plain"), "XTSE0010") == "XTSE0010"
    assert error_code_from_exception(RuntimeError("plain")) is None


def test_numeric_helpers_cover_nan_float32_and_coercion_fallbacks() -> None:
    assert _is_nan(float("nan"))
    assert _is_nan(Decimal("NaN"))
    assert not _is_nan(1)
    assert not _is_nan(Decimal("1"))

    assert _as_float32(1) == 1.0
    assert math.isinf(_as_float32(10**300))
    assert math.isinf(_as_float(10**400))
    assert math.isnan(_as_float("not-a-number"))


def test_shared_stylesheet_helper_declares_namespaces_and_attributes() -> None:
    assert stylesheet("<body/>") == (
        b'<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        b'xmlns:xs="http://www.w3.org/2001/XMLSchema" version="3.0">'
        b"<body/></xsl:stylesheet>"
    )
    assert stylesheet(
        "<body/>",
        version="2.0",
        attributes=' expand-text="yes"',
        namespaces={"my": "urn:my", "app": "urn:app"},
    ) == (
        b'<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        b'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        b'xmlns:my="urn:my" xmlns:app="urn:app" version="2.0"'
        b' expand-text="yes"><body/></xsl:stylesheet>'
    )


def test_shared_transform_and_element_children_helpers() -> None:
    body = '<xsl:template name="main"><out>text<a/><b/></out></xsl:template>'
    result = transform(stylesheet(body), initial_template="main")
    root = result.principal_tree.root_element
    children = element_children(root)
    assert tuple(child.local_name for child in children) == ("a", "b")
    assert all(child.kind is NodeKind.ELEMENT for child in children)

    source = (
        '<xsl:template name="main"><out><xsl:value-of select="count(doc/n)"/></out></xsl:template>'
    )
    result = transform(
        stylesheet(source),
        b"<doc><n/><n/><n/></doc>",
        initial_template="main",
    )
    assert result.principal_tree.root_element.string_value == "3"
