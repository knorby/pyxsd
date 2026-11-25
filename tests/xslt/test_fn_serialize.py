"""The XSLT override of ``fn:serialize``.

The function shares the serializer's parameters and error codes with
stylesheet delivery.  Parameter maps follow the F&O 3.1 option conventions:
fixed table defaults (including ``omit-xml-declaration = yes``), empty values
select the default, wrong types are ``XPTY0004``, invalid values are
``SEPM0016``, and the 3.0 ``output:serialization-parameters`` element form is
also accepted.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from elementpath.xpath2 import XPath2Parser
from elementpath.xpath3 import XPath31Parser
from tests.xslt.support import stylesheet

from pyxsd import xsd_data_types as xsd
from pyxsd.xdm import XdmAtomic, XdmMap
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xslt.errors import XsltDynamicError, XsltSerializationError
from pyxsd.xslt.processor import XsltProcessor
from pyxsd.xslt.runtime.functions import XsltContextBridge
from pyxsd.xslt.runtime.transformation import Transformation

FN_NS = "http://www.w3.org/2005/xpath-functions"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
SERIALIZATION_NS = "http://www.w3.org/2010/xslt-xquery-serialization"

_NODE = '<xsl:variable name="n"><r>x</r></xsl:variable>'


def _transform(body: str, *, attributes: str = "", **options: Any):
    source = stylesheet(body, attributes=attributes)
    compiled = XsltProcessor().compile(io.BytesIO(source))
    return compiled.transform(delivery="raw", initial_template="main", **options)


def _expression(expression: str, *, attributes: str = ""):
    body = f'<xsl:template name="main"><xsl:sequence select="{expression}"/></xsl:template>'
    return _transform(body, attributes=attributes)


def _serialize_node(parameters: str, *, attributes: str = ' exclude-result-prefixes="xs"'):
    body = _NODE + (
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        f'{parameters})"/></xsl:template>'
    )
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


def test_serialize_map_form_defaults_omit_the_declaration() -> None:
    single = _NODE + (
        '<xsl:template name="main"><xsl:sequence select="serialize($n)"/></xsl:template>'
    )
    assert _transform(single, attributes=' exclude-result-prefixes="xs"').principal == (
        XdmAtomic.string("<r>x</r>"),
    )

    declared = (
        '<xsl:output method="xml" omit-xml-declaration="no"/>'
        + _NODE
        + '<xsl:template name="main"><xsl:sequence select="serialize($n)"/></xsl:template>'
    )
    assert _transform(declared, attributes=' exclude-result-prefixes="xs"').principal == (
        XdmAtomic.string('<?xml version="1.0" encoding="UTF-8"?><r>x</r>'),
    )

    assert _serialize_node("()").principal == (XdmAtomic.string("<r>x</r>"),)
    assert _serialize_node("map{}").principal == (XdmAtomic.string("<r>x</r>"),)
    assert _serialize_node("map{'omit-xml-declaration': false()}").principal == (
        XdmAtomic.string('<?xml version="1.0" encoding="UTF-8"?><r>x</r>'),
    )
    assert _serialize_node("map{'method': 'xml', 'omit-xml-declaration': true()}").principal == (
        XdmAtomic.string("<r>x</r>"),
    )


def test_serialize_without_parameters_uses_the_unnamed_output() -> None:
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


def test_serialize_converts_boolean_values() -> None:
    body = _NODE + (
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        "map{'method': 'xml', 'omit-xml-declaration': false()})\"/></xsl:template>"
    )

    assert _transform(body, attributes=' exclude-result-prefixes="xs"').principal == (
        XdmAtomic.string('<?xml version="1.0" encoding="UTF-8"?><r>x</r>'),
    )


def test_serialize_accepts_a_character_map_value() -> None:
    body = _NODE + (
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        "map{'method': 'xml', 'omit-xml-declaration': true(), "
        "'use-character-maps': map{'x': 'y'}})\"/></xsl:template>"
    )
    result = _transform(body, attributes=' exclude-result-prefixes="xs"')

    assert result.principal == (XdmAtomic.string("<r>y</r>"),)


def test_serialize_empty_values_select_the_parameter_default() -> None:
    assert _serialize_node(
        "map{'method': (), 'omit-xml-declaration': (), 'indent': ()}"
    ).principal == (XdmAtomic.string("<r>x</r>"),)
    assert _expression(
        "serialize('x', map{'method': 'text', 'use-character-maps': ()})"
    ).principal == (XdmAtomic.string("x"),)


def test_serialize_supports_json_and_adaptive_methods() -> None:
    assert _expression("serialize(map{'a': [1, 2]}, map{'method': 'json'})").principal == (
        XdmAtomic.string('{"a":[1,2]}'),
    )
    assert _expression("serialize(('x', 1), map{'method': 'adaptive'})").principal == (
        XdmAtomic.string('"x"\n1'),
    )


# ---------------------------------------------------------------------------
# The output:serialization-parameters element form


def test_serialize_accepts_the_serialization_parameters_element() -> None:
    body = (
        '<xsl:variable name="p">'
        f'<output:serialization-parameters xmlns:output="{SERIALIZATION_NS}">'
        '<output:method value="json"/>'
        "<output:use-character-maps>"
        '<output:character-map character="x" map-string="y"/>'
        "</output:use-character-maps>"
        "</output:serialization-parameters>"
        "</xsl:variable>"
        '<xsl:template name="main"><xsl:sequence select="serialize(map{&quot;a&quot;: &quot;x&quot;}, $p)"/></xsl:template>'
    )

    assert _transform(body).principal == (XdmAtomic.string('{"a":"y"}'),)


def test_serialize_element_form_defaults_omit_the_declaration() -> None:
    body = (
        f'<xsl:variable name="p" xmlns:output="{SERIALIZATION_NS}">'
        "<output:serialization-parameters/>"
        "</xsl:variable>"
        '<xsl:template name="main"><xsl:sequence select="serialize($n, $p)"/></xsl:template>'
    )
    body = _NODE + body

    assert _transform(body, attributes=' exclude-result-prefixes="xs"').principal == (
        XdmAtomic.string("<r>x</r>"),
    )


def test_serialize_element_form_values_and_attributes_are_applied() -> None:
    body = _NODE + (
        f'<xsl:variable name="p" xmlns:output="{SERIALIZATION_NS}">'
        "<output:serialization-parameters>"
        '<output:omit-xml-declaration value="no"/>'
        '<output:indent value="yes"/>'
        "</output:serialization-parameters>"
        "</xsl:variable>"
        '<xsl:template name="main"><xsl:sequence select="serialize($n, $p)"/></xsl:template>'
    )

    assert _transform(body, attributes=' exclude-result-prefixes="xs"').principal == (
        XdmAtomic.string('<?xml version="1.0" encoding="UTF-8"?>\n<r>x</r>'),
    )


def test_serialize_element_form_rejects_invalid_values_and_names() -> None:
    def element_form(children: str) -> str:
        return (
            _NODE + f'<xsl:variable name="p" xmlns:output="{SERIALIZATION_NS}">'
            "<output:serialization-parameters>"
            f"{children}"
            "</output:serialization-parameters>"
            "</xsl:variable>"
            '<xsl:template name="main"><xsl:sequence select="serialize($n, $p)"/></xsl:template>'
        )

    invalid = element_form('<output:indent value="maybe"/>')
    assert _error_code(lambda: _transform(invalid)) == "SEPM0016"

    unknown = element_form('<output:bogus value="x"/>')
    assert _error_code(lambda: _transform(unknown)) == "SEPM0016"

    missing = element_form("<output:method/>")
    assert _error_code(lambda: _transform(missing)) == "SEPM0016"


# ---------------------------------------------------------------------------
# Errors


def test_serialize_rejects_unknown_parameters_with_sepm0016() -> None:
    assert (
        _error_code(lambda: _expression("serialize('x', map{'method': 'json', 'unknown': 'x'})"))
        == "SEPM0016"
    )
    assert _error_code(lambda: _expression("serialize('x', map{'method': 'yaml'})")) == "SEPM0016"


def test_serialize_rejects_qname_parameter_keys_with_sepm0016() -> None:
    assert (
        _error_code(
            lambda: _expression("serialize('x', map{QName('urn:example', 'method'): 'json'})")
        )
        == "SEPM0016"
    )

    bridge = XsltContextBridge(Transformation())
    serialize = bridge.function_overrides()[f"{{{FN_NS}}}serialize"]
    value = (XdmAtomic.string("x"),)
    bare = XdmAtomic("method", f"{{{XSD_NS}}}QName", None)
    params = (XdmMap([(bare, XdmAtomic.string("json"))]),)

    with pytest.raises(XsltSerializationError) as caught:
        serialize.invoke(None, (value, params))
    assert caught.value.code == "SEPM0016"

    bare = XdmAtomic("x", f"{{{XSD_NS}}}QName", None)
    params = (XdmMap([(XdmAtomic.string("cdata-section-elements"), bare)]),)
    document = (XdmDocument.parse(b"<x>t</x>"),)
    assert serialize.invoke(None, (document, params)) == (XdmAtomic.string("<x><![CDATA[t]]></x>"),)


def test_serialize_keeps_serialization_codes() -> None:
    assert (
        _error_code(lambda: _expression("serialize(exists#1, map{'method': 'json'})")) == "SERE0021"
    )
    assert (
        _error_code(lambda: _expression("serialize(('a', 'b'), map{'method': 'json'})"))
        == "SERE0023"
    )


def test_serialize_rejects_wrong_value_types_with_xpty0004() -> None:
    for parameters in (
        "map{'indent': 'yes'}",
        "map{'indent': 1}",
        "map{'method': 1}",
        "map{'method': (1, 2)}",
        "map{'method': $n}",
        "map{'version': true()}",
        "map{'standalone': 'omit'}",
        "map{'html-version': '5'}",
        "map{'cdata-section-elements': 'a'}",
        "map{'suppress-indentation': ('a', 1)}",
        "map{'use-character-maps': 'm'}",
        "map{'use-character-maps': map{'a': (1, 2)}}",
    ):
        assert _error_code(lambda parameters=parameters: _serialize_node(parameters)) == (
            "XPTY0004"
        ), parameters


def test_serialize_valid_values_with_invalid_content_use_serialization_codes() -> None:
    assert _error_code(lambda: _serialize_node("map{'normalization-form': 'NFX'}")) == "SESU0011"
    assert _error_code(lambda: _serialize_node("map{'encoding': 'not-real'}")) == "SESU0007"
    assert (
        _error_code(lambda: _serialize_node("map{'use-character-maps': map{'ab': 'x'}}"))
        == "SEPM0016"
    )


def test_serialize_character_map_values_are_converted() -> None:
    assert _serialize_node(
        "map{'method': 'text', 'use-character-maps': map{'x': 'y'}}"
    ).principal == (XdmAtomic.string("y"),)


def test_serialize_rejects_a_non_map_parameter_argument() -> None:
    assert _error_code(lambda: _expression("serialize('x', 'json')")) == "XPTY0004"
    assert _error_code(lambda: _expression("serialize('x', ('a', 'b'))")) == "XPTY0004"

    commented = (
        '<xsl:variable name="c"><!--note--></xsl:variable>'
        '<xsl:template name="main">'
        "<xsl:sequence select=\"serialize('x', $c)\"/></xsl:template>"
    )
    assert _error_code(lambda: _transform(commented)) == "XPTY0004"


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


def test_serialize_accepts_null_namespace_qname_methods() -> None:
    assert _expression("serialize('x', map{'method': QName('', 'json')})").principal == (
        XdmAtomic.string('"x"'),
    )

    body = (
        '<xsl:variable name="n"><r>x</r></xsl:variable>'
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        "map{'method': 'json', 'json-node-output-method': QName('', 'xml')})"
        '"/></xsl:template>'
    )
    assert _transform(body, attributes=' exclude-result-prefixes="xs"').principal == (
        XdmAtomic.string('"<r>x<\\/r>"'),
    )

    bridge = XsltContextBridge(Transformation())
    serialize = bridge.function_overrides()[f"{{{FN_NS}}}serialize"]
    value = (XdmAtomic.string("x"),)

    with xsd.qname_context({}):
        typed = xsd.QName("json")
    method = XdmAtomic.typed(typed, xsd.QName)
    params = (XdmMap([(XdmAtomic.string("method"), method)]),)
    assert serialize.invoke(None, (value, params)) == (XdmAtomic.string('"x"'),)


def test_serialize_rejects_namespaced_qname_methods() -> None:
    assert (
        _error_code(
            lambda: _expression("serialize('x', map{'method': QName('urn:other', 'json')})")
        )
        == "SEPM0016"
    )
    assert (
        _error_code(
            lambda: _expression(
                "serialize('x', map{'method': 'json', "
                "'json-node-output-method': QName('urn:other', 'xml')})"
            )
        )
        == "SEPM0016"
    )

    bridge = XsltContextBridge(Transformation())
    serialize = bridge.function_overrides()[f"{{{FN_NS}}}serialize"]
    value = (XdmAtomic.string("x"),)

    with xsd.qname_context({"p": "urn:other"}):
        typed = xsd.QName("p:json")
    method = XdmAtomic.typed(typed, xsd.QName)
    params = (XdmMap([(XdmAtomic.string("method"), method)]),)
    with pytest.raises(XsltSerializationError) as caught:
        serialize.invoke(None, (value, params))
    assert caught.value.code == "SEPM0016"

    unresolved = XdmAtomic("p:json", f"{{{XSD_NS}}}QName", None)
    params = (XdmMap([(XdmAtomic.string("method"), unresolved)]),)
    with pytest.raises(XsltSerializationError) as caught:
        serialize.invoke(None, (value, params))
    assert caught.value.code == "SEPM0016"


def test_serialize_converts_qname_list_parameters() -> None:
    body = (
        '<xsl:variable name="n"><r>x</r></xsl:variable>'
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        "map{'cdata-section-elements': QName('', 'r'), "
        "'omit-xml-declaration': true()})\"/></xsl:template>"
    )
    assert _transform(body, attributes=' exclude-result-prefixes="xs"').principal == (
        XdmAtomic.string("<r><![CDATA[x]]></r>"),
    )

    namespaced = (
        '<xsl:variable name="n"><r xmlns="urn:example">x</r></xsl:variable>'
        '<xsl:template name="main"><xsl:sequence select="serialize($n, '
        "map{'cdata-section-elements': QName('urn:example', 'r'), "
        "'omit-xml-declaration': true()})\"/></xsl:template>"
    )
    assert _transform(namespaced, attributes=' exclude-result-prefixes="xs"').principal == (
        XdmAtomic.string('<r xmlns="urn:example"><![CDATA[x]]></r>'),
    )


def test_serialize_rejects_unresolved_qname_values() -> None:
    bridge = XsltContextBridge(Transformation())
    serialize = bridge.function_overrides()[f"{{{FN_NS}}}serialize"]
    value = (XdmAtomic.string("x"),)
    unresolved = XdmAtomic("p:x", f"{{{XSD_NS}}}QName", None)
    params = (XdmMap([(XdmAtomic.string("cdata-section-elements"), unresolved)]),)

    with pytest.raises(XsltSerializationError) as caught:
        serialize.invoke(None, (value, params))
    assert caught.value.code == "SEPM0016"

    bare = XdmAtomic("x", f"{{{XSD_NS}}}QName", None)
    params = (XdmMap([(XdmAtomic.string("cdata-section-elements"), bare)]),)
    document = (XdmDocument.parse(b"<x>t</x>"),)
    assert serialize.invoke(None, (document, params)) == (XdmAtomic.string("<x><![CDATA[t]]></x>"),)


def test_serialize_accepts_a_decimal_html_version() -> None:
    assert _serialize_node("map{'html-version': 5, 'omit-xml-declaration': true()}").principal == (
        XdmAtomic.string("<r>x</r>"),
    )


def test_serialize_character_map_key_and_value_types_are_converted() -> None:
    assert (
        _error_code(lambda: _expression("serialize('x', map{'use-character-maps': map{1: 'x'}})"))
        == "XPTY0004"
    )
    assert (
        _error_code(lambda: _expression("serialize('x', map{'use-character-maps': map{'a': 1}})"))
        == "XPTY0004"
    )


def test_serialize_element_form_ignores_foreign_content() -> None:
    body = (
        '<xsl:variable name="p">'
        f'<output:serialization-parameters xmlns:output="{SERIALIZATION_NS}">'
        "junk"
        '<output:method value="json"/>'
        "<output:use-character-maps>"
        'junk<output:character-map character="x" map-string="y"/>'
        "</output:use-character-maps>"
        "</output:serialization-parameters>"
        "</xsl:variable>"
        '<xsl:template name="main"><xsl:sequence select="serialize(map{&quot;a&quot;: &quot;x&quot;}, $p/*)"/></xsl:template>'
    )

    assert _transform(body).principal == (XdmAtomic.string('{"a":"y"}'),)


def test_serialize_element_form_requires_character_map_attributes() -> None:
    body = (
        '<xsl:variable name="p">'
        f'<output:serialization-parameters xmlns:output="{SERIALIZATION_NS}">'
        "<output:use-character-maps>"
        '<output:character-map character="x"/>'
        "</output:use-character-maps>"
        "</output:serialization-parameters>"
        "</xsl:variable>"
        '<xsl:template name="main"><xsl:sequence select="serialize(\'x\', $p)"/></xsl:template>'
    )

    assert _error_code(lambda: _transform(body)) == "SEPM0016"


def test_serialize_rejects_non_parameter_elements() -> None:
    body = _NODE + (
        '<xsl:template name="main"><xsl:sequence select="serialize(\'x\', $n/*)"/></xsl:template>'
    )
    assert _error_code(lambda: _transform(body)) == "XPTY0004"

    empty_document = (
        '<xsl:variable name="p"><!--note--></xsl:variable>'
        '<xsl:template name="main">'
        "<xsl:sequence select=\"serialize('x', $p)\"/></xsl:template>"
    )
    assert _error_code(lambda: _transform(empty_document)) == "XPTY0004"

    two_documents = (
        '<xsl:variable name="p"><a/><b/></xsl:variable>'
        '<xsl:template name="main">'
        "<xsl:sequence select=\"serialize('x', $p)\"/></xsl:template>"
    )
    assert _error_code(lambda: _transform(two_documents)) == "XPTY0004"
