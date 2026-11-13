"""The XSLT override of ``fn:serialize``.

The function shares the serializer's parameters and error codes with
stylesheet delivery: an explicit call and the stylesheet's own output use the
same method rules, character maps, and ``SERE*``/``SEPM*`` codes.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from elementpath.xpath2 import XPath2Parser
from elementpath.xpath3 import XPath31Parser
from tests.xslt.support import stylesheet

from pyxsd import xsd_data_types as _xsd
from pyxsd.xdm import XdmAtomic, XdmMap
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xslt.errors import XsltDynamicError, XsltSerializationError
from pyxsd.xslt.processor import XsltProcessor
from pyxsd.xslt.runtime.functions import XsltContextBridge
from pyxsd.xslt.runtime.transformation import Transformation

FN_NS = "http://www.w3.org/2005/xpath-functions"
XSD_NS = "http://www.w3.org/2001/XMLSchema"


def _transform(body: str, *, attributes: str = "", **options: Any):
    source = stylesheet(body, attributes=attributes)
    compiled = XsltProcessor().compile(io.BytesIO(source))
    return compiled.transform(delivery="raw", initial_template="main", **options)


def _expression(expression: str, *, attributes: str = ""):
    body = f'<xsl:template name="main"><xsl:sequence select="{expression}"/></xsl:template>'
    return _transform(body, attributes=attributes)


def _error_code(action: Any) -> str | None:
    with pytest.raises((XsltSerializationError, XsltDynamicError)) as error:
        action()
    return error.value.code


# ---------------------------------------------------------------------------
# Parameter maps


def test_serialize_with_a_parameter_map() -> None:
    result = _expression("serialize(map{'a': 1}, map{'method': 'json'})")

    assert result.principal == (XdmAtomic.string('{"a":1}'),)
    assert result.principal[0].type_name == "{http://www.w3.org/2001/XMLSchema}string"


def test_serialize_uses_the_unnamed_output_without_parameters() -> None:
    body = (
        '<xsl:output method="text" item-separator="|"/>'
        '<xsl:template name="main">'
        "<xsl:sequence select=\"serialize(('a', 'b'))\"/></xsl:template>"
    )
    result = _transform(body)

    assert result.principal == (XdmAtomic.string("a|b"),)


def test_serialize_uses_the_unnamed_output_character_maps_without_parameters() -> None:
    body = (
        '<xsl:output method="xml" omit-xml-declaration="yes" use-character-maps="m"/>'
        '<xsl:character-map name="m"><xsl:output-character character="x" string="y"/>'
        "</xsl:character-map>"
        '<xsl:variable name="n"><r>x</r></xsl:variable>'
        '<xsl:template name="main"><xsl:sequence select="serialize($n)"/></xsl:template>'
    )
    result = _transform(body, attributes=' exclude-result-prefixes="xs"')

    assert result.principal == (XdmAtomic.string("<r>y</r>"),)


def test_serialize_accepts_qname_keys_by_local_name() -> None:
    body = (
        '<xsl:variable name="n"><r>x</r></xsl:variable>'
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        "map{QName('urn:example', 'method'): 'xml', "
        "QName('urn:example', 'omit-xml-declaration'): true()})\"/></xsl:template>"
    )
    result = _transform(body, attributes=' exclude-result-prefixes="xs"')

    assert result.principal == (XdmAtomic.string("<r>x</r>"),)


def test_serialize_converts_boolean_and_numeric_values() -> None:
    body = (
        '<xsl:variable name="n"><r>x</r></xsl:variable>'
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        "map{'method': 'xml', 'omit-xml-declaration': 1, 'indent': 0})\"/></xsl:template>"
    )
    result = _transform(body, attributes=' exclude-result-prefixes="xs"')

    assert result.principal == (XdmAtomic.string("<r>x</r>"),)


def test_serialize_accepts_a_character_map_value() -> None:
    body = (
        '<xsl:variable name="n"><r>x</r></xsl:variable>'
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        "map{'method': 'xml', 'omit-xml-declaration': true(), "
        "'use-character-maps': map{'x': 'y'}})\"/></xsl:template>"
    )
    result = _transform(body, attributes=' exclude-result-prefixes="xs"')

    assert result.principal == (XdmAtomic.string("<r>y</r>"),)


def test_serialize_supports_json_and_adaptive_methods() -> None:
    assert _expression("serialize(map{'a': [1, 2]}, map{'method': 'json'})").principal == (
        XdmAtomic.string('{"a":[1,2]}'),
    )
    assert _expression("serialize(('x', 1), map{'method': 'adaptive'})").principal == (
        XdmAtomic.string('"x"\n1'),
    )


# ---------------------------------------------------------------------------
# Errors


def test_serialize_rejects_unknown_parameters_with_sepm0016() -> None:
    assert (
        _error_code(lambda: _expression("serialize('x', map{'method': 'json', 'unknown': 'x'})"))
        == "SEPM0016"
    )
    assert _error_code(lambda: _expression("serialize('x', map{'method': 'yaml'})")) == "SEPM0016"


def test_serialize_keeps_serialization_codes() -> None:
    assert (
        _error_code(lambda: _expression("serialize(exists#1, map{'method': 'json'})")) == "SERE0021"
    )
    assert (
        _error_code(lambda: _expression("serialize(('a', 'b'), map{'method': 'json'})"))
        == "SERE0023"
    )


def test_serialize_rejects_parameter_values_that_are_not_attributes() -> None:
    body = (
        '<xsl:variable name="n"><r/></xsl:variable>'
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        "map{'method': (1, 2)})\"/></xsl:template>"
    )
    assert _error_code(lambda: _transform(body)) == "SEPM0016"

    body = (
        '<xsl:variable name="n"><r/></xsl:variable>'
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        "map{'method': $n})\"/></xsl:template>"
    )
    assert _error_code(lambda: _transform(body)) == "SEPM0016"


def test_serialize_character_map_parameter_values_are_validated() -> None:
    assert _error_code(lambda: _expression("serialize('x', map{'use-character-maps': 'm'})")) == (
        "SEPM0016"
    )
    assert (
        _error_code(
            lambda: _expression("serialize('x', map{'use-character-maps': map{'a': (1, 2)}})")
        )
        == "SEPM0016"
    )
    assert _expression(
        "serialize('x', map{'method': 'text', 'use-character-maps': ()})"
    ).principal == (XdmAtomic.string("x"),)
    assert _expression("serialize('x', ())").principal == (
        XdmAtomic.string('<?xml version="1.0" encoding="UTF-8"?>x'),
    )


def test_serialize_reads_a_qname_key_from_every_qname_representation() -> None:
    bridge = XsltContextBridge(Transformation())
    serialize = bridge.function_overrides()[f"{{{FN_NS}}}serialize"]
    value = (XdmAtomic.string("x"),)

    with _xsd.qname_context({"p": "urn:example"}):
        typed = _xsd.QName("p:method")
    typed_key = XdmAtomic.typed(typed, _xsd.QName)
    params = (XdmMap([(typed_key, XdmAtomic.string("json"))]),)

    assert serialize.invoke(None, (value, params)) == (XdmAtomic.string('"x"'),)

    bare = XdmAtomic("method", f"{{{XSD_NS}}}QName", None)
    params = (XdmMap([(bare, XdmAtomic.string("json"))]),)
    assert serialize.invoke(None, (value, params)) == (XdmAtomic.string('"x"'),)


def test_serialize_rejects_a_non_map_parameter_argument() -> None:
    assert _error_code(lambda: _expression("serialize('x', 'json')")) == "XPTY0004"
    assert _error_code(lambda: _expression("serialize('x', ('a', 'b'))")) == "XPTY0004"


# ---------------------------------------------------------------------------
# Profile availability


def test_serialize_is_unavailable_to_the_xpath_two_parser() -> None:
    assert XPath2Parser.symbol_table.get("serialize") is None
    assert XPath31Parser.symbol_table.get("serialize") is not None

    bridge = XsltContextBridge(Transformation(), xpath_version="2.0")
    overrides = bridge.function_overrides()
    assert f"{{{FN_NS}}}serialize" in overrides

    with pytest.raises(Exception, match="XPST0017"):
        XPathEngine("2.0").compile("serialize(())")


def test_serialize_override_is_usable_through_a_three_one_engine() -> None:
    bridge = XsltContextBridge(Transformation())
    overrides = bridge.function_overrides()
    engine = XPathEngine("3.1")
    compiled = engine.compile("serialize(map{'a': 1}, map{'method': 'json'})")

    assert engine.evaluate(compiled, ExpressionEnvironment(function_overrides=overrides)) == (
        XdmAtomic.string('{"a":1}'),
    )
