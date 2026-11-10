"""XSLT 3.0 map construction and generalized item-pattern execution.

Every stylesheet is written inline.  The tests drive public processor entry
points and the runtime instruction value layer directly; none depends on the
pinned conformance corpus.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic, XdmFunction, XdmMap
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError, XsltError, XsltStaticError
from pyxsd.xslt.runtime.instructions import (
    MapEntryInstruction,
    MapInstruction,
    construct_instruction,
    construct_map,
    construct_map_entry,
    is_deemed_empty,
    is_vacuous,
)

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"


def stylesheet(body: str, *, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="3.0"{attributes}>{body}</xsl:stylesheet>'
    ).encode()


def transform(
    source: bytes,
    input_source: bytes | None = None,
    *,
    delivery: str = "document",
    **options: Any,
):
    compiled = XsltProcessor().compile(io.BytesIO(source))
    if input_source is None:
        return compiled.transform(delivery=delivery, **options)
    return compiled.transform(source=input_source, delivery=delivery, **options)


def error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


# ---------------------------------------------------------------------------
# Runtime map values


def test_map_instruction_merges_entries_in_observable_order() -> None:
    first = construct_map_entry((XdmAtomic.string("a"),), (XdmAtomic.integer(1),))
    second = construct_map_entry((XdmAtomic.string("b"),), (XdmAtomic.string("two"),))

    merged = construct_map((first, second))

    assert isinstance(merged, XdmMap)
    assert [key.lexical for key, _ in merged.entries] == ["a", "b"]
    assert merged.get(XdmAtomic.string("a")) == (XdmAtomic.integer(1),)
    assert merged.get(XdmAtomic.string("b")) == (XdmAtomic.string("two"),)
    assert construct_map(()).size == 0


def test_map_instruction_rejects_duplicate_keys() -> None:
    first = construct_map_entry((XdmAtomic.string("a"),), (XdmAtomic.integer(1),))
    second = construct_map_entry((XdmAtomic.string("a"),), (XdmAtomic.integer(2),))

    with pytest.raises(XsltDynamicError) as caught:
        construct_map((first, second))
    assert caught.value.code == "XTDE3365"
    assert "'a'" in str(caught.value)


def test_map_instruction_requires_map_items() -> None:
    with pytest.raises(XsltDynamicError) as caught:
        construct_map((XdmAtomic.integer(1),))  # type: ignore[arg-type]
    assert caught.value.code == "XTTE3375"


def test_map_entry_key_conversion_and_cardinality() -> None:
    entry = construct_map_entry(
        (XdmAtomic.untyped_atomic("1"),),
        (XdmAtomic.string("value"),),
    )
    ((key, value),) = entry.entries
    assert key.type_name.endswith("}string")
    assert key.lexical == "1"
    assert value == (XdmAtomic.string("value"),)

    numeric = construct_map_entry((XdmAtomic.integer(3),), ())
    assert numeric.keys() == (XdmAtomic.integer(3),)
    assert numeric.values() == ((),)

    for supplied, code in (
        ((), "XPTY0004"),
        ((XdmAtomic.integer(1), XdmAtomic.integer(2)), "XPTY0004"),
    ):
        with pytest.raises(XsltDynamicError) as caught:
            construct_map_entry(supplied, ())
        assert caught.value.code == code

    with pytest.raises(XsltDynamicError) as caught:
        construct_map_entry((object(),), ())  # type: ignore[arg-type]
    assert caught.value.code == "FOTY0013"


def test_map_entry_atomizes_a_node_key() -> None:
    document = XdmDocument.parse(b"<doc><n>7</n></doc>")
    node = document.root_element
    assert node is not None
    key = node.children[0]

    entry = construct_map_entry((key,), ())

    ((converted, _),) = entry.entries
    assert converted.type_name.endswith("}string")
    assert converted.lexical == "7"


def test_map_instruction_dataclasses_construct_values() -> None:
    entry = MapEntryInstruction(XdmAtomic.string("a"), (XdmAtomic.integer(1),))
    entry_sequence = construct_instruction(entry)
    assert len(entry_sequence) == 1
    assert isinstance(entry_sequence[0], XdmMap)
    assert entry_sequence[0].entries == ((XdmAtomic.string("a"), (XdmAtomic.integer(1),)),)
    merged = MapInstruction((construct_map_entry((XdmAtomic.string("a"),), ()),))
    merged_sequence = construct_instruction(merged)
    assert isinstance(merged_sequence[0], XdmMap)
    assert merged_sequence[0].size == 1

    with pytest.raises(TypeError):
        MapEntryInstruction(XdmAtomic.string("a"), (object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MapEntryInstruction("key", ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MapInstruction((object(),))  # type: ignore[arg-type]


def test_non_value_items_are_never_deemed_empty() -> None:
    function = XdmFunction(lambda arguments, environment, closure: (), arity=0)
    assert is_vacuous(function) is False
    assert is_deemed_empty(function) is False


# ---------------------------------------------------------------------------
# Value predicates shared by conditional content and xsl:map


def test_vacuous_and_deemed_empty_predicates() -> None:
    empty_text = XdmDocument.parse(b"<doc/>").root_element
    assert empty_text is not None
    text = empty_text.children[0] if empty_text.children else None
    assert text is None

    assert is_vacuous(XdmAtomic.string("")) is True
    assert is_vacuous(XdmAtomic.integer(0)) is False
    assert is_vacuous(XdmMap.empty()) is False
    empty_document = XdmDocument.parse(b"<doc/>").root_element
    assert empty_document is not None
    assert is_vacuous(empty_document) is False
    assert is_deemed_empty(empty_document) is True

    empty_map = XdmMap.empty()
    assert is_deemed_empty(empty_map) is True


# ---------------------------------------------------------------------------
# Instruction-level map construction


def test_map_instruction_builds_a_lookup_table() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="m" as="map(*)">
            <xsl:map>
              <xsl:map-entry key="'a'" select="1"/>
              <xsl:map-entry key="'b'" select="('x', 'y')"/>
            </xsl:map>
          </xsl:variable>
          <xsl:sequence select="map:size($m), $m('a'), $m('b')"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(2),
        XdmAtomic.integer(1),
        XdmAtomic.string("x"),
        XdmAtomic.string("y"),
    )


def test_map_duplicate_keys_are_a_dynamic_error() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:map>
            <xsl:map-entry key="'a'" select="1"/>
            <xsl:map-entry key="'a'" select="2"/>
          </xsl:map>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(source, initial_template="main", delivery="raw")) == (
        "XTDE3365"
    )


def test_map_content_must_be_maps() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:map>text</xsl:map>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(source, initial_template="main", delivery="raw")) == (
        "XTTE3375"
    )


def test_map_entry_without_key_is_static_error() -> None:
    source = stylesheet(
        """
        <xsl:template name="main"><xsl:map><xsl:map-entry select="1"/></xsl:map></xsl:template>
        """
    )
    assert error_code(lambda: transform(source, initial_template="main")) == "XTSE0010"


def test_map_entry_select_and_content_are_mutually_exclusive() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:map><xsl:map-entry key="'a'" select="1">content</xsl:map-entry></xsl:map>
        </xsl:template>
        """
    )
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor().compile(io.BytesIO(source))
    assert caught.value.code == "XTSE3280"


def test_map_entry_empty_value_is_the_empty_sequence() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="m" as="map(*)">
            <xsl:map>
              <xsl:map-entry key="'a'"/>
            </xsl:map>
          </xsl:variable>
          <xsl:sequence select="map:contains($m, 'a'), empty($m('a'))"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
    )


def test_map_rejects_a_select_attribute() -> None:
    source = stylesheet(
        """
        <xsl:template name="main"><xsl:map select="1"/></xsl:template>
        """
    )
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor().compile(io.BytesIO(source))
    assert caught.value.code == "XTSE0010"


# ---------------------------------------------------------------------------
# Generalized item-pattern execution


def test_generalized_item_patterns_match_maps_and_atomics() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="m" as="map(*)">
            <xsl:map>
              <xsl:map-entry key="'k'" select="'v'"/>
            </xsl:map>
          </xsl:variable>
          <xsl:apply-templates select="$m" mode="m"/>
          <xsl:apply-templates select="42" mode="a"/>
        </xsl:template>
        <xsl:template match=".[. instance of map(*)]" mode="m">
          <xsl:sequence select="map:size(.), .('k')"/>
        </xsl:template>
        <xsl:template match=".[. instance of xs:integer]" mode="a">
          <xsl:sequence select=". + 1"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(1),
        XdmAtomic.string("v"),
        XdmAtomic.integer(43),
    )


def test_generalized_pattern_priority_selects_the_specific_rule() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:apply-templates select="(), 'x', map{}" mode="m"/>
        </xsl:template>
        <xsl:template match=".[. instance of map(*)]" mode="m" priority="2">
          <xsl:sequence select="'map'"/>
        </xsl:template>
        <xsl:template match=".[. instance of xs:string]" mode="m" priority="2">
          <xsl:sequence select="'string'"/>
        </xsl:template>
        <xsl:template match=".[. instance of item()]" mode="m" priority="1">
          <xsl:sequence select="'item'"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.string("string"),
        XdmAtomic.string("map"),
    )


def test_map_normalizes_untyped_attribute_keys_to_strings() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="m" as="map(*)">
            <xsl:map>
              <xsl:map-entry key="/*/@code" select="'value'"/>
            </xsl:map>
          </xsl:variable>
          <xsl:sequence select="$m('7'), map:keys($m)[1] instance of xs:string"/>
        </xsl:template>
        """
    )
    result = transform(source, b'<doc code="7"/>', initial_template="main", delivery="raw")
    assert result.principal == (XdmAtomic.string("value"), XdmAtomic.boolean(True))
