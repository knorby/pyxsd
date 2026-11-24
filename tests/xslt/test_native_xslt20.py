"""The native, schema-aware XSLT 2.0 processor profile.

``XsltProcessor(language="2.0", xpath="2.0")`` is a distinct processor
identity, not a version-flag alias: it runs XPath 2.0, owns the XSLT 2.0
element vocabulary and its XTRE recovery table, restricts XDM values to
2.0 item kinds, selects the Serialization 2.0 method set, and reports
``system-property('xsl:version')`` as ``2.0``.  A ``version="2.0"``
stylesheet under the default 3.0 processor keeps the pinned compatibility
behavior (see ``tests/xslt/test_compatibility.py``); these tests exercise
the native identity and the feature errors that separate the two.
"""

from __future__ import annotations

import io
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from tests.xslt.support import stylesheet as _stylesheet

from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmDocument
from pyxsd.xslt import ExtensionSignature, XsltProcessor
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.expressions import compile_xpath
from pyxsd.xslt.errors import XsltDynamicError, XsltStaticError
from pyxsd.xslt.extensions import XsltExtensions
from pyxsd.xslt.profiles import (
    DOCUMENT_RETRIEVAL,
    XSLT20_RECOVERY_RULES,
    LanguageProfile,
    RecoveryPolicy,
)
from pyxsd.xslt.python_rules import PythonRuleContext, PythonRules

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
MY_NS = "urn:my"

stylesheet = partial(_stylesheet, namespaces={"my": MY_NS})
NATIVE = {"language": "2.0", "xpath": "2.0"}


def native_compile(
    body: str,
    *,
    version: str = "2.0",
    attributes: str = "",
    schema_aware: bool = True,
    **options: Any,
) -> Any:
    processor = XsltProcessor(schema_aware=schema_aware, **NATIVE, **options)
    return processor.compile(io.BytesIO(stylesheet(body, version=version, attributes=attributes)))


def native_transform(
    body: str,
    *,
    version: str = "2.0",
    source: bytes = b"<doc/>",
    delivery: str = "raw",
    compile_options: dict[str, Any] | None = None,
    **options: Any,
) -> Any:
    compiled = native_compile(
        body,
        version=version,
        **(compile_options or {}),
    )
    options.setdefault("initial_template", "main")
    return compiled.transform(source=source, delivery=delivery, **options)


def element_text(node: Any) -> str:
    return "".join(child.string_value for child in node.children if child.kind is NodeKind.TEXT)


# ---------------------------------------------------------------------------
# Processor identity


def test_native_processor_identity_and_compilation() -> None:
    processor = XsltProcessor(schema_aware=True, **NATIVE)
    assert processor.language == "2.0"
    assert processor.xpath == "2.0"
    compiled = native_compile('<xsl:template name="main"><out>ok</out></xsl:template>')
    assert compiled.language == "2.0"
    assert compiled.xpath == "2.0"
    assert compiled.profile.processor_profile == "xslt-2.0"
    result = compiled.transform(source=b"<doc/>", initial_template="main", delivery="document")
    assert element_text(result.principal_tree.children[0]) == "ok"

    version = native_transform(
        '<xsl:template name="main">'
        "<xsl:sequence select=\"system-property('xsl:version')\"/>"
        "</xsl:template>"
    )
    assert version.principal == (XdmAtomic.string("2.0"),)


def test_native_processor_requires_matching_xpath() -> None:
    with pytest.raises(ValueError, match=r"requires XPath 2\.0"):
        XsltProcessor(language="2.0")
    with pytest.raises(ValueError, match=r"requires XPath 2\.0"):
        XsltProcessor(language="2.0", xpath="3.1")
    with pytest.raises(ValueError, match="unsupported XSLT language"):
        XsltProcessor(language="4.0", xpath="3.1")
    XsltProcessor(language="2.0", xpath="2.0")


def test_distinct_processor_identities_coexist() -> None:
    body = (
        '<xsl:template name="main">'
        "<xsl:sequence select=\"system-property('xsl:version')\"/>"
        "</xsl:template>"
    )
    compatibility = XsltProcessor().compile(io.BytesIO(stylesheet(body, version="2.0")))
    native = XsltProcessor(**NATIVE).compile(io.BytesIO(stylesheet(body, version="2.0")))
    assert compatibility.transform(initial_template="main").principal[0] == XdmAtomic.string("3.0")
    assert native.transform(initial_template="main").principal[0] == XdmAtomic.string("2.0")


# ---------------------------------------------------------------------------
# Static feature gate: the XSLT 2.0 element vocabulary


@pytest.mark.parametrize(
    "declaration",
    [
        '<xsl:try><xsl:sequence select="1"/><xsl:catch errors="err:XPTY0004"/></xsl:try>',
        '<xsl:evaluate xpath="$expr"/>',
        '<xsl:iterate select="1 to 3"/>',
        "<xsl:fork/>",
        '<xsl:map><xsl:map-entry key="\'k\'" select="1"/></xsl:map>',
        "<xsl:merge/>",
        '<xsl:assert test="true()"/>',
        "<xsl:where-populated/>",
        "<xsl:on-empty/>",
        '<xsl:source-document href="x.xml"/>',
        '<xsl:accumulator name="a" initial-value="0"/>',
        '<xsl:context-item use="absent"/>',
        "<xsl:global-context-item/>",
        "<xsl:next-iteration/>",
    ],
)
def test_native_processor_rejects_3_0_instructions(declaration: str) -> None:
    body = f'<xsl:template name="main">{declaration}</xsl:template>'
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XTSE0010"
    assert "XSLT 2.0" in str(caught.value)


@pytest.mark.parametrize(
    "source",
    [
        '<xsl:package name="urn:p" version="2.0" xmlns:xsl="{xslt}">'
        '<xsl:template name="t"/></xsl:package>',
        '<xsl:stylesheet version="2.0" xmlns:xsl="{xslt}">'
        '<xsl:mode name="m" visibility="public"/></xsl:stylesheet>',
    ],
)
def test_native_processor_rejects_3_0_declarations(source: str) -> None:
    raw = source.replace("{xslt}", XSLT_NS).encode()
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor(**NATIVE).compile(io.BytesIO(raw))
    assert caught.value.code == "XTSE0010"
    assert "XSLT 2.0" in str(caught.value)


def test_native_processor_rejects_unknown_xslt_elements() -> None:
    with pytest.raises(XsltStaticError) as caught:
        native_compile('<xsl:template name="main"><xsl:nonsense-thing/></xsl:template>')
    assert caught.value.code == "XTSE0010"


def test_native_processor_accepts_2_0_vocabulary() -> None:
    body = """
        <xsl:key name="k" match="doc" use="@id"/>
        <xsl:function name="my:shout" as="xs:string">
          <xsl:param name="value" as="xs:string"/>
          <xsl:sequence select="upper-case($value)"/>
        </xsl:function>
        <xsl:template name="main">
          <out>
            <xsl:variable name="words" select="tokenize('a,b,c', ',')"/>
            <xsl:value-of select="my:shout(string-join($words, '-'))"/>
            <xsl:variable name="tree">
              <xsl:document><held/></xsl:document>
            </xsl:variable>
            <xsl:value-of select="count($tree/held)"/>
            <xsl:for-each-group select="1 to 6" group-by=". mod 2">
              <g n="{current-grouping-key()}"/>
            </xsl:for-each-group>
            <xsl:analyze-string select="/doc" regex="k(2)">
              <xsl:matching-substring>
                <m><xsl:value-of select="regex-group(1)"/></m>
              </xsl:matching-substring>
            </xsl:analyze-string>
            <xsl:apply-templates select="/doc" mode="my:process"/>
            <xsl:value-of select="count(key('k', 'i'))"/>
          </out>
        </xsl:template>
        <xsl:template match="doc" mode="my:process" priority="5">
          <d><xsl:next-match/></d>
        </xsl:template>
        <xsl:template match="doc" mode="my:process">
          <second/>
        </xsl:template>
    """
    result = native_transform(body, source=b"<doc id='i'>k2</doc>", delivery="document")
    element = result.principal_tree.children[0]
    assert element_text(element) == "A-B-C11"
    elements = [child for child in element.children if child.kind is NodeKind.ELEMENT]
    assert [child.local_name for child in elements] == ["g", "g", "m", "d"]
    nested = next(child for child in elements if child.local_name == "d")
    assert [child.local_name for child in nested.children] == ["second"]


def test_native_2_0_numbering_works() -> None:
    body = """
        <xsl:template name="main"><out>
          <xsl:for-each select="/doc/s/i">
            <xsl:number level="multiple" count="s|i" format="1.1 "/>
          </xsl:for-each>
          <xsl:number select="/doc/s[2]/i" level="any" count="i" format="1"/>
          <xsl:number value="4" format="i"/>
        </out></xsl:template>
    """
    result = native_transform(
        body,
        source=b"<doc><s><i/><i/></s><s><i/></s></doc>",
        delivery="document",
    )
    assert element_text(result.principal_tree.children[0]) == "1.1 1.2 2.1 3iv"


def test_native_numbering_has_no_3_0_start_at() -> None:
    body = '<xsl:template name="main"><xsl:number value="3" start-at="2"/></xsl:template>'
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XTSE0090"
    assert "start-at" in str(caught.value)
    # The default 3.0 processor compiles the same numbering instruction.
    XsltProcessor().compile(io.BytesIO(stylesheet(body)))


# ---------------------------------------------------------------------------
# The XPath 2.0 static surface


@pytest.mark.parametrize(
    "expression",
    [
        "'a' || 'b'",
        "(1, 2) => sum()",
        "let $x := 1 return $x",
        "map {'a': 1}",
        "array { 1, 2 }",
    ],
)
def test_native_xpath_grammar_is_2_0(expression: str) -> None:
    body = f'<xsl:template name="main"><xsl:sequence select="{expression}"/></xsl:template>'
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XPST0003"
    # The same expression keeps working under the 3.0 processor, including
    # for a version="2.0" stylesheet: compatibility is not the native profile.
    XsltProcessor().compile(io.BytesIO(stylesheet(body, version="2.0")))


@pytest.mark.parametrize(
    "expression",
    [
        "head((1, 2))",
        "for-each((1,2), function($x) { $x })",
        "parse-json('{}')",
    ],
)
def test_native_xpath_rejects_3_0_functions(expression: str) -> None:
    body = f'<xsl:template name="main"><xsl:sequence select="{expression}"/></xsl:template>'
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XPST0017"


def test_native_function_library_is_2_0() -> None:
    body = """
        <xsl:key name="k" match="doc" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="function-available('fn:key')"/>
          <xsl:sequence select="function-available('fn:format-number')"/>
          <xsl:sequence select="element-available('xsl:try')"/>
          <xsl:sequence select="element-available('xsl:analyze-string')"/>
          <xsl:sequence select="format-number(1.5, '0.00')"/>
          <xsl:sequence select="distinct-values(1 to 3)"/>
        </xsl:template>
    """
    result = native_transform(body)
    assert result.principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(True),
        XdmAtomic.string("1.50"),
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
        XdmAtomic.integer(3),
    )


def test_2_0_formatting_functions_are_declared() -> None:
    """``format-integer`` and the date/time formatters are XSLT 2.0 §16.5.

    They are F&O 2.0 functions with no symbol in the XPath 2.0 parser, so the
    native 2.0 profile declares and implements them itself instead of treating
    their absence as a 2.0 rule.
    """
    result = native_transform(
        '<xsl:template name="main">'
        "<xsl:sequence select=\"function-available('fn:format-integer')\"/>"
        "<xsl:sequence select=\"function-available('fn:format-date')\"/>"
        "<xsl:sequence select=\"function-available('fn:format-time')\"/>"
        "<xsl:sequence select=\"function-available('fn:format-dateTime')\"/>"
        "</xsl:template>"
    )
    assert result.principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
    )


def _formatted(body: str, **options: Any) -> str:
    result = native_transform(body, source=b"<doc/>", **options)
    return "".join(item.string_value for item in result.principal)


def test_2_0_date_time_numeric_pictures() -> None:
    """Numeric pictures cover year/month/day and hour/minute/second."""
    assert (
        _formatted(
            '<xsl:param name="d" as="xs:date" select="xs:date(\'2003-12-07\')"/>'
            '<xsl:template name="main">'
            "<xsl:value-of select=\"format-date($d, '[Y]-[M01]-[D]')\"/>"
            "</xsl:template>"
        )
        == "2003-12-7"
    )
    assert (
        _formatted(
            '<xsl:param name="t" as="xs:time" select="xs:time(\'01:02:03\')"/>'
            '<xsl:template name="main">'
            "<xsl:value-of select=\"format-time($t, '[H01]:[m01]:[s01]')\"/>"
            "</xsl:template>"
        )
        == "01:02:03"
    )
    assert (
        _formatted(
            '<xsl:param name="t" as="xs:dateTime" '
            "select=\"xs:dateTime('0985-03-01T09:15:06')\"/>"
            '<xsl:template name="main">'
            "<xsl:value-of select=\"format-dateTime($t, '[Y]-[M01]-[D]')\"/>"
            "</xsl:template>"
        )
        == "985-03-1"
    )


def test_2_0_date_time_width_modifiers() -> None:
    """A width modifier pads and truncates a numeric or roman value."""
    body = (
        '<xsl:param name="t" as="xs:dateTime" '
        "select=\"xs:dateTime('0985-03-01T09:15:06')\"/>"
        '<xsl:template name="main">'
        "<xsl:value-of select=\"format-dateTime($t, '[Y,4-4]|[Y,2-2]|[M,2-2]|[D,1-1]')\"/>"
        "</xsl:template>"
    )
    assert _formatted(body) == "0985|85|03|1"
    roman = (
        '<xsl:param name="t" as="xs:date" select="xs:date(\'1004-01-01\')"/>'
        '<xsl:template name="main">'
        "<xsl:value-of select=\"format-date($t, '[Yi,4-4]')\"/>"
        "</xsl:template>"
    )
    assert _formatted(roman) == "miv "


def test_2_0_date_time_timezone_pictures() -> None:
    """The timezone pictures omit zero minutes only under a narrow width."""
    body = (
        '<xsl:param name="t" as="xs:dateTime" '
        "select=\"xs:dateTime('2002-06-15T12:27:30+03:00')\"/>"
        '<xsl:template name="main">'
        "<xsl:value-of select=\"format-dateTime($t, '[Z]|[z]|[z,2-2]|[z,6-6]')\"/>"
        "</xsl:template>"
    )
    assert _formatted(body) == "+03:00|GMT+03:00|GMT+03|GMT+03:00"
    half = (
        '<xsl:param name="t" as="xs:dateTime" '
        "select=\"xs:dateTime('2002-06-15T12:27:30-05:30')\"/>"
        '<xsl:template name="main">'
        "<xsl:value-of select=\"format-dateTime($t, '[z,2-2]')\"/>"
        "</xsl:template>"
    )
    assert _formatted(half) == "GMT-05:30"


def test_2_0_fractional_rounding_carries_into_seconds() -> None:
    """Rounding a fractional second can advance the whole second."""
    body = (
        '<xsl:param name="t" as="xs:dateTime" '
        "select=\"xs:dateTime('2003-12-07T00:00:00.99996')\"/>"
        '<xsl:template name="main">'
        "<xsl:value-of select=\"format-dateTime($t, '[H01]:[m01]:[s01].[f1]')\"/>"
        "</xsl:template>"
    )
    assert _formatted(body) == "00:00:01.0"


def test_2_0_date_time_am_pm_pictures() -> None:
    """English am/pm uses the F&O words, with max-width truncation only."""
    body = (
        '<xsl:param name="t" as="xs:time" select="xs:time(\'00:00:00\')"/>'
        '<xsl:template name="main">'
        "<xsl:value-of select="
        "\"format-time($t, '[H01]:[m01] = [h][P]|[PN,2-2]|[Pn,2-2]|[PNn,2-2]|[PNn,1-1]')\"/>"
        "</xsl:template>"
    )
    assert _formatted(body) == "00:00 = 12am|AM|am|Am|A"


def test_2_0_date_time_picture_escaping() -> None:
    """``[[`` and ``]]`` are literal brackets in a picture string."""
    body = (
        '<xsl:param name="d" as="xs:date" select="xs:date(\'2003-12-07\')"/>'
        '<xsl:template name="main">'
        "<xsl:value-of select=\"format-date($d, '[[[Y0001]]]')\"/>"
        "</xsl:template>"
    )
    assert _formatted(body) == "[2003]"


def test_2_0_date_time_invalid_picture() -> None:
    """An unknown component or a malformed picture reports XSLT's code."""
    for picture, code in (("[bla]", "FOFD1340"), ("[Y,0]", "FOFD1340")):
        body = (
            '<xsl:param name="d" as="xs:date" select="xs:date(\'2003-12-07\')"/>'
            '<xsl:template name="main">'
            f"<xsl:value-of select=\"format-date($d, '{picture}')\"/>"
            "</xsl:template>"
        )
        with pytest.raises(XsltDynamicError) as caught:
            native_transform(body)
        assert caught.value.code == code
    unavailable = (
        '<xsl:param name="d" as="xs:date" select="xs:date(\'2003-12-07\')"/>'
        '<xsl:template name="main">'
        "<xsl:value-of select=\"format-date($d, '[H]')\"/>"
        "</xsl:template>"
    )
    with pytest.raises(XsltDynamicError) as caught:
        native_transform(unavailable)
    assert caught.value.code == "FOFD1350"


def test_2_0_date_time_language_and_calendar_fallback() -> None:
    """An unsupported language or calendar names its implementation fallback."""
    body = (
        '<xsl:param name="d" as="xs:date" select="xs:date(\'2006-03-01\')"/>'
        '<xsl:template name="main">'
        "<xsl:value-of select=\"format-date($d, '[MNn]', 'xib', (), ())\"/>"
        "<xsl:value-of select=\"format-date($d, '[M01]', 'en', 'CB', ())\"/>"
        "</xsl:template>"
    )
    assert _formatted(body) == "[Language: en]March[Calendar: AD]03"


def test_2_0_format_integer_pictures() -> None:
    """``format-integer`` covers decimal, alphabetic, roman, and words."""
    body = (
        '<xsl:template name="main">'
        '<xsl:value-of separator="|" select="'
        "format-integer(3, '1'), format-integer(3, '001'), "
        "format-integer(4, 'a'), format-integer(4, 'A'), "
        "format-integer(4, 'i'), format-integer(4, 'I'), "
        "format-integer(21, 'w;o'), format-integer(1, 'w;o')\"/>"
        "</xsl:template>"
    )
    assert _formatted(body) == "3|003|d|D|iv|IV|twenty-first|first"


def test_2_0_format_integer_words_case() -> None:
    """The word pictures select lower, upper, and title case."""
    body = (
        '<xsl:template name="main">'
        '<xsl:value-of separator="," select="format-integer(21, \'w\'), '
        "format-integer(21, 'W'), format-integer(21, 'Ww')\"/>"
        "</xsl:template>"
    )
    assert _formatted(body) == "twenty-one,TWENTY-ONE,Twenty-One"


@pytest.mark.parametrize(
    "expression",
    [
        "key('k', 'i', /)",
        "function-available('fn:format-number', 2)",
        "type-available('xs:integer', 'node()*')",
        "unparsed-entity-public-id('x')",
    ],
)
def test_native_function_arities_are_2_0(expression: str) -> None:
    body = (
        '<xsl:key name="k" match="doc" use="@id"/>'
        f'<xsl:template name="main"><xsl:sequence select="{expression}"/></xsl:template>'
    )
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XPST0017"


def test_compile_xpath_version_keyword_selects_the_static_context() -> None:
    context = StaticContext()
    with pytest.raises(XsltStaticError) as caught:
        compile_xpath("map {'a': 1}", context, version="2.0")
    assert caught.value.code == "XPST0003"
    assert compile_xpath("map {'a': 1}", context, version="3.1") is not None
    assert compile_xpath("'a' || 'b'", context, version="3.1") is not None
    with pytest.raises(XsltStaticError) as caught:
        compile_xpath("'a' || 'b'", context, version="2.0")
    assert caught.value.code == "XPST0003"
    assert compile_xpath("1 + 1", context, version="1.0") is not None
    with pytest.raises(XsltStaticError) as caught:
        compile_xpath("1 to 3", context, version="1.0")
    assert caught.value.code == "XPST0003"
    with pytest.raises(ValueError):
        compile_xpath("1", context, version="4.0")
    with pytest.raises(TypeError):
        compile_xpath("1", context, version=2)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Compatibility conversions, recovery, and system properties


def test_native_2_0_backwards_compatible_conversions() -> None:
    legacy = native_transform(
        '<xsl:template name="main"><out><xsl:value-of xsl:version="1.0" '
        'select="(1, 2, 3)"/></out></xsl:template>',
        delivery="document",
    )
    assert element_text(legacy.principal_tree.children[0]) == "1"

    modern = native_transform(
        '<xsl:template name="main"><out><xsl:value-of select="(1, 2, 3)"/></out></xsl:template>',
        delivery="document",
    )
    assert element_text(modern.principal_tree.children[0]) == "1 2 3"


def test_native_2_0_compatibility_uses_the_2_0_version_rule() -> None:
    # XSLT 2.0 §3.8: any effective version below 2.0 enables backwards
    # compatibility.  The 3.0 processor only does so for exactly 1.0
    # (§3.9.1), so 1.5 separates the two rules where the version is directly
    # observable.  (A 1.5-effective instruction is not otherwise supported by
    # this engine, so the end-to-end case uses the 1.0-effective form.)
    assert LanguageProfile.for_processor("2.0", "1.5").compatibility_mode is True
    assert LanguageProfile.for_processor("3.0", "1.5").compatibility_mode is False
    assert LanguageProfile.for_processor("2.0", "2.0").compatibility_mode is False
    # A 1.0-effective element stays in compatibility mode; a forwards
    # compatible version above the processor version does not.
    assert LanguageProfile.for_processor("2.0", "1.0").compatibility_mode is True
    assert LanguageProfile.for_processor("2.0", "3.0").compatibility_mode is False
    native = native_transform(
        '<xsl:template name="main"><out><xsl:value-of xsl:version="1.0" '
        'select="(1, 2)"/></out></xsl:template>',
        delivery="document",
    )
    assert element_text(native.principal_tree.children[0]) == "1"


def test_native_recovery_table_differs_from_3_0() -> None:
    compiled = native_compile("<xsl:template name='m'/>")
    profile = LanguageProfile.from_options(compiled.profile)
    assert profile.native is True
    assert profile.processor_version == "2.0"
    assert profile.recovery_rules is XSLT20_RECOVERY_RULES
    assert profile.recovery_rule_for("XTRE1500") is not None
    assert profile.recovery_rule_for("FODC0002") is None

    # A policy enabling the 3.0 document-retrieval recovery cannot recover it
    # under the native profile: the rule is outside the 2.0 recovery table.
    body = (
        '<xsl:template name="main">'
        "<xsl:sequence select=\"count(document('urn:missing'))\"/>"
        "</xsl:template>"
    )
    recovering = XsltProcessor(
        recovery_policy=RecoveryPolicy(rules=frozenset({DOCUMENT_RETRIEVAL.name})),
        **NATIVE,
    )
    with pytest.raises(XsltDynamicError) as caught:
        recovering.compile(io.BytesIO(stylesheet(body))).transform(initial_template="main")
    assert caught.value.code == "FODC0002"


def test_native_system_properties_resolve_any_prefix() -> None:
    body = """
        <xsl:template name="main">
          <xsl:sequence select="system-property('x:version')"/>
          <xsl:sequence select="system-property('x:xpath-version')"/>
          <xsl:sequence select="system-property('x:supports-backwards-compatibility')"/>
          <xsl:sequence select="system-property('x:is-schema-aware')"/>
        </xsl:template>
    """
    source = stylesheet(body, version="2.0", namespaces={"x": XSLT_NS})
    native = XsltProcessor(**NATIVE).compile(io.BytesIO(source))
    assert native.transform(initial_template="main").principal == (
        XdmAtomic.string("2.0"),
        XdmAtomic.string("2.0"),
        XdmAtomic.string("yes"),
        XdmAtomic.string("yes"),
    )
    compatibility = XsltProcessor().compile(io.BytesIO(source))
    assert compatibility.transform(initial_template="main").principal == (
        XdmAtomic.string("3.0"),
        XdmAtomic.string("3.1"),
        XdmAtomic.string("yes"),
        XdmAtomic.string("yes"),
    )


def test_native_system_properties_differ_from_3_0() -> None:
    body = """
        <xsl:template name="main">
          <xsl:sequence select="system-property('xsl:version')"/>
          <xsl:sequence select="system-property('xsl:xpath-version')"/>
          <xsl:sequence select="system-property('xsl:is-schema-aware')"/>
          <xsl:sequence select="system-property('xsl:supports-backwards-compatibility')"/>
          <xsl:sequence select="system-property('xsl:supports-dynamic-evaluation')"/>
          <xsl:sequence select="system-property('xsl:supports-higher-order-functions')"/>
          <xsl:sequence select="system-property('xsl:supports-namespace-axis')"/>
          <xsl:sequence select="system-property('xsl:supports-streaming')"/>
          <xsl:sequence select="system-property('xsl:vendor')"/>
        </xsl:template>
    """
    result = native_transform(body)
    assert result.principal == (
        XdmAtomic.string("2.0"),
        XdmAtomic.string("2.0"),
        XdmAtomic.string("yes"),
        XdmAtomic.string("yes"),  # the 2.0 processor implements 1.0 behavior
        XdmAtomic.string("no"),  # a 3.0-only capability property
        XdmAtomic.string("no"),
        XdmAtomic.string("yes"),  # the 2.0 namespace axis is implemented
        XdmAtomic.string("no"),
        XdmAtomic.string("pyxsd"),
    )
    non_aware = native_compile("<xsl:template name='m'/>", schema_aware=False)
    profile = LanguageProfile.from_options(non_aware.profile)
    assert profile.system_property("xsl:is-schema-aware") == "no"


# ---------------------------------------------------------------------------
# XDM 2.0 value restrictions


def test_native_rejects_non_2_0_parameter_values() -> None:
    body = (
        '<xsl:param name="m"/>'
        '<xsl:template name="main"><xsl:sequence select="count($m)"/></xsl:template>'
    )
    compiled = native_compile(body)
    supplied_values = (
        XdmMap.from_mapping({"k": 1}),
        XdmArray((1, 2)),
        XdmFunction(lambda *arguments: (), arity=0),
    )
    for supplied in supplied_values:
        with pytest.raises(XsltDynamicError) as caught:
            compiled.transform(initial_template="main", parameters={"m": supplied})
        assert caught.value.code == "XPTY0004"

    # The default processor accepts the map invocation unchanged.
    permissive = XsltProcessor().compile(io.BytesIO(stylesheet(body)))
    assert permissive.transform(
        initial_template="main", parameters={"m": supplied_values[0]}
    ).principal == (XdmAtomic.integer(1),)


def test_native_rejects_non_2_0_extension_function_results() -> None:
    mapping = XdmMap.from_mapping({"k": 1})
    extensions = XsltExtensions().register_function(
        "my:table",
        0,
        lambda: mapping,
        signature=ExtensionSignature((), "map(*)"),
    )
    source = stylesheet(
        '<xsl:template name="main"><xsl:sequence select="my:table()"/></xsl:template>',
        version="2.0",
    )
    native = XsltProcessor(**NATIVE).compile(io.BytesIO(source), extensions=extensions)
    with pytest.raises(XsltDynamicError) as caught:
        native.transform(initial_template="main", delivery="raw")
    assert caught.value.code == "XPTY0004"

    permissive = XsltProcessor().compile(io.BytesIO(source), extensions=extensions)
    delivered = permissive.transform(initial_template="main", delivery="raw").principal
    assert isinstance(delivered[0], XdmMap)
    assert delivered[0].get(XdmAtomic.string("k")) == (XdmAtomic.integer(1),)

    with pytest.raises(TypeError):
        extensions.runtime_targets(profile="2.0")  # type: ignore[arg-type]


def test_native_rejects_non_2_0_python_rule_results() -> None:
    map_value = XdmMap.from_mapping({"k": 1})
    function_value = XdmFunction(lambda *arguments: (), arity=0)

    def map_callback(context: PythonRuleContext) -> Any:
        del context
        return map_value

    def function_callback(context: PythonRuleContext) -> Any:
        del context
        return function_value

    def sequence_callback(context: PythonRuleContext) -> Any:
        del context
        return (1, 2)

    base = '<xsl:template match="doc" mode="my:process"><base/></xsl:template>'
    source = stylesheet(base, version="2.0")
    selection = XdmDocument.parse(b"<doc/>").root_element
    cases: tuple[tuple[str, Any, bool], ...] = (
        ("map", map_callback, False),
        ("function item", function_callback, False),
        ("sequence", sequence_callback, True),
    )
    for label, callback, allowed in cases:
        extensions = XsltExtensions().with_python_rules(
            PythonRules.empty().add_mode_rule("my:process", "doc", callback)
        )
        compiled = XsltProcessor(**NATIVE).compile(io.BytesIO(source), extensions=extensions)
        options: dict[str, Any] = {
            "initial_match_selection": selection,
            "initial_mode": "Q{urn:my}process",
            "delivery": "raw",
        }
        if allowed:
            result = compiled.transform(**options)
            assert result.principal == (XdmAtomic.integer(1), XdmAtomic.integer(2)), label
        else:
            with pytest.raises(XsltDynamicError) as caught:  # type: ignore[call-overload]
                compiled.transform(**options)
            assert caught.value.code == "XPTY0004", label


# ---------------------------------------------------------------------------
# Serialization 2.0: output methods and result-document restrictions


@pytest.mark.parametrize("method", ["json", "adaptive", "binary", "TEXT", "Q{urn:other}mine"])
def test_native_output_method_set_is_2_0(method: str) -> None:
    body = f'<xsl:output method="{method}"/><xsl:template name="main"><out>1</out></xsl:template>'
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XTSE0020"


def test_native_output_method_set_accepts_2_0_methods() -> None:
    body = '<xsl:template name="main"><out>ok</out></xsl:template>'
    for method in ("xml", "html", "text", "xhtml"):
        output = f'<xsl:output method="{method}" indent="no"/>'
        assert native_compile(f"{output}{body}") is not None
    serialized = native_transform(
        '<xsl:output method="xhtml" indent="no"/>'
        '<xsl:template name="main"><out>ok</out></xsl:template>'
    )
    assert "ok" in serialized.serialize()


@pytest.mark.parametrize(
    "attribute",
    [
        'html-version="5"',
        'item-separator="|"',
        'allow-duplicate-names="yes"',
        'json-node-output-method="xml"',
        'build-tree="yes"',
        'parameter-document="urn:params"',
        'suppress-indentation="a"',
    ],
)
def test_native_result_document_rejects_3_0_attributes(attribute: str) -> None:
    instruction = f'<xsl:result-document href="out.xml" {attribute}/>'
    with pytest.raises(XsltStaticError) as caught:
        native_compile(f'<xsl:template name="main">{instruction}</xsl:template>')
    assert caught.value.code == "XTSE0090"
    output = f"<xsl:output {attribute}/>"
    with pytest.raises(XsltStaticError) as caught:
        native_compile(f'{output}<xsl:template name="main"><a/></xsl:template>')
    assert caught.value.code == "XTSE0090"


def test_native_result_document_format_is_a_2_0_attribute() -> None:
    body = (
        '<xsl:output name="fmt" method="text"/>'
        '<xsl:template name="main">'
        '<xsl:result-document href="side.txt" format="fmt">side</xsl:result-document>'
        "<main/>"
        "</xsl:template>"
    )
    assert native_compile(body) is not None
    # xsl:output itself uses @name in XSLT 2.0; @format is not a 2.0 name.
    with pytest.raises(XsltStaticError) as caught:
        native_compile('<xsl:output format="fmt"/><xsl:template name="main"><a/></xsl:template>')
    assert caught.value.code == "XTSE0090"
    assert "format" in str(caught.value)


def test_native_result_document_method_is_2_0() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:result-document href="one.txt" method="json"><a/></xsl:result-document>'
        "</xsl:template>"
    )
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XTSE0020"


def test_native_constant_avt_method_is_decided_statically() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:result-document href="one.txt" method="{\'binary\'}"><a/></xsl:result-document>'
        "</xsl:template>"
    )
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XTSE0020"

    # A genuinely dynamic method value is a runtime decision (XTDE0030
    # governs the effective value); the static gate leaves it alone.
    dynamic = (
        '<xsl:template name="main">'
        '<xsl:result-document href="one.txt" '
        "method=\"{if (true()) then 'xml' else 'json'}\">"
        "<a/></xsl:result-document></xsl:template>"
    )
    assert native_compile(dynamic) is not None
    reference = (
        '<xsl:variable name="m" select="\'xml\'"/>'
        '<xsl:template name="main">'
        '<xsl:result-document href="one.txt" method="{$m}">'
        "<a/></xsl:result-document>"
        '<xsl:result-document href="two.txt" method="{$m}-suffix">'
        "<a/></xsl:result-document></xsl:template>"
    )
    assert native_compile(reference) is not None


def test_native_output_without_method_and_prefixed_method() -> None:
    body = '<xsl:output omit-xml-declaration="yes"/><xsl:template name="main"><a/></xsl:template>'
    assert native_compile(body) is not None
    prefixed = '<xsl:output method="my:binary"/><xsl:template name="main"><a/></xsl:template>'
    with pytest.raises(XsltStaticError) as caught:
        native_compile(prefixed)
    assert caught.value.code == "XTSE0020"


def test_native_result_document_2_0_delivery_works(tmp_path: Path) -> None:
    secondary = (tmp_path / "side.txt").as_uri()
    body = (
        '<xsl:template name="main">'
        f'<xsl:result-document href="{secondary}" method="text">side</xsl:result-document>'
        "<main>principal</main>"
        "</xsl:template>"
    )
    result = native_transform(body, delivery="raw")
    assert "principal</main>" in result.serialize()
    entry = result.secondary[secondary]
    assert len(entry.raw) == 1
    assert entry.raw[0].string_value == "side"


# ---------------------------------------------------------------------------
# Regular expressions


@pytest.mark.parametrize("flags", ["q", "iq", "x q"])
def test_native_analyze_string_flags_are_2_0(flags: str) -> None:
    body = (
        '<xsl:template name="main">'
        f'<xsl:analyze-string select="doc" regex="a" flags="{flags}">'
        "<xsl:matching-substring><m/></xsl:matching-substring>"
        "</xsl:analyze-string></xsl:template>"
    )
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XTSE0020"


def test_native_analyze_string_flags_avt_is_decided_statically_when_constant() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:analyze-string select="/doc" regex="-" flags="{\'q\'}">'
        "<xsl:matching-substring><m/></xsl:matching-substring>"
        "</xsl:analyze-string></xsl:template>"
    )
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XTSE0020"

    dynamic = (
        '<xsl:template name="main">'
        '<xsl:analyze-string select="/doc" regex="-" '
        "flags=\"{if (true()) then 'i' else 'q'}\">"
        "<xsl:matching-substring><m/></xsl:matching-substring>"
        "</xsl:analyze-string></xsl:template>"
    )
    assert native_compile(dynamic) is not None


def test_native_analyze_string_dynamic_flags_use_the_2_0_rule() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:analyze-string select="/doc" regex="-" '
        "flags=\"{if (true()) then 'q' else 'i'}\">"
        "<xsl:matching-substring><m/></xsl:matching-substring>"
        "</xsl:analyze-string></xsl:template>"
    )
    compiled = native_compile(body)
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source=b"<doc>a-b</doc>", initial_template="main")
    assert caught.value.code == "XTDE1145"
    # The same computed flag is defined under the 3.0 processor.
    XsltProcessor().compile(io.BytesIO(stylesheet(body))).transform(
        source=b"<doc>a-b</doc>", initial_template="main"
    )


def test_native_analyze_string_accepts_2_0_flags() -> None:
    body = (
        '<xsl:template name="main"><out>'
        '<xsl:analyze-string select="/doc" regex="-" flags="i">'
        '<xsl:matching-substring><m><xsl:value-of select="position()"/></m></xsl:matching-substring>'
        '<xsl:non-matching-substring><n><xsl:value-of select="."/></n></xsl:non-matching-substring>'
        "</xsl:analyze-string></out></xsl:template>"
    )
    result = native_transform(body, source=b"<doc>a-b-c</doc>", delivery="document")
    # position() numbers the whole analyze-string output sequence, so the
    # matching substrings sit at positions 2 and 4.
    assert result.principal_tree.children[0].string_value == "a2b4c"


# ---------------------------------------------------------------------------
# Schema awareness


def test_native_non_schema_aware_processor_rejects_import_schema() -> None:
    body = (
        '<xsl:import-schema><xs:schema targetNamespace="urn:my">'
        '<xs:element name="doc" type="xs:string"/></xs:schema></xsl:import-schema>'
        '<xsl:template name="main"><out/></xsl:template>'
    )
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body, schema_aware=False)
    assert caught.value.code == "XTSE1650"


def test_native_schema_aware_typing_rules() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:sequence select="/doc/@id instance of xs:integer"/>'
        '<xsl:sequence select="/doc instance of element()"/>'
        "</xsl:template>"
    )
    result = native_transform(body, source=b'<doc id="3"/>')
    assert result.principal == (
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(True),
    )


# ---------------------------------------------------------------------------
# Python mode-rule customization through the native processor


def _mode_rule_source() -> bytes:
    return stylesheet(
        '<xsl:template match="doc/item" mode="my:process" priority="1"><base-item/></xsl:template>'
        '<xsl:template match="doc" mode="my:process"><wrap>'
        '<xsl:apply-templates mode="my:process"/></wrap></xsl:template>',
        version="2.0",
    )


def _native_mode_rules(callback: Any) -> Any:
    extensions = XsltExtensions().with_python_rules(
        PythonRules.empty().add_mode_rule("my:process", "doc/item", callback)
    )
    compiled = XsltProcessor(**NATIVE).compile(
        io.BytesIO(_mode_rule_source()), extensions=extensions
    )
    assert compiled.profile.processor_profile == "xslt-2.0"
    return compiled


def _run_mode(compiled: Any) -> Any:
    selection = XdmDocument.parse(b"<doc><item>one</item></doc>").root_element
    return compiled.transform(
        initial_match_selection=selection,
        initial_mode="Q{urn:my}process",
        delivery="raw",
    )


def test_python_mode_rule_next_match_through_native_processor() -> None:
    def python_rule(context: PythonRuleContext) -> Any:
        marker = context.next_match()
        return ("[", marker, "]")

    compiled = _native_mode_rules(python_rule)
    result = _run_mode(compiled)
    assert "[<base-item/>]" in result.serialize()


def test_python_mode_rule_apply_imports_through_native_processor(
    tmp_path: Path,
) -> None:
    imported = stylesheet(
        '<xsl:template match="doc/item" mode="my:process" priority="10"><imported/></xsl:template>',
        version="2.0",
    )
    principal = stylesheet(
        '<xsl:import href="base.xsl"/>'
        '<xsl:template match="doc/item" mode="my:process" priority="1">'
        "<kept/></xsl:template>"
        '<xsl:template match="doc" mode="my:process"><wrap>'
        '<xsl:apply-templates mode="my:process"/></wrap></xsl:template>',
        version="2.0",
    )
    (tmp_path / "base.xsl").write_bytes(imported)
    main = tmp_path / "main.xsl"
    main.write_bytes(principal)

    def python_rule(context: PythonRuleContext) -> Any:
        return context.apply_imports()

    extensions = XsltExtensions().with_python_rules(
        PythonRules.empty().add_mode_rule("my:process", "doc/item", python_rule)
    )
    compiled = XsltProcessor(**NATIVE).compile(main, extensions=extensions)
    assert compiled.profile.processor_profile == "xslt-2.0"
    result = _run_mode(compiled)
    # apply-imports continues at the next lower import precedence: the
    # principal module's kept rule, not the higher-priority imported one.
    assert "<kept/>" in result.serialize()
    assert "<imported/>" not in result.serialize()


def test_python_component_override_requires_3_0_under_native() -> None:
    source = stylesheet(
        '<xsl:template name="my:target" visibility="public"><original/></xsl:template>',
        version="2.0",
    )

    def override(context: PythonRuleContext) -> Any:
        return context.invoke_original()

    extensions = XsltExtensions().with_python_rules(
        PythonRules.empty().override_template("my:target", override)
    )
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor(**NATIVE).compile(io.BytesIO(source), extensions=extensions)
    assert caught.value.code == "XTSE0090"
    assert "visibility" in str(caught.value)


# ---------------------------------------------------------------------------
# Attribute-level 3.0 features


@pytest.mark.parametrize(
    "attribute",
    [
        'expand-text="yes"',
        'default-mode="m"',
        'streamable="yes"',
        'static="yes"',
        'visibility="public"',
    ],
)
def test_native_rejects_3_0_only_standard_attributes(attribute: str) -> None:
    body = f'<xsl:template name="main" {attribute}><out/></xsl:template>'
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XTSE0090"


@pytest.mark.parametrize(
    "declaration",
    [
        '<xsl:template name="t" visibility="public"/>',
        '<xsl:for-each-group select="1" group-by="." composite="yes"/>',
        '<xsl:for-each-group select="1" group-by="." bind-group="yes"/>',
        '<xsl:for-each-group select="1" group-by="." bind-grouping-key="yes"/>',
        '<xsl:key name="k" match="doc" use="@id" composite="yes"/>',
        '<xsl:variable name="tree"><xsl:copy copy-accumulators="yes"/></xsl:variable>',
        '<xsl:copy-of select="/" copy-accumulators="yes"/>',
    ],
)
def test_native_rejects_3_0_only_element_attributes(declaration: str) -> None:
    body = f'<xsl:template name="main">{declaration}</xsl:template>'
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body)
    assert caught.value.code == "XTSE0090"


@pytest.mark.parametrize(
    "body",
    [
        '<xsl:template name="main">'
        '<xsl:message error-code="err:FOER0000">x</xsl:message>'
        "</xsl:template>",
        '<xsl:template match="doc" visitor="yes"/>',
        '<xsl:template name="main">'
        '<xsl:apply-templates select="/doc" mode="#unnamed"/>'
        "</xsl:template>",
        '<xsl:template name="main"><xsl:apply-templates select="/doc" mode="#all"/></xsl:template>',
        '<xsl:template match="doc" mode="#unnamed"/>',
    ],
)
def test_native_schema_gate_rejects_undeclared_2_0_attributes(body: str) -> None:
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor(**NATIVE).compile(io.BytesIO(stylesheet(body, version="2.0")))
    assert caught.value.code in ("XTSE0090", "XTSE0550")
    assert "XSLT 2.0" in str(caught.value)


def test_native_schema_gate_keeps_the_2_0_vocabulary() -> None:
    body = """
        <xsl:template match="doc" mode="#all"><matched/></xsl:template>
        <xsl:template name="main">
          <xsl:apply-templates select="/doc" mode="#current"/>
          <xsl:apply-templates select="/doc" mode="#default"/>
          <xsl:message select="1" terminate="no"/>
          <xsl:value-of select="1" disable-output-escaping="no"/>
          <xsl:for-each select="1 to 2">
            <xsl:sort select="." stable="yes"/>
          </xsl:for-each>
          <xsl:result-document href="side.txt" format="fmt"><a/></xsl:result-document>
        </xsl:template>
        <xsl:output name="fmt" method="text"/>
        <xsl:attribute-set name="s" use-attribute-sets=""/>
    """
    assert native_compile(body) is not None


def test_native_rejects_static_variables() -> None:
    # A declaration above 2.0 would enable forwards-compatible processing, in
    # which XSLT 2.0 §3.6 requires an undefined attribute to be ignored; the
    # strict 2.0 declaration keeps XTSE0090.
    source = stylesheet(
        '<xsl:variable name="v" static="yes" select="1"/>'
        '<xsl:template name="main"><xsl:value-of select="$v"/></xsl:template>',
        version="2.0",
    )
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor(**NATIVE).compile(io.BytesIO(source))
    assert caught.value.code == "XTSE0090"
    assert "static" in str(caught.value)
