"""XSLT-defined XPath functions and per-evaluation dynamic context.

These tests are corpus-independent.  They pin the bridge between the XSLT
runtime and XPath evaluation: the instruction current item, direct versus
dynamic function-item calls, group/merge/regex/accumulator state contracts,
documents, keys, system properties, function availability, formatting state,
stable date/time values, and invocation isolation.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic, XdmSequence
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, XPathStaticContext
from pyxsd.xpath.functions import ContextFunction
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.packages import PackageResolver
from pyxsd.xslt.errors import XsltError, XsltStaticError
from pyxsd.xslt.runtime.functions import (
    ABSENT,
    XsltContextBridge,
    xslt_function_declarations,
)
from pyxsd.xslt.runtime.transformation import Transformation

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
FN_NS = "http://www.w3.org/2005/xpath-functions"
MY_NS = "urn:my"


def stylesheet(body: str, *, version: str = "3.0", attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="{version}"{attributes}>{body}</xsl:stylesheet>'
    ).encode()


def transform(
    source: bytes,
    input_source: bytes | None = None,
    *,
    initial_template: str | None = None,
    delivery: str = "raw",
    **options: Any,
):
    compiled = XsltProcessor().compile(io.BytesIO(source))
    arguments: dict[str, Any] = dict(delivery=delivery, **options)
    if initial_template is not None:
        arguments["initial_template"] = initial_template
    if input_source is not None:
        arguments["source"] = input_source
    return compiled.transform(**arguments)


def error_code(call: Callable[[], object]) -> str | None:
    with pytest.raises(Exception) as caught:
        call()
    return str(getattr(caught.value, "code", "")).rsplit(":", 1)[-1]


def bridge(**state: Any) -> XsltContextBridge:
    transformation = Transformation()
    return XsltContextBridge(transformation, **state)


def _document() -> XdmDocument:
    return XdmDocument.parse(b"<doc><item id='a'>one</item><item id='b'>two</item></doc>")


# ---------------------------------------------------------------------------
# Profile-aware function surface


def test_function_declarations_follow_the_effective_profile() -> None:
    three = xslt_function_declarations("3.0")
    assert three["fn:current"] == 0
    assert three["fn:current-group"] == 0
    assert three["fn:current-merge-group"] == (0, 1)
    assert three["fn:regex-group"] == 1
    assert three["fn:accumulator-before"] == 1
    assert three["fn:available-system-properties"] == 0

    two = xslt_function_declarations("2.0")
    assert two["fn:current"] == 0
    assert two["fn:current-group"] == 0
    assert "fn:accumulator-before" not in two
    assert "fn:current-merge-group" not in two
    assert "fn:available-system-properties" not in two

    with pytest.raises(ValueError, match="version"):
        xslt_function_declarations("4.0")


def test_two_point_zero_modules_compile_legal_functions_only() -> None:
    current = stylesheet(
        """
        <xsl:template name="main"><xsl:sequence select="current()"/></xsl:template>
        """,
        version="2.0",
    )
    result = transform(current, b"<doc/>", initial_template="main")
    assert isinstance(result.principal[0], XdmDocument)

    unsupported = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="accumulator-before('x')"/>
        </xsl:template>
        """,
        version="2.0",
    )
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor().compile(io.BytesIO(unsupported))
    assert caught.value.code == "XPST0017"

    legacy = stylesheet(
        """
        <xsl:key name="by-id" match="item" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="starts-with(generate-id(/*), 'ID')"/>
          <xsl:sequence select="count(key('by-id', 'a'))"/>
        </xsl:template>
        """,
        version="2.0",
    )
    assert transform(legacy, b'<doc><item id="a"/></doc>', initial_template="main").principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.integer(1),
    )


# ---------------------------------------------------------------------------
# current() semantics


def test_current_uses_the_instruction_item_not_predicate_focus() -> None:
    source = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence
            select="string-join(//item[@ref = current()/doc/@target]/@id, ',')"/>
        </xsl:template>
        """
    )
    result = transform(
        source,
        b'<doc target="b"><item id="a" ref="b"/><item id="b" ref="a"/></doc>',
    )
    assert result.principal == (XdmAtomic.string("a"),)


def test_current_follows_template_rule_transitions() -> None:
    source = stylesheet(
        """
        <xsl:template match="/">
          <xsl:apply-templates select="doc/item"/>
        </xsl:template>
        <xsl:template match="item">
          <xsl:sequence
            select="string-join(//item[@ref = current()/@ref]/@id, ',')"/>
        </xsl:template>
        """
    )
    result = transform(
        source,
        b'<doc><item id="a" ref="b"/><item id="b" ref="a"/></doc>',
    )
    assert result.principal == (XdmAtomic.string("a"), XdmAtomic.string("b"))


def test_current_returns_the_entry_item_for_direct_calls() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="string(current())"/>
          <xsl:sequence select="string(current()/doc/item[1]/@id)"/>
        </xsl:template>
        """
    )
    result = transform(
        source,
        b'<doc><item id="a">one</item></doc>',
        initial_template="main",
    )
    assert result.principal == (XdmAtomic.string("one"), XdmAtomic.string("a"))


def test_current_dynamic_invocation_has_absent_context() -> None:
    forms = (
        "current#0()",
        "function-lookup(QName('http://www.w3.org/2005/xpath-functions', 'current'), 0)()",
        "let $f := current#0 return $f()",
        "let $f := function-lookup("
        "QName('http://www.w3.org/2005/xpath-functions', 'current'), 0) return $f()",
    )
    for expression in forms:
        source = stylesheet(
            f"""
            <xsl:template name="main">
              <xsl:sequence select="{expression}"/>
            </xsl:template>
            """
        )
        assert (
            error_code(
                lambda source=source: transform(
                    source,
                    b"<doc/>",
                    initial_template="main",
                )
            )
            == "XTDE1360"
        )


def test_stylesheet_functions_start_with_absent_focus_and_no_caller_locals() -> None:
    current = stylesheet(
        """
        <xsl:function name="my:probe-current">
          <xsl:sequence select="current()"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:variable name="local" select="'caller'"/>
          <xsl:sequence select="my:probe-current()"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(current, b"<doc/>", initial_template="main")) == "XTDE1360"

    focus = stylesheet(
        """
        <xsl:function name="my:probe-focus">
          <xsl:sequence select="name(.)"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:sequence select="my:probe-focus()"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(focus, b"<doc/>", initial_template="main")) == "XPDY0002"

    locals_ = stylesheet(
        """
        <xsl:function name="my:echo" as="xs:string">
          <xsl:param name="value" as="xs:string"/>
          <xsl:sequence select="$value"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:variable name="value" select="'caller'"/>
          <xsl:sequence select="my:echo('argument')"/>
          <xsl:sequence select="$value"/>
        </xsl:template>
        """
    )
    assert transform(locals_, b"<doc/>", initial_template="main").principal == (
        XdmAtomic.string("argument"),
        XdmAtomic.string("caller"),
    )


def test_function_lookup_and_original_function_item_closure(tmp_path: Path) -> None:
    simple = stylesheet(
        """
        <xsl:function name="my:inc" as="xs:integer">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="$value + 1"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:sequence select="my:inc#1(41)"/>
          <xsl:sequence
            select="function-lookup(QName('urn:my', 'inc'), 1)(41)"/>
        </xsl:template>
        """
    )
    assert transform(simple, b"<doc/>", initial_template="main").principal == (
        XdmAtomic.integer(42),
        XdmAtomic.integer(42),
    )

    library = tmp_path / "library.xsl"
    library.write_text(
        f"""<xsl:package name="urn:library" package-version="1.0"
            xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" xmlns:my="{MY_NS}" version="3.0">
          <xsl:function name="my:label" as="xs:string" visibility="public">
            <xsl:param name="value" as="xs:string"/>
            <xsl:sequence select="concat('base:', $value)"/>
          </xsl:function>
        </xsl:package>""",
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        f"""<xsl:package name="urn:principal" package-version="1.0"
            xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" xmlns:my="{MY_NS}" version="3.0">
          <xsl:use-package name="urn:library" package-version="1.0">
            <xsl:override>
              <xsl:function name="my:label" as="xs:string" visibility="public">
                <xsl:param name="value" as="xs:string"/>
                <xsl:variable name="original"
                  select="function($argument) {{ xsl:original($argument) }}"/>
                <xsl:sequence select="concat('over:', $original($value))"/>
              </xsl:function>
            </xsl:override>
          </xsl:use-package>
          <xsl:template name="xsl:initial-template" visibility="public">
            <xsl:sequence select="my:label('x')"/>
          </xsl:template>
        </xsl:package>""",
        encoding="utf-8",
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(principal)
    assert compiled.transform().principal == (XdmAtomic.string("over:base:x"),)


# ---------------------------------------------------------------------------
# Group, merge, regex, and accumulator state contracts


def test_group_and_merge_state_is_absent_by_default() -> None:
    group = stylesheet(
        """
        <xsl:template name="main"><xsl:sequence select="current-group()"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(group, b"<doc/>", initial_template="main")) == "XTDE1061"

    grouping_key = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="current-grouping-key()"/>
        </xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(grouping_key, b"<doc/>", initial_template="main"))
        == "XTDE1071"
    )

    merge_group = stylesheet(
        """
        <xsl:template name="main"><xsl:sequence select="current-merge-group()"/></xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(merge_group, b"<doc/>", initial_template="main")) == "XTDE3480"
    )

    merge_key = stylesheet(
        """
        <xsl:template name="main"><xsl:sequence select="current-merge-key()"/></xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(merge_key, b"<doc/>", initial_template="main")) == "XTDE3510"
    )


def test_group_and_merge_function_items_do_not_retain_state() -> None:
    forms = (
        ("current-group#0()", "XTDE1061"),
        ("current-grouping-key#0()", "XTDE1071"),
        ("current-merge-group#0()", "XTDE3480"),
        ("current-merge-key#0()", "XTDE3510"),
    )
    for expression, expected in forms:
        source = stylesheet(
            f"""
            <xsl:template name="main">
              <xsl:sequence select="{expression}"/>
            </xsl:template>
            """
        )
        assert (
            error_code(
                lambda source=source: transform(
                    source,
                    b"<doc/>",
                    initial_template="main",
                )
            )
            == expected
        )


def test_group_merge_and_regex_state_transitions_are_explicit() -> None:
    document = _document()
    first, second = document.root_element.children
    key = XdmAtomic.string("group")

    grouped = bridge(
        group=(first,),
        grouping_key=(key,),
    )
    assert grouped.current_group() == (first,)
    assert grouped.current_grouping_key() == (key,)
    cleared = grouped.cleared_for_call()
    assert grouped.current_group() == (first,)
    with pytest.raises(XsltError) as caught:
        cleared.current_group()
    assert caught.value.code == "XTDE1061"
    with pytest.raises(XsltError) as caught:
        cleared.current_grouping_key()
    assert caught.value.code == "XTDE1071"

    merged = bridge(
        merge_group={"left": (first,), "right": (second,)},
        merge_sources=("left", "right"),
        merge_key=(key,),
    )
    assert merged.current_merge_group() == (first, second)
    assert merged.current_merge_group("right") == (second,)
    with pytest.raises(XsltError) as caught:
        merged.current_merge_group("missing")
    assert caught.value.code == "XTDE3490"
    assert merged.current_merge_key() == (key,)
    with pytest.raises(XsltError) as caught:
        merged.cleared_for_call().current_merge_group()
    assert caught.value.code == "XTDE3480"
    with pytest.raises(XsltError) as caught:
        merged.cleared_for_call().current_merge_key()
    assert caught.value.code == "XTDE3510"

    regex = bridge(regex_captures=("whole", "capture", ""))
    assert regex.regex_group(0) == "whole"
    assert regex.regex_group(1) == "capture"
    assert regex.regex_group(2) == ""
    assert regex.regex_group(3) == ""
    assert regex.regex_group(-1) == ""
    assert regex.cleared_for_call().regex_group(0) == ""


def test_regex_group_is_empty_outside_analyze_string() -> None:
    direct = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="concat('[', regex-group(0), ']')"/>
          <xsl:sequence select="regex-group#1(1)"/>
        </xsl:template>
        """
    )
    assert transform(direct, b"<doc/>", initial_template="main").principal == (
        XdmAtomic.string("[]"),
        XdmAtomic.string(""),
    )


def test_accumulator_functions_are_explicitly_unavailable() -> None:
    for name in ("accumulator-before", "accumulator-after"):
        source = stylesheet(
            f"""
            <xsl:template name="main">
              <xsl:sequence select="{name}('missing')"/>
            </xsl:template>
            """
        )
        assert (
            error_code(lambda source=source: transform(source, b"<doc/>", initial_template="main"))
            == "XTDE3340"
        )

    dynamic = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="accumulator-before#1('missing')"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(dynamic, b"<doc/>", initial_template="main")) == "XTDE3340"


# ---------------------------------------------------------------------------
# Documents, keys, and identifiers


def test_document_resolves_the_stylesheet_module_and_denies_other_resources() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="count(document('')/xsl:stylesheet)"/>
          <xsl:sequence select="count(document('')/xsl:stylesheet/xsl:template)"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(1),
    )

    denied = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="document('urn:missing')"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(denied, b"<doc/>", initial_template="main")) == "FODC0002"

    file_denied = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="document('file:///etc/passwd')"/>
        </xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(file_denied, b"<doc/>", initial_template="main")) == "FODC0002"
    )

    bad_node = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="document('urn:missing', 1)"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(bad_node, b"<doc/>", initial_template="main")) == "XPTY0004"


def test_keys_use_expression_and_explicit_document_arguments() -> None:
    source = stylesheet(
        """
        <xsl:key name="by-id" match="item" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="string-join(key('by-id', 'b')/@id, ',')"/>
          <xsl:sequence
            select="string-join(key('by-id', ('a', 'b'), /*)/@id, ',')"/>
        </xsl:template>
        """
    )
    assert transform(
        source, b'<doc><item id="a"/><item id="b"/></doc>', initial_template="main"
    ).principal == (
        XdmAtomic.string("b"),
        XdmAtomic.string("a,b"),
    )

    absent = stylesheet(
        """
        <xsl:key name="by-id" match="item" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="key('by-id', 'b')"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(absent, initial_template="main")) == "XTDE1270"


def test_unparsed_entity_uri_checks_context_and_returns_empty_uri() -> None:
    absent = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="unparsed-entity-uri('entity')"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(absent, initial_template="main")) == "XTDE1370"

    contextual = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="string(unparsed-entity-uri('entity', /*))"/>
        </xsl:template>
        """
    )
    assert transform(contextual, b"<doc/>", initial_template="main").principal == (
        XdmAtomic.string(""),
    )


# ---------------------------------------------------------------------------
# System properties and availability


def test_system_properties_and_availability() -> None:
    source = stylesheet(
        """
        <xsl:function name="my:inc" as="xs:integer">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="$value + 1"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:sequence select="system-property('xsl:version')"/>
          <xsl:sequence select="system-property('xsl:missing')"/>
          <xsl:sequence select="function-available('fn:current')"/>
          <xsl:sequence select="function-available('fn:current', 0)"/>
          <xsl:sequence select="function-available('fn:current', 1)"/>
          <xsl:sequence select="function-available('my:inc', 1)"/>
          <xsl:sequence select="function-available('my:missing')"/>
          <xsl:sequence select="function-available('accumulator-before')"/>
          <xsl:sequence select="element-available('xsl:template')"/>
          <xsl:sequence select="element-available('xsl:not-real')"/>
          <xsl:sequence select="element-available('my:instruction')"/>
          <xsl:sequence select="type-available('xs:string')"/>
          <xsl:sequence select="type-available('xs:not-real')"/>
          <xsl:sequence
            select="some $name in available-system-properties()
                    satisfies local-name-from-QName($name) = 'version'"/>
        </xsl:template>
        """
    )
    assert transform(source, b"<doc/>", initial_template="main").principal == (
        XdmAtomic.string("3.0"),
        XdmAtomic.string(""),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(True),
    )


def test_availability_functions_report_invalid_eqnames() -> None:
    invalid = {
        "system-property('missing:name')": "XTDE1390",
        "function-available('missing:name')": "XTDE1400",
        "type-available('missing:name')": "XTDE1428",
        "element-available('missing:name')": "XTDE1440",
    }
    for expression, expected in invalid.items():
        source = stylesheet(
            f"""
            <xsl:template name="main">
              <xsl:sequence select="{expression}"/>
            </xsl:template>
            """
        )
        assert (
            error_code(lambda source=source: transform(source, b"<doc/>", initial_template="main"))
            == expected
        )


def test_two_point_zero_availability_rejects_three_point_zero_functions() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="function-available('accumulator-before')"/>
          <xsl:sequence select="function-available('current-merge-group')"/>
          <xsl:sequence select="function-available('regex-group')"/>
        </xsl:template>
        """,
        version="2.0",
    )
    assert transform(source, b"<doc/>", initial_template="main").principal == (
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(True),
    )


# ---------------------------------------------------------------------------
# Formatting and stable date/time


def test_formatting_uses_package_decimal_formats_and_stable_time() -> None:
    source = stylesheet(
        """
        <xsl:decimal-format name="my:dec" decimal-separator=","
          grouping-separator="."/>
        <xsl:template name="main">
          <xsl:sequence select="format-number(1234.5, '#,##0.00')"/>
          <xsl:sequence select="format-number(1234.5, '#.##0,00', 'my:dec')"/>
          <xsl:sequence select="format-date(current-date(), '[Y]')"/>
          <xsl:sequence select="string(current-dateTime())"/>
          <xsl:sequence select="string(current-dateTime())"/>
          <xsl:sequence select="string(implicit-timezone())"/>
          <xsl:sequence select="string(implicit-timezone())"/>
        </xsl:template>
        """
    )
    values = transform(source, b"<doc/>", initial_template="main").principal
    assert [value.lexical for value in values[:3]] == ["1,234.50", "1.234,50", values[2].lexical]
    assert len(values[2].lexical) == 4
    assert values[3] == values[4]
    assert values[5] == values[6]


# ---------------------------------------------------------------------------
# Invocation isolation


def test_nested_and_independent_evaluations_do_not_leak_state() -> None:
    source = stylesheet(
        """
        <xsl:key name="by-id" match="item" use="@id"/>
        <xsl:function name="my:date" as="xs:string">
          <xsl:sequence select="string(current-dateTime())"/>
        </xsl:function>
        <xsl:template match="/">
          <xsl:sequence select="string-join(key('by-id', doc/@target)/@id, ',')"/>
          <xsl:sequence select="string(current()/doc/@name)"/>
          <xsl:sequence select="generate-id(/*/item[1])"/>
          <xsl:sequence select="my:date()"/>
          <xsl:sequence select="string(current-dateTime())"/>
        </xsl:template>
        """
    )
    compiled = XsltProcessor().compile(io.BytesIO(source))
    first = compiled.transform(
        source=b'<doc name="one" target="b"><item id="a"/><item id="b"/></doc>',
    )
    second = compiled.transform(
        source=b'<doc name="two" target="a"><item id="a"/><item id="b"/></doc>',
    )

    assert first.principal[0] == XdmAtomic.string("b")
    assert second.principal[0] == XdmAtomic.string("a")
    assert first.principal[1] == XdmAtomic.string("one")
    assert second.principal[1] == XdmAtomic.string("two")
    assert first.principal[2].lexical.startswith("ID")
    assert second.principal[2].lexical.startswith("ID")
    assert first.principal[3] == first.principal[4]
    assert second.principal[3] == second.principal[4]


def test_failed_evaluation_does_not_poison_later_transformations() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="current#0()"/>
        </xsl:template>
        <xsl:template name="safe">
          <xsl:sequence select="()"/>
        </xsl:template>
        """
    )
    compiled = XsltProcessor().compile(io.BytesIO(source))
    assert (
        error_code(lambda: compiled.transform(source=b"<doc/>", initial_template="main"))
        == "XTDE1360"
    )
    assert compiled.transform(source=b"<doc/>", initial_template="safe").principal == ()
    assert (
        error_code(lambda: compiled.transform(source=b"<doc/>", initial_template="main"))
        == "XTDE1360"
    )


# ---------------------------------------------------------------------------
# Standalone token contract


def test_context_function_token_keeps_standalone_evaluation_non_contextual() -> None:
    document = _document()
    item = document.root_element
    seen: list[tuple[Any, tuple[XdmSequence, ...]]] = []

    def direct(context_item: Any, arguments: tuple[XdmSequence, ...]) -> XdmSequence:
        seen.append((context_item, arguments))
        return (item,)

    def dynamic(context_item: Any, arguments: tuple[XdmSequence, ...]) -> XdmSequence:
        raise XsltError("dynamic current has no context item", code="XTDE1360")

    static = XPathStaticContext(
        namespaces={"fn": FN_NS},
        functions={"fn:current": 0},
    )
    engine = XPathEngine("3.1")
    compiled = engine.compile("count(current()/item)", static)
    environment = ExpressionEnvironment(
        function_overrides={f"{{{FN_NS}}}current": ContextFunction(direct, dynamic)}
    )

    assert engine.evaluate(compiled, environment, focus=item) == (XdmAtomic.integer(2),)
    assert seen == [(item, ())]

    dynamic_compiled = engine.compile("current#0()", static)
    with pytest.raises(XsltError) as caught:
        engine.evaluate(dynamic_compiled, environment, focus=item)
    assert caught.value.code == "XTDE1360"

    absent = engine.compile("current()", static)
    assert engine.evaluate(absent, environment, focus=Focus.absent()) == (item,)


def test_bridge_absent_sentinel_is_not_an_empty_sequence() -> None:
    assert ABSENT != ()
    empty_group = bridge(group=())
    assert empty_group.current_group() == ()
    absent_group = bridge()
    with pytest.raises(XsltError) as caught:
        absent_group.current_group()
    assert caught.value.code == "XTDE1061"


def test_bridge_rejects_invalid_state_versions() -> None:
    with pytest.raises(ValueError, match="version"):
        XsltContextBridge(Transformation(), xpath_version="4.0")
    with pytest.raises(TypeError, match="current_group"):
        XsltContextBridge(Transformation(), group="not a sequence")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="current_merge_group"):
        XsltContextBridge(
            Transformation(),
            merge_group=[],  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="regex_groups"):
        XsltContextBridge(Transformation(), regex_captures="bad")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="namespaces"):
        XsltContextBridge(Transformation(), namespaces=[])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Defensive bridge contracts and branch coverage


def test_context_function_validation_and_direct_dynamic_invocation() -> None:
    with pytest.raises(TypeError, match="direct callback"):
        ContextFunction(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="dynamic callback"):
        ContextFunction(
            lambda context_item, arguments: (),
            object(),  # type: ignore[arg-type]
        )

    function = ContextFunction(
        lambda context_item, arguments: (XdmAtomic.string("direct"),),
        lambda context_item, arguments: (XdmAtomic.string("dynamic"),),
    )
    assert function(None, ()) == (XdmAtomic.string("direct"),)
    assert function.invoke(None, ()) == (XdmAtomic.string("direct"),)
    assert function.invoke(None, (), dynamic=True) == (XdmAtomic.string("dynamic"),)
    fallback = ContextFunction(lambda context_item, arguments: (XdmAtomic.string("fallback"),))
    assert fallback.invoke(None, (), dynamic=True) == (XdmAtomic.string("fallback"),)


def test_context_function_token_requires_installed_callbacks() -> None:
    from elementpath.xpath3 import XPath31Parser

    from pyxsd.xpath.functions import ContextFunctionToken

    token = ContextFunctionToken(XPath31Parser())
    with pytest.raises(RuntimeError, match="no evaluation context"):
        token.evaluate(None)
    with pytest.raises(RuntimeError, match="no evaluation context"):
        token()
    assert "ContextFunctionToken" in repr(token)


def test_eqname_forms_and_invalid_names_in_availability_functions() -> None:
    contextual = bridge(
        namespaces={"x": "urn:x", "xs": XSD_NS, "fn": FN_NS},
        default_namespace="urn:default",
    )
    assert contextual.system_property("Q{}nothing") == ""
    assert contextual.system_property("Q{http://www.w3.org/1999/XSL/Transform}version") == "3.0"
    assert contextual.type_available("Q{http://www.w3.org/2001/XMLSchema}string")
    assert not contextual.type_available("Q{urn:missing}Thing")
    assert contextual.element_available("Q{http://www.w3.org/1999/XSL/Transform}template")
    assert not contextual.element_available("Q{urn:missing}instruction")
    assert contextual.function_available("Q{http://www.w3.org/2005/xpath-functions}current")
    assert not contextual.function_available("Q{}current")
    assert contextual.function_available("xs:string")
    assert not contextual.function_available("xs:string", 2)
    assert contextual.function_available("fn:document", 1)
    assert contextual.function_available("fn:document", 2)
    assert not contextual.function_available("fn:document", 3)

    invalid = ("", "Q{bad", "Q{urn:x}1bad", "1bad", "x:two:parts", "missing:name")
    for value in invalid:
        with pytest.raises(XsltError) as caught:
            contextual.system_property(value)
        assert caught.value.code == "XTDE1390"


def test_bridge_state_validation_and_copy_helpers() -> None:
    with pytest.raises(TypeError, match="focus"):
        bridge(focus="bad")
    with pytest.raises(TypeError, match="current_item"):
        bridge(current_item="bad")
    with pytest.raises(TypeError, match="contain XDM items"):
        bridge(group=("bad",))
    with pytest.raises(TypeError, match="current_merge_group source names"):
        bridge(merge_group={1: ()})
    with pytest.raises(TypeError, match="contain strings"):
        bridge(regex_captures=(1,))
    with pytest.raises(TypeError, match="map strings"):
        bridge(namespaces={"x": 1})
    with pytest.raises(TypeError, match="base_uri"):
        bridge(base_uri=1)
    with pytest.raises(TypeError, match="package_name"):
        bridge(package_name=1)
    with pytest.raises(TypeError, match="key_lookup"):
        bridge(key_lookup=1)

    document = _document()
    first = document.root_element.children[0]
    key = XdmAtomic.string("k")
    grouped = bridge().with_group((first,), (key,))
    assert grouped.current_group() == (first,)
    assert grouped.current_grouping_key() == (key,)
    merged = bridge().with_merge_group({"left": (first,)}, (key,))
    assert merged.current_merge_group() == (first,)
    assert merged.current_merge_key() == (key,)
    regex = bridge().with_regex_groups(("whole",))
    assert regex.regex_group(0) == "whole"


def test_schema_pool_and_stylesheet_component_availability() -> None:
    from types import SimpleNamespace

    class _Pool:
        def get_type(self, name: str) -> object | None:
            return object() if name == "{urn:types}Thing" else None

    stylesheet = SimpleNamespace(schema_pool=_Pool(), _packages={}, package=None)
    transformation = SimpleNamespace(stylesheet=stylesheet)
    typed = XsltContextBridge(transformation)  # type: ignore[arg-type]
    assert typed.type_available("Q{urn:types}Thing")
    assert not typed.type_available("Q{urn:types}Missing")

    hidden = SimpleNamespace(
        kind="function",
        name="{urn:my}f",
        hidden=True,
        abstract=False,
        arity=1,
    )
    abstract = SimpleNamespace(
        kind="function",
        name="{urn:my}g",
        hidden=False,
        abstract=True,
        arity=1,
    )
    visible = SimpleNamespace(
        kind="function",
        name="{urn:my}h",
        hidden=False,
        abstract=False,
        arity=1,
    )
    package = SimpleNamespace(
        components={
            "x": hidden,
            "y": abstract,
            "z": visible,
        },
        accumulator=lambda name: None,
    )
    stylesheet = SimpleNamespace(
        schema_pool=None,
        _packages={None: package, "pkg": package},
        package=package,
    )
    transformed = XsltContextBridge(
        SimpleNamespace(stylesheet=stylesheet),  # type: ignore[arg-type]
        namespaces={"my": "urn:my"},
    )
    assert not transformed.function_available("my:f")
    assert not transformed.function_available("my:g")
    assert transformed.function_available("my:h")
    assert transformed.function_available("my:h", 1)
    assert not transformed.function_available("my:h", 2)
    assert not transformed.function_available("my:missing", 1)


def test_bridge_document_state_and_denial_paths(tmp_path: Path) -> None:
    from types import SimpleNamespace

    document = _document()
    transformation = SimpleNamespace(documents={"urn:doc": document})
    available = XsltContextBridge(transformation)  # type: ignore[arg-type]
    assert available.document("urn:doc") is document
    assert available.document("urn:doc", document.root_element) is document
    overrides = available.function_overrides()
    assert overrides[f"{{{FN_NS}}}document"].direct(
        None, ((XdmAtomic.string("urn:doc"),), (document.root_element,))
    ) == (document,)

    denied = XsltContextBridge(SimpleNamespace(documents={}))  # type: ignore[arg-type]
    with pytest.raises(XsltError) as caught:
        denied.document("urn:doc")
    assert caught.value.code == "FODC0002"
    with pytest.raises(XsltError) as caught:
        denied.document("urn:doc#fragment")
    assert caught.value.code == "FODC0002"
    with pytest.raises(XsltError) as caught:
        denied.document("")
    assert caught.value.code == "FODC0002"

    path = tmp_path / "module.xsl"
    path.write_text(
        stylesheet(
            """
            <xsl:template name="main">
              <xsl:sequence select="count(document('')/xsl:stylesheet)"/>
            </xsl:template>
            """
        ).decode(),
        encoding="utf-8",
    )
    compiled = XsltProcessor().compile(path)
    assert compiled.transform(initial_template="main").principal == (XdmAtomic.integer(1),)


def test_accumulator_state_contract_for_a_declared_component() -> None:
    from types import SimpleNamespace

    package = SimpleNamespace(
        components={},
        accumulator=lambda name: object(),
    )
    stylesheet = SimpleNamespace(
        schema_pool=None,
        _packages={None: package},
        package=package,
    )
    declared = XsltContextBridge(SimpleNamespace(stylesheet=stylesheet))  # type: ignore[arg-type]
    for call in (declared.accumulator_before, declared.accumulator_after):
        with pytest.raises(XsltError):
            call("known")


def test_bridge_argument_helpers_reject_invalid_callbacks() -> None:
    contextual = bridge()
    overrides = contextual.function_overrides()
    system = overrides[f"{{{FN_NS}}}system-property"]
    with pytest.raises(XsltError) as caught:
        system.direct(None, ())
    assert caught.value.code == "XPTY0004"
    with pytest.raises(XsltError) as caught:
        system.direct(None, ((XdmAtomic.string("a"), XdmAtomic.string("b")),))
    assert caught.value.code == "XPTY0004"
    with pytest.raises(XsltError) as caught:
        system.direct(None, ((_document().root_element,),))
    assert caught.value.code == "XPTY0004"

    regex = overrides[f"{{{FN_NS}}}regex-group"]
    with pytest.raises(XsltError) as caught:
        regex.direct(None, ((XdmAtomic.string("not-an-integer"),),))
    assert caught.value.code == "XPTY0004"

    document = overrides[f"{{{FN_NS}}}document"]
    with pytest.raises(XsltError) as caught:
        document.direct(None, ((XdmAtomic.string("urn:x"),), ()))
    assert caught.value.code == "XPTY0004"

    entity = overrides[f"{{{FN_NS}}}unparsed-entity-uri"]
    with pytest.raises(XsltError) as caught:
        entity.direct(None, ((XdmAtomic.string("e"),), (XdmAtomic.string("x"),)))
    assert caught.value.code == "XPTY0004"


def test_environment_decimal_format_validation() -> None:
    with pytest.raises(TypeError, match="decimal format names"):
        ExpressionEnvironment(decimal_formats={1: {}})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="must be a mapping"):
        ExpressionEnvironment(decimal_formats={"x": []})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="attributes must map"):
        ExpressionEnvironment(
            decimal_formats={"x": {"decimal-separator": 1}}  # type: ignore[dict-item]
        )
    environment = ExpressionEnvironment(
        decimal_formats={None: {"decimal-separator": ","}, "urn:x": {"zero-digit": "0"}}
    )
    assert environment.decimal_formats[None]["decimal-separator"] == ","


def test_key_three_argument_forms_and_dynamic_invocation() -> None:
    source = stylesheet(
        """
        <xsl:key name="by-id" match="item" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('by-id', 'a', ()))"/>
          <xsl:sequence select="key#3('by-id', 'a', /*)/@id/string()"/>
          <xsl:sequence select="key#2('by-id', 'a')"/>
        </xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(source, b'<doc><item id="a"/></doc>', initial_template="main"))
        == "XTDE1270"
    )

    explicit = stylesheet(
        """
        <xsl:key name="by-id" match="item" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('by-id', 'a', ()))"/>
          <xsl:sequence select="string-join(key('by-id', 'a', /*)/@id, ',')"/>
        </xsl:template>
        """
    )
    assert transform(explicit, b'<doc><item id="a"/></doc>', initial_template="main").principal == (
        XdmAtomic.integer(0),
        XdmAtomic.string("a"),
    )

    for third in ("('x', 'y')", "'x'"):
        bad = stylesheet(
            f"""
            <xsl:key name="by-id" match="item" use="@id"/>
            <xsl:template name="main">
              <xsl:sequence select="key('by-id', 'a', {third})"/>
            </xsl:template>
            """
        )
        assert (
            error_code(
                lambda bad=bad: transform(
                    bad, b'<doc><item id="a"/></doc>', initial_template="main"
                )
            )
            == "XPTY0004"
        )


def test_dynamic_pure_context_functions_keep_their_state() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="system-property#1('xsl:version')"/>
          <xsl:sequence select="function-available#2('fn:current', 0)"/>
          <xsl:sequence select="element-available#1('xsl:template')"/>
        </xsl:template>
        """
    )
    assert transform(source, b"<doc/>", initial_template="main").principal == (
        XdmAtomic.string("3.0"),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
    )


def test_unnamed_decimal_format_is_bridged() -> None:
    source = stylesheet(
        """
        <xsl:decimal-format decimal-separator="," grouping-separator="."/>
        <xsl:template name="main">
          <xsl:sequence select="format-number(1234.5, '#.##0,00')"/>
        </xsl:template>
        """
    )
    assert transform(source, b"<doc/>", initial_template="main").principal == (
        XdmAtomic.string("1.234,50"),
    )


def test_standalone_key_three_argument_host_function() -> None:
    document = _document()
    first = document.root_element.children[0]
    engine = XPathEngine("3.1")
    compiled = engine.compile(
        "count(key('by-id', 'a', /*))",
    )
    environment = ExpressionEnvironment(key_indexes={"by-id": {"a": (first,)}})
    assert engine.evaluate(compiled, environment, focus=document) == (XdmAtomic.integer(1),)


def test_declared_xslt_function_arity_filters_by_version() -> None:
    one = xslt_function_declarations("1.0")
    assert one["fn:document"] == 1
    assert one["fn:function-available"] == 1
    assert one["fn:unparsed-entity-uri"] == 1
    assert "fn:current-group" not in one
    assert one["fn:current"] == 0


def test_plain_function_overrides_receive_context_and_arguments() -> None:
    document = _document()
    item = document.root_element
    seen: list[tuple[Any, tuple[XdmSequence, ...]]] = []

    def override(context_item: Any, arguments: tuple[XdmSequence, ...]) -> XdmSequence:
        seen.append((context_item, arguments))
        return (XdmAtomic.string("override"),)

    engine = XPathEngine("3.1")
    compiled = engine.compile("generate-id(.)")
    environment = ExpressionEnvironment(function_overrides={f"{{{FN_NS}}}generate-id": override})
    assert engine.evaluate(compiled, environment, focus=item) == (XdmAtomic.string("override"),)
    assert seen == [(item, ((item,),))]
