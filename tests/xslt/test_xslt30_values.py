"""XSLT 3.0 map construction and generalized item-pattern execution.

Every stylesheet is written inline.  The tests drive public processor entry
points and the runtime instruction value layer directly; none depends on the
pinned conformance corpus.
"""

from __future__ import annotations

import io
from functools import partial
from typing import Any

import pytest
from elementpath.datatypes import Date, Time, UntypedAtomic
from tests.xslt.support import stylesheet as _stylesheet
from tests.xslt.support import transform

from pyxsd.xdm import XdmAtomic, XdmFunction, XdmMap
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xpath.engine import _function_item_arity
from pyxsd.xpath.functions import _date_time_argument, xdm_deep_equal
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

stylesheet = partial(_stylesheet, namespaces={"my": MY_NS})


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
    assert caught.value.code == "XTSE0090"


# ---------------------------------------------------------------------------
# Dynamic calls on maps and arrays


def test_map_dynamic_call_atomizes_a_node_key() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="m" as="map(*)" select="map{'1': '1st', '2': '2nd'}"/>
          <xsl:sequence select="$m(/*/e/@ref)"/>
        </xsl:template>
        """
    )
    result = transform(source, b'<doc><e ref="1"/></doc>', initial_template="main", delivery="raw")
    assert result.principal == (XdmAtomic.string("1st"),)


def test_array_dynamic_call_casts_an_untyped_key_to_integer() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="a" as="array(*)" select="['first', 'second']"/>
          <xsl:sequence select="$a(/*/e/@ref)"/>
        </xsl:template>
        """
    )
    result = transform(source, b'<doc><e ref="2"/></doc>', initial_template="main", delivery="raw")
    assert result.principal == (XdmAtomic.string("second"),)


def test_map_dynamic_call_requires_exactly_one_atomic_key() -> None:
    empty_key = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="m" as="map(*)" select="map{'1': '1st'}"/>
          <xsl:sequence select="$m(())"/>
        </xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(empty_key, initial_template="main", delivery="raw"))
        == "XPTY0004"
    )

    multi_key = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="m" as="map(*)" select="map{'1': '1st'}"/>
          <xsl:sequence select="$m(('1', '2'))"/>
        </xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(multi_key, initial_template="main", delivery="raw"))
        == "XPTY0004"
    )

    extra_argument = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="m" as="map(*)" select="map{'1': '1st'}"/>
          <xsl:sequence select="$m('1', '2')"/>
        </xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(extra_argument, initial_template="main", delivery="raw"))
        == "XPTY0004"
    )


def test_array_dynamic_call_requires_one_integer_position() -> None:
    for expression in ("$a(('1', '2'))", "$a('1')", "$a(1, 2)", "$a(0)"):
        source = stylesheet(
            f"""
            <xsl:template name="main">
              <xsl:variable name="a" as="array(*)" select="['first', 'second']"/>
              <xsl:sequence select="{expression}"/>
            </xsl:template>
            """
        )
        code = error_code(
            lambda source=source: transform(source, initial_template="main", delivery="raw")
        )
        assert code == ("FOAY0001" if expression == "$a(0)" else "XPTY0004")


def test_array_dynamic_call_rejects_an_invalid_untyped_position() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="a" as="array(*)" select="['first', 'second']"/>
          <xsl:sequence select="$a(/*/e/@ref)"/>
        </xsl:template>
        """
    )
    assert (
        error_code(
            lambda: transform(
                source, b'<doc><e ref="first"/></doc>', initial_template="main", delivery="raw"
            )
        )
        == "FORG0001"
    )


def test_xdm_deep_equal_compares_grounded_nodes_structurally() -> None:
    text = b'<d xmlns:p="urn:p" xmlns:q="urn:q" a="1" b="2"><!--c--><?pi v?>text<e>child</e></d>'
    left = XdmDocument.parse(text)
    right = XdmDocument.parse(text)
    left_root = left.root_element
    right_root = right.root_element
    assert left_root is not None and right_root is not None

    assert xdm_deep_equal((left,), (right,)) is True
    assert xdm_deep_equal((left_root,), (right_root,)) is True
    assert xdm_deep_equal((), (left_root,)) is False
    assert xdm_deep_equal((left_root,), (XdmAtomic.string("text"),)) is False
    assert xdm_deep_equal((XdmAtomic.integer(1),), (XdmAtomic.integer(1),)) is True
    assert xdm_deep_equal((XdmAtomic.integer(1),), (XdmAtomic.integer(2),)) is False

    # Element name, attribute map, and child count differences.
    renamed = XdmDocument.parse(
        b'<x xmlns:p="urn:p" xmlns:q="urn:q" a="1" b="2"><!--c--><?pi v?>text<e>child</e></x>'
    ).root_element
    assert renamed is not None
    assert xdm_deep_equal((left_root,), (renamed,)) is False
    changed_attribute = XdmDocument.parse(
        b'<d xmlns:p="urn:p" xmlns:q="urn:q" a="9" b="2"><!--c--><?pi v?>text<e>child</e></d>'
    ).root_element
    assert changed_attribute is not None
    assert xdm_deep_equal((left_root,), (changed_attribute,)) is False
    shorter = XdmDocument.parse(b'<d xmlns:p="urn:p" xmlns:q="urn:q" a="1" b="2"/>').root_element
    assert shorter is not None
    assert xdm_deep_equal((left_root,), (shorter,)) is False

    # Attribute, processing instruction, comment, text, and namespace nodes.
    assert xdm_deep_equal((left_root.attributes[0],), (right_root.attributes[0],)) is True
    assert xdm_deep_equal((left_root.attributes[0],), (changed_attribute.attributes[0],)) is False
    assert xdm_deep_equal((left_root.children[0],), (right_root.children[0],)) is True
    assert xdm_deep_equal((left_root.children[1],), (right_root.children[1],)) is True
    assert xdm_deep_equal((left_root.children[2],), (right_root.children[2],)) is True
    assert xdm_deep_equal((left_root.children[0],), (left_root.children[1],)) is False
    assert xdm_deep_equal((left_root.children[2],), (left_root.children[0],)) is False
    assert xdm_deep_equal((left_root.children[2],), (left_root.children[1],)) is False
    left_namespace = next(node for node in left_root.namespace_nodes if node.prefix == "p")
    right_namespace = next(node for node in right_root.namespace_nodes if node.prefix == "p")
    other_namespace = next(node for node in right_root.namespace_nodes if node.prefix == "q")
    assert xdm_deep_equal((left_namespace,), (right_namespace,)) is True
    assert xdm_deep_equal((left_namespace,), (other_namespace,)) is False


def test_array_is_atomized_in_simple_content() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="a" as="array(*)" select="[1, (2, 3), [4]]"/>
          <out avt="{$a}"><xsl:value-of select="$a" separator=","/></out>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main", delivery="document")
    out = result.principal[0]
    assert out.attributes[0].string_value == "1 2 3 4"
    assert out.children[0].string_value == "1,2,3,4"


def test_a_map_in_simple_content_is_still_a_function_item_error() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <out avt="{map{'a': 1}}"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(source, initial_template="main")) == "FOTY0013"


# ---------------------------------------------------------------------------
# Function items and declared-function conversion


def test_declared_function_accepts_a_sequence_argument() -> None:
    source = stylesheet(
        """
        <xsl:function name="my:total" as="xs:integer">
          <xsl:param name="offset" as="xs:integer"/>
          <xsl:param name="values" as="xs:integer*"/>
          <xsl:param name="more" as="xs:integer*"/>
          <xsl:sequence select="$offset + sum($values) + sum($more)"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:sequence select="my:total(0, 1 to 4, 5 to 6)"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(21),
    )


def test_partial_applications_keep_independent_closures_and_arity() -> None:
    source = stylesheet(
        """
        <xsl:function name="my:scale" as="xs:integer">
          <xsl:param name="factor" as="xs:integer"/>
          <xsl:param name="n" as="xs:integer"/>
          <xsl:sequence select="$factor * $n"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:variable name="double" select="my:scale(2, ?)"/>
          <xsl:variable name="triple" select="my:scale(3, ?)"/>
          <xsl:sequence select="function-arity($double), function-arity($triple),
            $double(10), $triple(10), $double(4)"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(1),
        XdmAtomic.integer(20),
        XdmAtomic.integer(30),
        XdmAtomic.integer(8),
    )


def test_inline_function_items_keep_independent_focus_and_body() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:for-each select="(10, 100)">
            <xsl:variable name="base" select="."/>
            <xsl:variable name="add" select="function($x) { $x + $base }"/>
            <xsl:sequence select="$add(1)"/>
          </xsl:for-each>
          <xsl:variable name="increment" select="function($x) { $x + 1 }"/>
          <xsl:variable name="double" select="function($x) { $x * 2 }"/>
          <xsl:sequence select="$increment(10), $double(10), $increment(10)"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(11),
        XdmAtomic.integer(101),
        XdmAtomic.integer(11),
        XdmAtomic.integer(20),
        XdmAtomic.integer(11),
    )


def test_function_items_report_names_and_arity() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="absf" as="function(*)" select="abs#1"/>
          <xsl:sequence select="local-name-from-QName(function-name($absf)),
            function-arity($absf), $absf(-2)"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.string("abs"),
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
    )


def test_anonymous_inline_function_item_reports_arity_and_no_name() -> None:
    source = stylesheet(
        """
        <xsl:function name="my:adder" as="function(*)">
          <xsl:param name="offset" as="xs:integer"/>
          <xsl:sequence select="function($x) { $x + $offset }"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:variable name="f" select="my:adder(5)"/>
          <xsl:sequence select="function-arity($f), empty(function-name($f)), $f(1)"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(1),
        XdmAtomic.boolean(True),
        XdmAtomic.integer(6),
    )


def test_declared_function_may_use_a_reserved_local_name() -> None:
    """A namespace disambiguates a reserved XPath function name.

    ``switch``/``typeswitch`` are reserved names with no parser token, so a
    stylesheet function in a namespace can use them.  A name that also has a
    parser token (``node``, ``function``) still collides with that token; see
    the task report for that narrower residual.
    """
    source = stylesheet(
        """
        <xsl:function name="my:switch" as="xs:string">
          <xsl:sequence select="'switch-function'"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:sequence select="my:switch()"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.string("switch-function"),
    )


def test_date_time_in_xslt10_compatibility_uses_the_first_argument() -> None:
    source = stylesheet(
        """
        <xsl:template name="main" version="1.0">
          <xsl:sequence select="string(dateTime(
            (xs:date('1999-11-11'), xs:date('2006-05-11')),
            (xs:time('01:02:03'), xs:time('22:22:22'))))"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.string("1999-11-11T01:02:03"),
    )


def test_date_time_rejects_a_sequence_argument_outside_compatibility_mode() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="dateTime(
            (xs:date('1999-11-11'), xs:date('2006-05-11')),
            xs:time('01:02:03'))"/>
        </xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(source, initial_template="main", delivery="raw")) == "XPTY0004"
    )


def test_date_time_conversion_rejects_wrong_argument_types() -> None:
    wrong_atomic = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="dateTime(xs:integer(1), xs:time('01:02:03'))"/>
        </xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(wrong_atomic, initial_template="main", delivery="raw"))
        == "XPTY0004"
    )

    function_item = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="dateTime(function() { 1 }, xs:time('01:02:03'))"/>
        </xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(function_item, initial_template="main", delivery="raw"))
        == "XPTY0004"
    )

    invalid_lexical = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="dateTime(/*/@d, /*/t)"/>
        </xsl:template>
        """
    )
    assert (
        error_code(
            lambda: transform(
                invalid_lexical,
                b'<doc d="not-a-date"><t>01:02:03</t></doc>',
                initial_template="main",
                delivery="raw",
            )
        )
        == "FORG0001"
    )


def test_date_time_argument_conversion_covers_every_operand_shape() -> None:
    class Token:
        def error(self, code: str, message: str) -> None:
            raise ValueError(code)

    class Node:
        def __init__(self, typed: tuple[Any, ...]) -> None:
            self.typed_value = typed

    class Adapter:
        def __init__(self, typed: tuple[Any, ...]) -> None:
            self.xdm = Node(typed)

    token = Token()
    first = Date.fromstring("2020-01-01")
    assert _date_time_argument(token, first, Date) is first
    typed_date = XdmAtomic(first, f"{{{XSD_NS}}}date", Date)
    assert _date_time_argument(token, Adapter((typed_date,)), Date) is first
    assert _date_time_argument(token, XdmAtomic.untyped_atomic("2020-01-01"), Date) == first
    assert _date_time_argument(token, UntypedAtomic("2020-01-01"), Date) == first

    for argument in (Adapter(()), Adapter((typed_date, typed_date))):
        with pytest.raises(ValueError, match="XPTY0004"):
            _date_time_argument(token, argument, Date)
    with pytest.raises(ValueError, match="XPTY0004"):
        _date_time_argument(token, Adapter((XdmAtomic.integer(1),)), Date)
    with pytest.raises(ValueError, match="FORG0001"):
        _date_time_argument(token, XdmAtomic.untyped_atomic("2020-01-01"), Time)
    with pytest.raises(ValueError, match="XPTY0004"):
        _date_time_argument(token, object(), Date)
    with pytest.raises(ValueError, match="FORG0001"):
        _date_time_argument(token, XdmAtomic.untyped_atomic("not-a-date"), Date)


def test_deep_equal_with_a_collation_uses_the_substrate_rule() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="deep-equal(
            (1, 2), (1, 2),
            'http://www.w3.org/2005/xpath-functions/collation/codepoint')"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.boolean(True),
    )


def test_date_time_returns_empty_for_an_empty_operand() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="empty(dateTime((), xs:time('01:02:03'))),
            empty(dateTime(xs:date('2020-01-01'), ())),
            empty(dateTime((), ())),
            empty(dateTime(/*/@missing, /*/t))"/>
        </xsl:template>
        """
    )
    assert transform(
        source, b"<doc><t>01:02:03</t></doc>", initial_template="main", delivery="raw"
    ).principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
    )


def test_function_item_arity_helper_accepts_every_nargs_shape() -> None:
    class Token:
        def __init__(self, nargs: Any, count: int) -> None:
            self.nargs = nargs
            self._count = count

        def __len__(self) -> int:
            return self._count

    assert _function_item_arity(Token((1, 2), 0)) == (1, 2)  # type: ignore[arg-type]
    assert _function_item_arity(Token(2, 0)) == 2  # type: ignore[arg-type]
    assert _function_item_arity(Token(None, 3)) == 3  # type: ignore[arg-type]


def test_function_item_keeps_a_variadic_core_function_arity() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="f" select="function-lookup(
            QName('http://www.w3.org/2005/xpath-functions', 'key'), 2)"/>
          <xsl:sequence select="function-arity($f)"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(2),
    )


def test_date_time_constructor_keeps_the_one_argument_form() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="string(xs:dateTime('2020-01-01T00:00:00'))"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main", delivery="raw").principal == (
        XdmAtomic.string("2020-01-01T00:00:00"),
    )


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
