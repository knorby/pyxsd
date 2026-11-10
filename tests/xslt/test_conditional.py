"""Conditional instructions, conditional content, and forward fallback.

Every stylesheet is inline and every assertion is behavioral: no test reads the
pinned corpus, the clock, the filesystem, or a fixed element ordering where the
specification leaves ordering open.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from pyxsd.xdm import XdmArray, XdmAtomic, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.runtime.instructions import is_deemed_empty, is_vacuous

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"


def stylesheet(body: str, *, version: str = "3.0") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="urn:my" version="{version}">{body}</xsl:stylesheet>'
    ).encode()


def compiled(source: bytes):
    return XsltProcessor().compile(io.BytesIO(source))


def transform(
    source: bytes,
    input_source: bytes | None = None,
    *,
    delivery: str = "raw",
    **options: Any,
):
    stylesheet_ = compiled(source)
    if input_source is None:
        return stylesheet_.transform(delivery=delivery, **options)
    return stylesheet_.transform(source=input_source, delivery=delivery, **options)


def error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def principal_strings(body: str, **options: Any) -> tuple[str, ...]:
    result = transform(stylesheet(body), initial_template="main", **options)
    return tuple(item.lexical for item in result.principal if isinstance(item, XdmAtomic))


# ---------------------------------------------------------------------------
# xsl:if and xsl:choose


def test_if_selects_content_with_the_effective_boolean_value() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:if test="true()"><xsl:sequence select="'yes'"/></xsl:if>
          <xsl:if test="false()"><xsl:sequence select="'no'"/></xsl:if>
          <xsl:if test="()"><xsl:sequence select="'empty'"/></xsl:if>
          <xsl:if test="'0'"><xsl:sequence select="'string'"/></xsl:if>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("yes"),
        XdmAtomic.string("string"),
    )


def test_if_does_not_evaluate_unselected_content() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:if test="false()">
            <xsl:sequence select="1 div 0"/>
          </xsl:if>
          <xsl:sequence select="'safe'"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("safe"),)


def test_choose_evaluates_only_the_first_satisfied_branch() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:choose>
            <xsl:when test="false()"><xsl:sequence select="1 div 0"/></xsl:when>
            <xsl:when test="true()"><xsl:sequence select="'one'"/></xsl:when>
            <xsl:when test="true()"><xsl:sequence select="'two'"/></xsl:when>
            <xsl:otherwise><xsl:sequence select="'other'"/></xsl:otherwise>
          </xsl:choose>
          <xsl:choose>
            <xsl:when test="false()"><xsl:sequence select="'no'"/></xsl:when>
            <xsl:otherwise><xsl:sequence select="'other'"/></xsl:otherwise>
          </xsl:choose>
          <xsl:choose>
            <xsl:when test="false()"><xsl:sequence select="'no'"/></xsl:when>
          </xsl:choose>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("one"),
        XdmAtomic.string("other"),
    )


def test_if_requires_a_test_attribute() -> None:
    source = stylesheet(
        """
        <xsl:template name="main"><xsl:if>content</xsl:if></xsl:template>
        """
    )
    assert error_code(lambda: compiled(source)) == "XTSE0010"


# ---------------------------------------------------------------------------
# xsl:where-populated


def test_where_populated_discards_deemed_empty_items() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:where-populated><a/></xsl:where-populated>
          <xsl:where-populated><xsl:sequence select="''"/></xsl:where-populated>
          <xsl:where-populated><xsl:sequence select="map{}"/></xsl:where-populated>
          <xsl:where-populated><xsl:sequence select="[]"/></xsl:where-populated>
          <xsl:where-populated><xsl:sequence select="'kept'"/></xsl:where-populated>
          <xsl:where-populated><a>x</a></xsl:where-populated>
          <xsl:where-populated><xsl:sequence select="map{'a': 1}"/></xsl:where-populated>
          <xsl:where-populated><xsl:sequence select="['x']"/></xsl:where-populated>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    items = result.principal
    assert [type(item).__name__ for item in items] == [
        "XdmAtomic",
        "XdmNode",
        "XdmMap",
        "XdmArray",
    ]
    assert isinstance(items[0], XdmAtomic)
    assert items[0] == XdmAtomic.string("kept")
    assert isinstance(items[1], XdmNode)
    assert items[1].name == "a"
    assert isinstance(items[2], XdmMap)
    assert items[2].size == 1
    assert isinstance(items[3], XdmArray)
    assert items[3].size == 1


def test_where_populated_keeps_a_document_whose_child_is_empty() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="tree"><a/></xsl:variable>
          <xsl:where-populated>
            <xsl:sequence select="$tree"/>
          </xsl:where-populated>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert len(result.principal) == 1
    document = result.principal[0]
    assert isinstance(document, XdmDocument)
    assert document.root_element is not None
    assert document.root_element.name == "a"


def test_where_populated_rejects_a_select_attribute() -> None:
    source = stylesheet(
        """
        <xsl:template name="main"><xsl:where-populated select="1"/></xsl:template>
        """
    )
    assert error_code(lambda: compiled(source)) == "XTSE0010"


def test_where_populated_uses_on_empty_and_on_non_empty_content() -> None:
    empty = stylesheet(
        """
        <xsl:template name="main">
          <xsl:where-populated>
            <xsl:sequence select="()"/>
            <xsl:on-empty select="'empty'"/>
          </xsl:where-populated>
        </xsl:template>
        """
    )
    assert transform(empty, initial_template="main").principal == (XdmAtomic.string("empty"),)

    populated = stylesheet(
        """
        <xsl:template name="main">
          <xsl:where-populated>
            <xsl:on-non-empty select="'header'"/>
            <xsl:sequence select="'body'"/>
            <xsl:on-non-empty select="'footer'"/>
          </xsl:where-populated>
        </xsl:template>
        """
    )
    assert transform(populated, initial_template="main").principal == (
        XdmAtomic.string("header"),
        XdmAtomic.string("body"),
        XdmAtomic.string("footer"),
    )


# ---------------------------------------------------------------------------
# xsl:on-empty and xsl:on-non-empty


def test_on_empty_replaces_vacuous_content() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:sequence select="''"/>
            <xsl:sequence select="()"/>
            <xsl:on-empty select="'fallback'"/>
          </xsl:sequence>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("fallback"),)


def test_on_empty_is_ignored_when_any_item_is_not_vacuous() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:sequence select="''"/>
            <a/>
            <xsl:on-empty select="'fallback'"/>
          </xsl:sequence>
        </xsl:template>
        """
    )
    result = transform(source, initial_template="main")
    assert len(result.principal) == 2
    assert result.principal[0] == XdmAtomic.string("")
    assert isinstance(result.principal[1], XdmNode)
    assert result.principal[1].name == "a"


def test_on_empty_alone_always_runs() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence><xsl:on-empty select="'only'"/></xsl:sequence>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("only"),)


def test_on_non_empty_is_omitted_without_ordinary_content() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-non-empty select="'header'"/>
            <xsl:sequence select="''"/>
          </xsl:sequence>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string(""),)


def test_on_non_empty_waits_for_the_first_ordinary_non_vacuous_item() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-non-empty select="'before'"/>
            <xsl:sequence select="''"/>
            <xsl:on-non-empty select="'middle'"/>
            <xsl:sequence select="'body'"/>
            <xsl:on-non-empty select="'after'"/>
          </xsl:sequence>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string(""),
        XdmAtomic.string("before"),
        XdmAtomic.string("middle"),
        XdmAtomic.string("body"),
        XdmAtomic.string("after"),
    )


def test_on_non_empty_uses_the_scope_at_its_own_position() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:variable name="before" select="'before'"/>
            <xsl:on-non-empty select="$before"/>
            <xsl:sequence select="'body'"/>
            <xsl:variable name="after" select="'after'"/>
          </xsl:sequence>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("before"),
        XdmAtomic.string("body"),
    )


def test_on_empty_applies_inside_a_literal_result_element() -> None:
    result = transform(
        stylesheet(
            """
            <xsl:template name="main">
              <out>
                <xsl:sequence select="''"/>
                <xsl:on-empty select="'fallback'"/>
              </out>
            </xsl:template>
            """
        ),
        initial_template="main",
        delivery="document",
    )
    assert result.principal_tree.root_element is not None
    assert result.principal_tree.root_element.string_value == "fallback"


def test_on_empty_applies_inside_a_variable_value() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="value">
            <xsl:sequence select="''"/>
            <xsl:on-empty select="'fallback'"/>
          </xsl:variable>
          <xsl:sequence select="string($value)"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("fallback"),)


def test_on_empty_and_on_non_empty_value_forms() -> None:
    result = transform(
        stylesheet(
            """
            <xsl:template name="main">
              <xsl:sequence>
                <xsl:sequence select="''"/>
                <xsl:on-empty><xsl:text>empty-content</xsl:text></xsl:on-empty>
              </xsl:sequence>
              <xsl:sequence>
                <xsl:on-non-empty><xsl:text>header-content</xsl:text></xsl:on-non-empty>
                <xsl:sequence select="'body'"/>
              </xsl:sequence>
            </xsl:template>
            """
        ),
        initial_template="main",
        delivery="document",
    )
    assert result.principal_tree.string_value == "empty-contentheader-contentbody"


@pytest.mark.parametrize(
    "body",
    [
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-empty select="1"/>
            <xsl:on-empty select="2"/>
          </xsl:sequence>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-empty select="1"/>
            <xsl:value-of select="'after'"/>
          </xsl:sequence>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-empty select="1"/>
            <a/>
          </xsl:sequence>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-empty select="1"/>
            <xsl:text>after</xsl:text>
          </xsl:sequence>
        </xsl:template>
        """,
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-empty select="1"/>
            <xsl:on-non-empty select="2"/>
          </xsl:sequence>
        </xsl:template>
        """,
    ],
)
def test_on_empty_placement_constraints(body: str) -> None:
    assert error_code(lambda: compiled(stylesheet(body))) == "XTSE0010"


def test_on_empty_select_and_content_are_mutually_exclusive() -> None:
    for element in ("on-empty", "on-non-empty"):
        source = stylesheet(
            f"""
            <xsl:template name="main">
              <xsl:sequence>
                <xsl:{element} select="1">content</xsl:{element}>
              </xsl:sequence>
            </xsl:template>
            """
        )
        assert error_code(lambda source=source: compiled(source)) == "XTSE3185"


def test_on_empty_may_be_followed_by_fallback() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-empty select="'empty'"/>
            <xsl:fallback><xsl:sequence select="'ignored'"/></xsl:fallback>
          </xsl:sequence>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("empty"),)


# ---------------------------------------------------------------------------
# Forward-compatible fallback


def test_fallback_is_ignored_outside_fallback_processing() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:fallback><xsl:sequence select="1 div 0"/></xsl:fallback>
          <xsl:sequence select="'safe'"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("safe"),)


def test_unknown_instruction_uses_fallback_in_a_later_version() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:future-instruction>
            <xsl:sequence select="1 div 0"/>
            <xsl:fallback><xsl:sequence select="'first'"/></xsl:fallback>
            <xsl:fallback><xsl:sequence select="'second'"/></xsl:fallback>
            <xsl:sequence select="1 div 0"/>
          </xsl:future-instruction>
        </xsl:template>
        """,
        version="4.0",
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("first"),
        XdmAtomic.string("second"),
    )


def test_unknown_instruction_without_fallback_is_a_static_error() -> None:
    unsupported = stylesheet(
        """
        <xsl:template name="main"><xsl:future-instruction/></xsl:template>
        """,
        version="4.0",
    )
    assert error_code(lambda: compiled(unsupported)) == "XTSE0010"

    three = stylesheet(
        """
        <xsl:template name="main">
          <xsl:future-instruction>
            <xsl:fallback><xsl:sequence select="'fallback'"/></xsl:fallback>
          </xsl:future-instruction>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(three)) == "XTSE0010"


def test_nested_fallback_processing_in_a_later_version() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:outer-future>
            <xsl:fallback>
              <xsl:inner-future>
                <xsl:fallback><xsl:sequence select="'inner'"/></xsl:fallback>
              </xsl:inner-future>
            </xsl:fallback>
          </xsl:outer-future>
        </xsl:template>
        """,
        version="4.0",
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("inner"),)


def test_known_but_unimplemented_instructions_keep_explicit_errors() -> None:
    source = stylesheet(
        """
        <xsl:template name="main"><xsl:message select="'hello'"/></xsl:template>
        """
    )
    with pytest.raises(XsltError, match="not supported"):
        transform(source, initial_template="main")


# ---------------------------------------------------------------------------
# xsl:assert


def assert_transform(source: bytes, **options: Any):
    stylesheet_ = XsltProcessor(static_options={"assertions": True}).compile(io.BytesIO(source))
    return stylesheet_.transform(initial_template="main", delivery="raw", **options)


def test_assertions_are_disabled_by_default() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="false()" select="'disabled'"/>
          <xsl:sequence select="'after'"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("after"),)


def test_assertions_pass_and_return_the_empty_sequence() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="1 + 1 = 2"/>
          <xsl:sequence select="'after'"/>
        </xsl:template>
        """
    )
    assert assert_transform(source).principal == (XdmAtomic.string("after"),)


def test_assertion_failure_has_a_useful_diagnostic_and_default_code() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="1 = 2" select="'one does not equal two'"/>
          <xsl:sequence select="'unreachable'"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9001"
    assert "assertion failed" in str(caught.value)
    assert "one does not equal two" in str(caught.value)
    assert "1 = 2" in str(caught.value)


def test_assertion_message_content_and_custom_error_code() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="false()" error-code="my:bad">
            <xsl:text>constructed message</xsl:text>
          </xsl:assert>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "{urn:my}bad"
    assert "constructed message" in str(caught.value)


def test_assertion_test_error_is_reported_with_the_assertion_code() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="1 div 0" select="'division failed'"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9001"
    assert "division failed" in str(caught.value)
    assert caught.value.cause is not None
    assert str(getattr(caught.value.cause, "code", None)).endswith("FOAR0001")


def test_assertion_in_an_unselected_branch_is_not_evaluated() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:if test="false()"><xsl:assert test="false()"/></xsl:if>
          <xsl:sequence select="'safe'"/>
        </xsl:template>
        """
    )
    assert assert_transform(source).principal == (XdmAtomic.string("safe"),)


def test_assert_requires_a_test_attribute() -> None:
    source = stylesheet(
        """
        <xsl:template name="main"><xsl:assert select="'message'"/></xsl:template>
        """
    )
    assert error_code(lambda: compiled(source)) == "XTSE0010"


def test_assertion_failure_without_a_message_still_names_the_test() -> None:
    source = stylesheet(
        """
        <xsl:template name="main"><xsl:assert test="false()"/></xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9001"
    assert "assertion failed" in str(caught.value)
    assert "evaluated to false" in str(caught.value)


def test_assertion_error_code_in_the_standard_error_namespace_is_normalized() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="false()"
                      select="'message'"
                      error-code="err:XTMM9002"
                      xmlns:err="http://www.w3.org/2005/xqt-errors"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9002"


def test_on_empty_preserves_leading_parameter_bindings() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:param name="value" select="2"/>
          <xsl:sequence select="$value"/>
          <xsl:on-empty select="3"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(2),)


def test_parameter_after_ordinary_conditional_content_is_a_dynamic_error() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="1"/>
          <xsl:param name="value" select="2"/>
          <xsl:on-empty select="3"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(source, initial_template="main")) == "XTDE0000"


# ---------------------------------------------------------------------------
# Value predicates


def test_vacuous_and_deemed_empty_follow_the_specification() -> None:
    empty_document = XdmDocument.parse(b"<doc/>")
    element = empty_document.root_element
    assert element is not None

    text = XdmDocument.parse(b"<doc> </doc>").root_element
    assert text is not None
    whitespace = text.children[0]
    assert whitespace.kind is NodeKind.TEXT
    assert is_vacuous(whitespace) is False
    assert is_deemed_empty(whitespace) is False

    empty_text_source = XdmDocument.parse(b"<doc><n/></doc>").root_element
    assert empty_text_source is not None
    assert is_deemed_empty(empty_text_source.children[0]) is True

    comment = XdmDocument.parse(b"<doc><!----></doc>").root_element
    assert comment is not None
    empty_comment = comment.children[0]
    assert empty_comment.kind is NodeKind.COMMENT
    assert is_vacuous(empty_comment) is False
    assert is_deemed_empty(empty_comment) is True

    assert is_vacuous(XdmAtomic.string("")) is True
    assert is_vacuous(XdmAtomic.decimal(0)) is False
    assert is_vacuous(XdmMap.empty()) is False
    assert is_deemed_empty(XdmMap.empty()) is True
    assert is_vacuous(XdmArray([()])) is True
    assert is_deemed_empty(XdmArray([()])) is True
    assert is_vacuous(XdmArray([(XdmAtomic.string(""),)])) is True
    assert is_vacuous(XdmArray([(XdmAtomic.integer(1),)])) is False
    assert is_deemed_empty(XdmArray([(XdmAtomic.integer(1),)])) is False
    assert is_deemed_empty(XdmMap.from_mapping({"a": 1})) is False


# ---------------------------------------------------------------------------
# Fix round 1: fallback-aware mutual exclusion


def test_map_entry_select_permits_fallback_children() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="m" as="map(*)">
            <xsl:map>
              <xsl:map-entry key="'a'" select="1"><xsl:fallback/></xsl:map-entry>
            </xsl:map>
          </xsl:variable>
          <xsl:sequence select="$m('a')"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(1),)


def test_on_empty_select_permits_fallback_children() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-empty select="'empty'"><xsl:fallback/></xsl:on-empty>
          </xsl:sequence>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.string("empty"),)


def test_on_non_empty_select_permits_fallback_children() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-non-empty select="'header'"><xsl:fallback/></xsl:on-non-empty>
            <xsl:sequence select="'body'"/>
          </xsl:sequence>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("header"),
        XdmAtomic.string("body"),
    )


def test_sequence_select_permits_fallback_children() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="1"><xsl:fallback/></xsl:sequence>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(1),)


def test_sequence_select_with_content_uses_the_sequence_error_code() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="1"><xsl:sequence select="2"/></xsl:sequence>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(source)) == "XTSE3185"


def test_fallback_only_content_does_not_hide_real_content_errors() -> None:
    map_entry = stylesheet(
        """
        <xsl:template name="main">
          <xsl:map>
            <xsl:map-entry key="'a'" select="1"><xsl:sequence select="2"/></xsl:map-entry>
          </xsl:map>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(map_entry)) == "XTSE3280"

    on_empty = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:on-empty select="'empty'"><xsl:sequence select="2"/></xsl:on-empty>
          </xsl:sequence>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(on_empty)) == "XTSE3185"


# ---------------------------------------------------------------------------
# Fix round 1: assertion error-code EQName handling


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "XTMM9001"),
        ("not a qname", "XTMM9001"),
        ("missing:bad", "XTMM9001"),
        ("a:b:c", "XTMM9001"),
        ("Q{{urn:my}}bad", "{urn:my}bad"),
        ("Q{{urn:my}}1bad", "XTMM9001"),
        ("Q{{bad", "XTMM9001"),
        ("Q{{http://www.w3.org/2005/xqt-errors}}XTMM9002", "XTMM9002"),
        ("Q{{}}plain", "plain"),
        ("{{urn:my}}bad", "XTMM9001"),
        ("{{urn:my}}1bad", "XTMM9001"),
        ("{{urn:my", "XTMM9001"),
        ("{{}}plain", "XTMM9001"),
        ("XTMM9002", "XTMM9002"),
    ],
)
def test_assertion_error_code_is_an_eqname_or_the_default(value: str, expected: str) -> None:
    source = stylesheet(
        f"""
        <xsl:template name="main">
          <xsl:assert test="false()" select="'message'" error-code="{value}"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == expected


# ---------------------------------------------------------------------------
# Fix round 1: assertion message construction failures


def test_assertion_select_failure_becomes_the_assertion_diagnostic() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="false()" select="1 div 0"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9001"
    assert "message construction failed" in str(caught.value)
    assert caught.value.cause is not None
    assert str(getattr(caught.value.cause, "code", None)).endswith("FOAR0001")


def test_assertion_content_failure_becomes_the_assertion_diagnostic() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="false()">
            <xsl:sequence select="1 div 0"/>
          </xsl:assert>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9001"
    assert "message construction failed" in str(caught.value)
    assert caught.value.cause is not None
    assert str(getattr(caught.value.cause, "code", None)).endswith("FOAR0001")


def test_assertion_message_tree_failure_becomes_the_assertion_diagnostic() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="false()" select="true#0"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9001"
    assert "message construction failed" in str(caught.value)
    assert caught.value.cause is not None


def test_assertion_message_failure_keeps_a_custom_error_code() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="false()" select="1 div 0" error-code="my:bad"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "{urn:my}bad"
    assert caught.value.cause is not None
    assert str(getattr(caught.value.cause, "code", None)).endswith("FOAR0001")


def test_assertion_error_code_failure_keeps_a_message_failure() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="false()" select="1 div 0" error-code="{1 div 0}"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9001"
    assert "message construction failed" in str(caught.value)
    assert caught.value.cause is not None
    assert str(getattr(caught.value.cause, "code", None)).endswith("FOAR0001")


def test_assertion_error_code_failure_keeps_a_successful_message() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="false()" select="'message'" error-code="{1 div 0}"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9001"
    assert "message" in str(caught.value)
    assert "message construction failed" not in str(caught.value)
    assert caught.value.cause is not None


def test_assertion_error_code_failure_without_a_message_reports_it() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="false()" error-code="{1 div 0}"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9001"
    assert "message construction failed" in str(caught.value)
    assert caught.value.cause is not None


def test_assertion_error_code_rejects_a_prefix_bound_to_an_empty_namespace() -> None:
    from pyxsd.xslt.processor import _assertion_error_code

    assert _assertion_error_code("p:bad", {"p": ""}) == "XTMM9001"


def test_assertion_test_failure_takes_cause_precedence_over_error_code_failure() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:assert test="1 div 0" error-code="{xs:integer('bad')}"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        assert_transform(source)
    assert caught.value.code == "XTMM9001"
    assert "message construction failed" in str(caught.value)
    assert caught.value.cause is not None
    assert str(getattr(caught.value.cause, "code", None)).endswith("FOAR0001")
