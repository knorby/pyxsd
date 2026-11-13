"""The JSON and adaptive output methods from Serialization 3.1.

These tests operate on XDM values directly.  JSON and adaptive output never
perform sequence normalization, so each test pins the exact item-level rule
rather than a normalized text result.
"""

from __future__ import annotations

import io

import pytest
from elementpath.datatypes import QName
from tests.xslt.support import stylesheet

from pyxsd import xsd_data_types as xsd
from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmItem, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmDocument
from pyxsd.xslt.errors import XsltSerializationError
from pyxsd.xslt.processor import XsltProcessor
from pyxsd.xslt.serialization import SerializationParameters, Serializer

FN_NS = "http://www.w3.org/2005/xpath-functions"
MATH_NS = "http://www.w3.org/2005/xpath-functions/math"
MAP_NS = "http://www.w3.org/2005/xpath-functions/map"
ARRAY_NS = "http://www.w3.org/2005/xpath-functions/array"
XSD_NS = "http://www.w3.org/2001/XMLSchema"


def _serialize(sequence: tuple[object, ...], **values: object) -> str:
    parameters = SerializationParameters.merge(explicit=values)
    return Serializer.serialize(sequence, parameters)  # type: ignore[arg-type]


def _error_code(action: object) -> str | None:
    assert callable(action)
    with pytest.raises(XsltSerializationError) as error:
        action()
    return error.value.code


def _map(*entries: tuple[object, object]) -> XdmMap:
    pairs = []
    for key, value in entries:
        if isinstance(key, str):
            key = XdmAtomic.string(key)
        pairs.append((key, value))
    return XdmMap(pairs)  # type: ignore[arg-type]


def _function(name: str | None, arity: int = 1) -> XdmFunction:
    return XdmFunction(lambda arguments, environment, closure: (), arity=arity, name=name)


# ---------------------------------------------------------------------------
# Method support and parameter validation


def test_json_and_adaptive_are_supported_methods() -> None:
    assert Serializer.supports_method("json")
    assert Serializer.supports_method("adaptive")
    assert Serializer.supports_method("Q{urn:example}json")
    assert Serializer.supports_method("p:adaptive")
    assert not Serializer.supports_method("yaml")


def test_modern_parameters_validate_through_sepm0016() -> None:
    parameters = SerializationParameters(
        method="json",
        allow_duplicate_names=True,
        json_node_output_method="Q{urn:example}html",
    )
    assert parameters.method == "json"
    assert parameters.allow_duplicate_names is True
    assert parameters.json_node_output_method == "html"

    assert _error_code(lambda: SerializationParameters(allow_duplicate_names="maybe")) == "SEPM0016"
    for method in ("yaml", "json", "adaptive"):
        assert (
            _error_code(
                lambda method=method: SerializationParameters(json_node_output_method=method)
            )
            == "SEPM0016"
        )
    assert _error_code(lambda: SerializationParameters(character_maps=(("ab", "x"),))) == "SEPM0016"
    assert (
        _error_code(lambda: SerializationParameters(character_maps=(("a", 1),)))  # type: ignore[arg-type]
        == "SEPM0016"
    )
    assert SerializationParameters(character_maps=None).character_maps == ()  # type: ignore[arg-type]
    assert SerializationParameters(character_maps={"a": "b"}).character_maps == (("a", "b"),)
    assert (
        _error_code(lambda: SerializationParameters(character_maps="ab"))  # type: ignore[arg-type]
        == "SEPM0016"
    )
    assert (
        _error_code(lambda: SerializationParameters(character_maps=("ab",)))  # type: ignore[arg-type]
        == "SEPM0016"
    )
    assert (
        _error_code(lambda: SerializationParameters(json_node_output_method=1))  # type: ignore[arg-type]
        == "SEPM0016"
    )


# ---------------------------------------------------------------------------
# JSON output method


def test_json_empty_sequence_serializes_as_null() -> None:
    assert _serialize((), method="json") == "null"


def test_json_serializes_scalar_atomic_values() -> None:
    assert _serialize((XdmAtomic.string("a"),), method="json") == '"a"'
    assert _serialize((XdmAtomic.untyped_atomic("u"),), method="json") == '"u"'
    assert _serialize((XdmAtomic.any_uri("urn:x"),), method="json") == '"urn:x"'
    assert _serialize((XdmAtomic.boolean(True),), method="json") == "true"
    assert _serialize((XdmAtomic.boolean(False),), method="json") == "false"
    assert _serialize((XdmAtomic.integer(5),), method="json") == "5"
    assert _serialize((XdmAtomic.decimal("1.5"),), method="json") == "1.5"
    assert _serialize((XdmAtomic.double(1.5),), method="json") == "1.5"
    assert _serialize((XdmAtomic.typed(xsd.Date("2020-01-01"), xsd.Date),), method="json") == (
        '"2020-01-01"'
    )


def test_json_rejects_non_finite_numbers_with_sere0020() -> None:
    for value in (float("inf"), float("-inf"), float("nan")):
        assert (
            _error_code(lambda value=value: _serialize((XdmAtomic.double(value),), method="json"))
            == "SERE0020"
        )
        assert (
            _error_code(lambda value=value: _serialize((XdmAtomic.float(value),), method="json"))
            == "SERE0020"
        )


def test_json_rejects_more_than_one_item_with_sere0023() -> None:
    assert (
        _error_code(lambda: _serialize((XdmAtomic.integer(1), XdmAtomic.integer(2)), method="json"))
        == "SERE0023"
    )
    nested = _map(("a", (XdmAtomic.integer(1), XdmAtomic.integer(2))))
    assert _error_code(lambda: _serialize((nested,), method="json")) == "SERE0023"
    member = XdmArray([(XdmAtomic.integer(1), XdmAtomic.integer(2))])
    assert _error_code(lambda: _serialize((member,), method="json")) == "SERE0023"


def test_json_rejects_function_items_with_sere0021() -> None:
    assert _error_code(lambda: _serialize((_function(None),), method="json")) == "SERE0021"
    nested = _map(("a", _function(None)))
    assert _error_code(lambda: _serialize((nested,), method="json")) == "SERE0021"
    member = XdmArray([_function(None)])
    assert _error_code(lambda: _serialize((member,), method="json")) == "SERE0021"


def test_json_serializes_maps_and_arrays() -> None:
    mapping = _map(
        ("a", XdmAtomic.integer(1)),
        ("b", XdmArray([XdmAtomic.integer(2), XdmAtomic.string("x")])),
        ("c", XdmMap.empty()),
    )
    assert _serialize((mapping,), method="json") == '{"a":1,"b":[2,"x"],"c":{}}'
    assert _serialize((XdmArray([]),), method="json") == "[]"
    assert _serialize((XdmMap.empty(),), method="json") == "{}"


def test_json_duplicate_string_keys_raise_sere0022() -> None:
    mapping = _map(
        (XdmAtomic.integer(1), XdmAtomic.string("first")),
        (XdmAtomic.string("1"), XdmAtomic.string("second")),
    )
    assert _error_code(lambda: _serialize((mapping,), method="json")) == "SERE0022"
    assert _serialize((mapping,), method="json", allow_duplicate_names=True) == (
        '{"1":"first","1":"second"}'
    )
    assert (
        _serialize((mapping,), method="json", allow_duplicate_names="yes")
        == '{"1":"first","1":"second"}'
    )


def test_json_serializes_nodes_with_json_node_output_method() -> None:
    document = XdmDocument.parse(b"<a>t</a>")

    assert _serialize((document,), method="json") == '"<a>t<\\/a>"'
    assert _serialize((document,), method="json", json_node_output_method="text") == '"t"'
    assert _serialize((document,), method="json", json_node_output_method="xhtml") == (
        '"<a>t<\\/a>"'
    )

    quoted = XdmDocument.parse(b"<a>say &quot;hi&quot;</a>")
    assert _serialize((quoted,), method="json") == '"<a>say \\"hi\\"<\\/a>"'


def test_json_string_escaping() -> None:
    value = XdmAtomic.string('q"\\/ \b\f\n\r\t\x01\x7f\x9f')

    assert _serialize((value,), method="json") == (
        '"q\\"\\\\\\/ \\b\\f\\n\\r\\t\\u0001\\u007F\\u009F"'
    )


def test_json_non_encodable_characters_escape_instead_of_raising() -> None:
    assert _serialize((XdmAtomic.string("\u00e9"),), method="json", encoding="ascii") == (
        '"\\u00E9"'
    )
    assert _serialize((XdmAtomic.string("\U0001f600"),), method="json", encoding="ascii") == (
        '"\\uD83D\\uDE00"'
    )
    assert _serialize((XdmAtomic.string("\u00e9"),), method="json") == '"\u00e9"'
    encoded = Serializer.encode(
        _serialize((XdmAtomic.string("\u00e9"),), method="json", encoding="ascii"),
        SerializationParameters(method="json", encoding="ascii"),
    )
    assert encoded == b'"\\u00E9"'


# ---------------------------------------------------------------------------
# Adaptive output method


def test_adaptive_serializes_scalar_atomic_values() -> None:
    assert _serialize((XdmAtomic.string('a"b'),), method="adaptive") == '"a""b"'
    assert _serialize((XdmAtomic.untyped_atomic("u"),), method="adaptive") == '"u"'
    assert _serialize((XdmAtomic.any_uri("urn:x"),), method="adaptive") == '"urn:x"'
    assert _serialize((XdmAtomic.boolean(True),), method="adaptive") == "true()"
    assert _serialize((XdmAtomic.boolean(False),), method="adaptive") == "false()"
    assert _serialize((XdmAtomic.integer(5),), method="adaptive") == "5"
    assert _serialize((XdmAtomic.decimal("1.5"),), method="adaptive") == "1.5"
    date = XdmAtomic.typed(xsd.Date("2020-01-01"), xsd.Date)
    assert _serialize((date,), method="adaptive") == 'xs:date("2020-01-01")'


def test_adaptive_serializes_doubles_with_the_number_picture() -> None:
    assert _serialize((XdmAtomic.double(1.5),), method="adaptive") == "1.5e0"
    assert _serialize((XdmAtomic.double(100),), method="adaptive") == "1.0e2"
    assert _serialize((XdmAtomic.double(1e20),), method="adaptive") == "1.0e20"
    assert _serialize((XdmAtomic.double(0.0001),), method="adaptive") == "1.0e-4"
    assert _serialize((XdmAtomic.double(float("inf")),), method="adaptive") == "INF"
    assert _serialize((XdmAtomic.double(float("-inf")),), method="adaptive") == "-INF"
    assert _serialize((XdmAtomic.double(float("nan")),), method="adaptive") == "NaN"


def test_adaptive_serializes_float_by_the_constructor_rule() -> None:
    assert _serialize((XdmAtomic.float(1.5),), method="adaptive") == 'xs:float("1.5")'
    assert _serialize((XdmAtomic.float(float("inf")),), method="adaptive") == ('xs:float("INF")')


def test_adaptive_serializes_qnames_as_uri_qualified_names() -> None:
    with xsd.qname_context({"p": "urn:example"}):
        value = XdmAtomic.typed(xsd.QName("p:local"), xsd.QName)

    assert _serialize((value,), method="adaptive") == "Q{urn:example}local"


def test_adaptive_serializes_elementpath_and_unresolved_qnames() -> None:
    elementpath_qname = QName("urn:example", "p:local")
    item = XdmAtomic(elementpath_qname, f"{{{XSD_NS}}}QName", QName)
    assert _serialize((item,), method="adaptive") == "Q{urn:example}local"

    unresolved = XdmAtomic.typed(xsd.QName("p:local"), xsd.QName)
    assert _serialize((unresolved,), method="adaptive") == "p:local"

    bare = XdmAtomic("local", f"{{{XSD_NS}}}QName", None)
    assert _serialize((bare,), method="adaptive") == "Q{}local"


def test_adaptive_serializes_arrays_and_maps() -> None:
    array = XdmArray([XdmAtomic.integer(1), XdmAtomic.integer(2)])
    assert _serialize((array,), method="adaptive") == "[1,2]"

    multi = XdmArray([(XdmAtomic.integer(1), XdmAtomic.integer(2))])
    assert _serialize((multi,), method="adaptive") == "[(1,2)]"

    mapping = _map(("a", XdmAtomic.integer(1)), ("b", (XdmAtomic.integer(2),)))
    assert _serialize((mapping,), method="adaptive") == 'map{"a":1,"b":2}'

    empty_value = _map(("e", ()))
    assert _serialize((empty_value,), method="adaptive") == 'map{"e":()}'

    multi_value = _map(("m", (XdmAtomic.integer(1), XdmAtomic.integer(2))))
    assert _serialize((multi_value,), method="adaptive") == 'map{"m":(1,2)}'


def test_adaptive_serializes_function_items_with_conventional_prefixes() -> None:
    assert _serialize((_function(f"{{{FN_NS}}}exists"),), method="adaptive") == "fn:exists#1"
    assert _serialize((_function(f"{{{MATH_NS}}}pi", 0),), method="adaptive") == "math:pi#0"
    assert _serialize((_function(f"{{{MAP_NS}}}get", 2),), method="adaptive") == "map:get#2"
    assert _serialize((_function(f"{{{ARRAY_NS}}}size"),), method="adaptive") == "array:size#1"
    assert _serialize((_function(f"{{{XSD_NS}}}string"),), method="adaptive") == "xs:string#1"
    assert _serialize((_function("{urn:example}f", 2),), method="adaptive") == ("Q{urn:example}f#2")
    assert _serialize((_function(None),), method="adaptive") == "(anonymous-function)#1"


def test_adaptive_serializes_nodes_and_standalone_attributes() -> None:
    document = XdmDocument.parse(b'<a xmlns:p="urn:x" id="1"><b/></a>')
    root = document.root_element
    assert (
        _serialize((document,), method="adaptive", omit_xml_declaration=True)
        == '<a xmlns:p="urn:x" id="1"><b/></a>'
    )

    attribute = root.attributes[0]
    assert attribute.kind is NodeKind.ATTRIBUTE
    assert _serialize((attribute,), method="adaptive") == 'id="1"'

    namespace = root.namespace_node("p")
    assert namespace is not None
    assert _serialize((namespace,), method="adaptive") == 'xmlns:p="urn:x"'

    default = XdmDocument.parse(b'<a xmlns="urn:d" xmlns:p="urn:x" p:id="1"/>').root_element
    prefixed = default.attributes[0]
    assert _serialize((prefixed,), method="adaptive") == 'p:id="1"'
    default_namespace = default.namespace_nodes[0]
    assert _serialize((default_namespace,), method="adaptive") == 'xmlns="urn:d"'


def test_adaptive_uses_item_separator_or_a_newline() -> None:
    items = (XdmAtomic.string("a"), XdmAtomic.integer(1))

    assert _serialize(items, method="adaptive") == '"a"\n1'
    assert _serialize(items, method="adaptive", item_separator="|") == '"a"|1'
    assert _serialize((), method="adaptive") == ""


def test_json_and_adaptive_reject_unknown_item_kinds() -> None:
    item = XdmItem()

    assert _error_code(lambda: _serialize((item,), method="json")) == "SERE0021"
    assert _error_code(lambda: _serialize((item,), method="adaptive")) == "SENR0001"


def test_adaptive_function_name_fallback_spellings() -> None:
    assert _serialize((_function("local"),), method="adaptive") == "Q{}local#1"
    assert _serialize((_function("fn:exists"),), method="adaptive") == "fn:exists#1"


def test_adaptive_character_maps_apply_to_quoted_strings_and_nodes() -> None:
    parameters = {
        "method": "adaptive",
        "character_maps": (("x", "<"),),
    }

    assert _serialize((XdmAtomic.string("x"),), **parameters) == '"<"'
    document = XdmDocument.parse(b"<r>x</r>")
    assert _serialize((document,), omit_xml_declaration=True, **parameters) == "<r><</r>"


def test_adaptive_delegates_all_parameters_to_xml_nodes() -> None:
    document = XdmDocument.parse(b"<a><b>t</b></a>")

    assert _serialize((document,), method="adaptive") == (
        '<?xml version="1.0" encoding="UTF-8"?><a><b>t</b></a>'
    )
    assert _serialize((document,), method="adaptive", omit_xml_declaration=True) == (
        "<a><b>t</b></a>"
    )
    assert _serialize((document,), method="adaptive", omit_xml_declaration=True, indent=True) == (
        "<a>\n  <b>t</b>\n</a>"
    )
    assert (
        _serialize(
            (document,),
            method="adaptive",
            omit_xml_declaration=True,
            character_maps=(("t", "u"),),
        )
        == "<a><b>u</b></a>"
    )


def _function_item(expression: str) -> XdmFunction:
    source = stylesheet(
        '<xsl:function name="my:f"><xsl:param name="a"/><xsl:sequence select="$a"/>'
        "</xsl:function>"
        f'<xsl:template name="main"><xsl:sequence select="{expression}"/></xsl:template>',
        namespaces={"my": "urn:my"},
    )
    compiled = XsltProcessor().compile(io.BytesIO(source))
    return compiled.transform(delivery="raw", initial_template="main").principal[0]


def test_native_function_reference_names_are_propagated() -> None:
    cases = {
        "exists#1": "fn:exists#1",
        "math:pi#0": "math:pi#0",
        "map:get#2": "map:get#2",
        "array:size#1": "array:size#1",
        "local-name#0": "fn:local-name#0",
        "my:f#1": "Q{urn:my}f#1",
        "function($a){$a}": "(anonymous-function)#1",
    }
    for expression, expected in cases.items():
        item = _function_item(expression)
        assert _serialize((item,), method="adaptive") == expected, expression
