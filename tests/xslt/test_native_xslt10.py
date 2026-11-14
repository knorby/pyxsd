"""The native XSLT 1.0 processor profile.

``XsltProcessor(language="1.0", xpath="1.0")`` is a distinct processor
identity, not the XSLT 3.0 backwards-compatibility mode: it runs the XPath 1.0
grammar and function set, uses the legacy value model (node-sets, result tree
fragments, and XPath 1.0 conversions), owns the XSLT 1.0 element/attribute
vocabulary and its Serialization 1.0 parameters, and reports
``system-property('xsl:version')`` as ``1.0``.  A ``version="1.0"`` stylesheet
under the default 3.0 processor keeps the pinned compatibility behavior (see
``tests/xslt/test_compatibility.py``); these tests exercise the native identity
and the errors that separate the two.
"""

from __future__ import annotations

import io
import math
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from elementpath.datatypes import UntypedAtomic
from tests.xslt.support import stylesheet as _stylesheet

from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.legacy import (
    LegacyNodeSetError,
    ResultTreeFragment,
    is_legacy_number,
    legacy_boolean,
    legacy_boolean_sequence,
    legacy_node_set,
    legacy_number,
    legacy_number_from_string,
    legacy_number_sequence,
    legacy_round,
    legacy_string,
    legacy_string_sequence,
    number_to_string,
)
from pyxsd.xdm.nodes import NodeKind, XdmDocument
from pyxsd.xpath.engine import _legacy_number_value, _legacy_string_value
from pyxsd.xslt import XsltExtensions, XsltProcessor
from pyxsd.xslt.compiler.context import ProfileOptions, StaticContext
from pyxsd.xslt.compiler.expressions import compile_sequence_type
from pyxsd.xslt.errors import XsltDynamicError, XsltStaticError
from pyxsd.xslt.profiles import (
    DOCUMENT_RETRIEVAL,
    LanguageProfile,
    RecoveryPolicy,
    system_property_for,
)
from pyxsd.xslt.python_rules import PythonRuleContext, PythonRules

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
MY_NS = "urn:my"

stylesheet = partial(_stylesheet, namespaces={"my": MY_NS})
NATIVE = {"language": "1.0", "xpath": "1.0"}

SOURCE = b"<doc><a>1</a><b>two</b></doc>"
ORDERED_SOURCE = b"<doc><a><n>1</n></a><b><n>2</n></b></doc>"


def native_processor(**options: Any) -> XsltProcessor:
    return XsltProcessor(**NATIVE, **options)


def native_compile(
    body: str,
    *,
    version: str = "1.0",
    attributes: str = "",
    **options: Any,
) -> Any:
    processor = native_processor(**options)
    return processor.compile(io.BytesIO(stylesheet(body, version=version, attributes=attributes)))


def native_transform(
    body: str,
    *,
    version: str = "1.0",
    source: bytes = SOURCE,
    delivery: str = "raw",
    **options: Any,
) -> Any:
    compiled = native_compile(body, version=version)
    options.setdefault("initial_template", "main")
    return compiled.transform(source=source, delivery=delivery, **options)


def static_error(body: str, **options: Any) -> XsltStaticError:
    with pytest.raises(XsltStaticError) as caught:
        native_compile(body, **options)
    return caught.value


def principal_text(result: Any) -> str:
    return "".join(item.string_value for item in result.principal)


def text_of(body: str, *, source: bytes = SOURCE) -> str:
    return principal_text(native_transform(body, source=source))


def element_text(node: Any) -> str:
    return "".join(child.string_value for child in node.children if child.kind is NodeKind.TEXT)


def system_property(processor: XsltProcessor, name: str) -> str:
    compiled = processor.compile(
        io.BytesIO(
            stylesheet(
                '<xsl:template name="main">'
                f"<xsl:value-of select=\"system-property('{name}')\"/>"
                "</xsl:template>",
                version="1.0",
            )
        )
    )
    return compiled.transform(
        initial_template="main", delivery="document"
    ).principal_tree.string_value


def numeric_result(processor: XsltProcessor, body: str) -> str:
    compiled = processor.compile(
        io.BytesIO(stylesheet(f'<xsl:template name="main">{body}</xsl:template>', version="1.0"))
    )
    return principal_text(compiled.transform(initial_template="main", delivery="raw"))


# ---------------------------------------------------------------------------
# The brief's native data-model acceptance test


def test_native_xslt10_data_model(tmp_path: Path) -> None:
    native = '<xsl:template match="/"><out><xsl:value-of select="count(//a)"/></out></xsl:template>'
    (tmp_path / "native-10.xsl").write_text(
        stylesheet(native, version="1.0").decode(), encoding="utf-8"
    )
    rtf_error = (
        '<xsl:template match="/">'
        '<xsl:variable name="fragment"><b>text</b></xsl:variable>'
        '<xsl:apply-templates select="$fragment"/>'
        "</xsl:template>"
    )
    (tmp_path / "rtf-nodeset-error.xsl").write_text(
        stylesheet(rtf_error, version="1.0").decode(), encoding="utf-8"
    )

    processor = XsltProcessor(language="1.0", xpath="1.0")
    assert (
        principal_text(processor.compile(str(tmp_path / "native-10.xsl")).transform(SOURCE)) == "1"
    )
    with pytest.raises(XsltDynamicError, match="result tree fragment"):
        processor.compile(str(tmp_path / "rtf-nodeset-error.xsl")).transform(SOURCE)

    ieee_case = (
        '<xsl:value-of select="round(-2.5)"/><xsl:text>|</xsl:text>'
        "<xsl:value-of select=\"number('')\"/><xsl:text>|</xsl:text>"
        '<xsl:value-of select="1 div 0"/>'
    )
    assert numeric_result(processor, ieee_case) == "-2|NaN|Infinity"
    assert system_property(processor, "xsl:version") == "1.0"


# ---------------------------------------------------------------------------
# Processor identity


def test_native_processor_identity_and_compilation() -> None:
    processor = native_processor()
    assert processor.language == "1.0"
    assert processor.xpath == "1.0"

    compiled = native_compile('<xsl:template name="main"><out>ok</out></xsl:template>')
    assert compiled.language == "1.0"
    assert compiled.xpath == "1.0"
    assert compiled.profile.processor_profile == "xslt-1.0"
    result = compiled.transform(source=b"<doc/>", initial_template="main", delivery="document")
    assert element_text(result.principal_tree.children[0]) == "ok"

    version = native_transform(
        '<xsl:template name="main">'
        "<xsl:value-of select=\"system-property('xsl:version')\"/>"
        "</xsl:template>"
    )
    assert principal_text(version) == "1.0"


def test_native_processor_requires_matching_xpath() -> None:
    with pytest.raises(ValueError, match=r"requires XPath 1\.0"):
        XsltProcessor(language="1.0")
    with pytest.raises(ValueError, match=r"requires XPath 1\.0"):
        XsltProcessor(language="1.0", xpath="3.1")
    with pytest.raises(ValueError, match="unsupported XSLT language"):
        XsltProcessor(language="4.0", xpath="3.1")
    assert XsltProcessor(language="1.0", xpath="1.0").language == "1.0"


def test_distinct_processor_identities_coexist() -> None:
    body = (
        '<xsl:template name="main">'
        "<xsl:value-of select=\"system-property('xsl:version')\"/>"
        "</xsl:template>"
    )
    compatibility = XsltProcessor().compile(io.BytesIO(stylesheet(body, version="1.0")))
    native = native_processor().compile(io.BytesIO(stylesheet(body, version="1.0")))
    assert principal_text(compatibility.transform(initial_template="main", delivery="raw")) == "3.0"
    assert principal_text(native.transform(initial_template="main", delivery="raw")) == "1.0"


def test_native_language_profile() -> None:
    profile = LanguageProfile.for_processor("1.0", "1.0")
    assert profile.native is True
    assert profile.legacy is True
    assert profile.compatibility_mode is True
    assert profile.xpath_version == "1.0"
    assert profile.regex_flags == frozenset({"i", "m", "s", "x"})
    assert profile.recovery_rules == frozenset()
    assert system_property_for(version="1.0", processor="1.0") == "1.0"
    assert LanguageProfile.for_processor("3.0", "1.0").legacy is False


def test_native_system_properties() -> None:
    processor = native_processor()
    assert system_property(processor, "Q{http://www.w3.org/1999/XSL/Transform}version") == "1.0"
    assert system_property(processor, "xsl:xpath-version") == "1.0"
    assert system_property(processor, "xsl:is-schema-aware") == "no"
    assert system_property(processor, "xsl:supports-backwards-compatibility") == ""
    assert system_property(processor, "xsl:supports-namespace-axis") == ""
    assert system_property(processor, "xsl:supports-dynamic-evaluation") == "no"
    assert system_property(processor, "xsl:supports-higher-order-functions") == "no"
    assert system_property(processor, "xsl:vendor") == "pyxsd"
    assert system_property(processor, "xsl:vendor-url") == "https://pyxsd.knorby.com"
    assert system_property(processor, "xsl:xsd-version") == ""
    assert system_property(processor, "version") == ""
    assert system_property(processor, "xsl:unknown") == ""


def test_native_recovery_is_not_inherited() -> None:
    profile = LanguageProfile.for_processor("1.0", "1.0")
    policy = RecoveryPolicy(rules={DOCUMENT_RETRIEVAL.name})
    assert policy.allows(DOCUMENT_RETRIEVAL, profile) is False
    assert profile.recovery_rule_for(DOCUMENT_RETRIEVAL) is None
    assert profile.recovery_rule_for("document-retrieval") is None
    assert profile.permits_recovery(DOCUMENT_RETRIEVAL) is False


# ---------------------------------------------------------------------------
# The XSLT 1.0 element and attribute vocabulary


@pytest.mark.parametrize(
    "declaration",
    [
        '<xsl:import-schema namespace="urn:schema"/>',
        '<xsl:package name="urn:p" version="1.0"/>',
        '<xsl:use-package name="urn:p"/>',
        '<xsl:mode name="m"/>',
        '<xsl:character-map name="cm"><xsl:output-character character="a" string="b"/></xsl:character-map>',
        '<xsl:accumulator name="a" initial-value="0"/>',
        '<xsl:function name="my:f" xmlns:my="urn:my"/>',
    ],
)
def test_native_rejects_later_top_level_elements(declaration: str) -> None:
    body = f'{declaration}<xsl:template name="main"/>'
    error = static_error(body)
    # ``xsl:use-package`` is also rejected by the package validator before the
    # language gate runs; both answers name the same unsupported feature.
    assert error.code in ("XTSE0010", "XTSE3000")


@pytest.mark.parametrize(
    "instruction",
    [
        '<xsl:for-each-group select="1" group-by="."/>',
        '<xsl:analyze-string select="\'a\'" regex="a"/>',
        '<xsl:result-document href="out.xml"/>',
        '<xsl:sequence select="1"/>',
        "<xsl:next-match/>",
        '<xsl:perform-sort select="1"/>',
        '<xsl:document><xsl:sequence select="1"/></xsl:document>',
        '<xsl:map><xsl:map-entry key="\'k\'" select="1"/></xsl:map>',
        "<xsl:evaluate xpath=\"'1'\"/>",
        '<xsl:try><xsl:sequence select="1"/></xsl:try>',
        '<xsl:iterate select="1"/>',
        "<xsl:fork/>",
        "<xsl:merge/>",
        '<xsl:namespace name="n">value</xsl:namespace>',
        "<xsl:where-populated/>",
        "<xsl:on-empty/>",
    ],
)
def test_native_rejects_later_instruction_elements(instruction: str) -> None:
    error = static_error(f'<xsl:template name="main">{instruction}</xsl:template>')
    assert error.code == "XTSE0010"


@pytest.mark.parametrize(
    "declaration",
    [
        '<xsl:variable name="v" as="xs:string" select="\'x\'"/>',
        '<xsl:param name="p" as="xs:string" select="\'x\'"/>',
        '<xsl:template name="t" as="xs:string"/>',
        '<xsl:template name="t" visibility="public"/>',
        '<xsl:template name="t" streamable="yes"/>',
        '<xsl:variable name="v" select="1" static="yes"/>',
        '<xsl:output method="xml" name="fmt"/>',
        '<xsl:output method="xml" normalization-form="NFC"/>',
        '<xsl:output method="xml" byte-order-mark="yes"/>',
        '<xsl:output method="xml" escape-uri-attributes="yes"/>',
        '<xsl:output method="xml" include-content-type="yes"/>',
        '<xsl:output method="xml" undeclare-prefixes="yes"/>',
        '<xsl:output method="xml" use-character-maps="cm"/>',
        '<xsl:output method="xml" suppress-indentation="a"/>',
        '<xsl:output method="xml" item-separator="|"/>',
        '<xsl:output method="xml" json-node-output-method="xml"/>',
        '<xsl:output method="xml" build-tree="yes"/>',
        '<xsl:output method="xml" allow-duplicate-names="yes"/>',
        '<xsl:output method="xml" html-version="5"/>',
        '<xsl:output method="xml" parameter-document="urn:p"/>',
        '<xsl:template name="main" default-mode="m"/>',
        '<xsl:template name="main" expand-text="yes"/>',
        '<xsl:template name="main" default-validation="strict"/>',
    ],
)
def test_native_rejects_later_attributes(declaration: str) -> None:
    body = (
        f'{declaration}<xsl:template name="main"/>'
        if 'name="main"' not in declaration
        else declaration
    )
    error = static_error(body)
    # A named character map is also validated against the package before the
    # language gate sees the attribute; either answer rejects the 2.0 surface.
    assert error.code in ("XTSE0090", "XTSE1590")


@pytest.mark.parametrize(
    "declaration",
    [
        '<xsl:sort select="." stable="yes"/>',
        '<xsl:sort select="." collation="urn:c"/>',
        '<xsl:element name="e" type="xs:string"/>',
        '<xsl:attribute name="a" type="xs:string"/>',
        '<xsl:copy copy-accumulators="yes"/>',
        '<xsl:copy-of select="/" copy-accumulators="yes"/>',
        '<xsl:number value="1" start-at="2"/>',
        '<xsl:for-each select="1" bind-group="yes"/>',
    ],
)
def test_native_rejects_later_instruction_attributes(declaration: str) -> None:
    error = static_error(f'<xsl:template name="main">{declaration}</xsl:template>')
    assert error.code == "XTSE0090"


@pytest.mark.parametrize("method", ["xhtml", "json", "adaptive", "my:xml", "Q{urn:my}xml", "TEXT"])
def test_native_rejects_later_output_methods(method: str) -> None:
    error = static_error(f'<xsl:output method="{method}"/><xsl:template name="main"/>')
    assert error.code == "XTSE0020"


def test_native_rejects_dynamic_output_method() -> None:
    error = static_error(
        "<xsl:output method=\"{concat('x', 'ml')}\"/><xsl:template name=\"main\"/>"
    )
    assert error.code == "XTSE0020"


@pytest.mark.parametrize("mode", ["#default", "#all", "#current", "#unnamed"])
def test_native_rejects_2_0_mode_tokens(mode: str) -> None:
    error = static_error(f'<xsl:template name="main" mode="{mode}"/>')
    # The compiler's own mode validation also rejects these before the gate.
    assert error.code in ("XTSE0550", "XTSE0500")
    error = static_error(
        f'<xsl:template name="main"><xsl:apply-templates mode="{mode}"/></xsl:template>'
    )
    assert error.code in ("XTSE0550", "XTSE0500")


def test_native_accepts_the_1_0_vocabulary() -> None:
    body = (
        '<xsl:output method="xml" version="1.0" encoding="UTF-8" '
        'omit-xml-declaration="yes" standalone="yes" '
        'doctype-public="-//W3C//DTD XHTML 1.0//EN" doctype-system="x.dtd" '
        'cdata-section-elements="x" indent="yes" media-type="text/xml"/>'
        '<xsl:strip-space elements="unused"/>'
        '<xsl:preserve-space elements="doc"/>'
        '<xsl:key name="k" match="a" use="."/>'
        '<xsl:decimal-format name="d" decimal-separator="." grouping-separator=","/>'
        '<xsl:attribute-set name="s"><xsl:attribute name="a">v</xsl:attribute></xsl:attribute-set>'
        '<xsl:namespace-alias stylesheet-prefix="my" result-prefix="my"/>'
        '<xsl:template name="named">'
        '<xsl:param name="p" select="\'d\'"/>'
        '<xsl:value-of select="$p"/>'
        "</xsl:template>"
        '<xsl:template match="/" name="main">'
        '<xsl:variable name="v" select="1"/>'
        '<xsl:if test="$v">'
        '<xsl:choose><xsl:when test="true()">'
        "<out>"
        '<xsl:call-template name="named">'
        '<xsl:with-param name="p" select="\'w\'"/>'
        "</xsl:call-template>"
        '<xsl:for-each select="//a"><xsl:sort select="." data-type="text" order="ascending" '
        'case-order="lower-first" lang="en"/>'
        '<xsl:copy><xsl:copy-of select="."/></xsl:copy>'
        "</xsl:for-each>"
        '<xsl:number value="1" format="1" lang="en" letter-value="alphabetic" '
        'grouping-separator="," grouping-size="3"/>'
        '<xsl:number level="single" count="a" from="doc" format="1"/>'
        "<xsl:comment>c</xsl:comment>"
        '<xsl:processing-instruction name="pi">p</xsl:processing-instruction>'
        '<xsl:element name="e" namespace="urn:e" use-attribute-sets="s"/>'
        '<xsl:attribute name="x" namespace="urn:x">v</xsl:attribute>'
        '<xsl:text disable-output-escaping="no">t</xsl:text>'
        "</out>"
        "</xsl:when><xsl:otherwise><bad/></xsl:otherwise></xsl:choose>"
        "</xsl:if>"
        '<xsl:message terminate="no">m</xsl:message>'
        "</xsl:template>"
    )
    assert native_compile(body) is not None


def test_native_ignores_foreign_namespace_attributes() -> None:
    body = (
        '<xsl:template name="main" xmlns:x="urn:x" x:extension="yes"><out>ok</out></xsl:template>'
    )
    assert native_compile(body) is not None


# ---------------------------------------------------------------------------
# XPath 1.0 grammar and function set


@pytest.mark.parametrize(
    "expression",
    [
        "for $x in 1 return $x",
        "some $x in 1 satisfies true()",
        "every $x in 1 satisfies true()",
        "if (true()) then 1 else 2",
        "1 to 3",
        "(1, 2)",
        "map {'a': 1}",
        "[1, 2]",
        "'a' || 'b'",
        "1 ! .",
        "1 => string()",
        "1 instance of xs:integer",
        "1 cast as xs:string",
        "1 treat as xs:integer",
        "-1 ? 0",
        "Q{urn:x}name",
        "1 intersect 2",
        "1 except 2",
        "1 = 1 = 1",
    ],
)
def test_native_rejects_later_xpath_grammar(expression: str) -> None:
    error = static_error(
        f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
    )
    assert error.code in ("XPST0003", "XPST0017", "XPST0081")


def test_native_accepts_the_xpath_1_0_grammar() -> None:
    expression = (
        "concat('a', 'b') = 'ab' and not(false()) or (1 + 2 * 3 - 4 div 2 mod 3 > 0)"
        " and (//a | //b)[position() = last()]"
    )
    assert text_of(
        f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
    ) in ("true", "false")
    axes = (
        "count(descendant::*) + count(child::doc) + count(ancestor::node())"
        " + count(following::*) + count(preceding::*) + count(self::node())"
        " + count(attribute::*) + count(namespace::*)"
    )
    assert (
        text_of(
            f'<xsl:template name="main"><xsl:value-of select="{axes}"/></xsl:template>',
            source=b'<doc xmlns:p="urn:p" id="1"><a/></doc>',
        )
        == "4"
    )


def test_native_xpath_1_0_function_set() -> None:
    body = (
        '<xsl:template name="main">'
        "<xsl:value-of select=\"function-available('fn:matches')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"function-available('fn:replace')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"function-available('fn:tokenize')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"function-available('fn:key')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"function-available('fn:generate-id')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"function-available('fn:format-number')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"function-available('fn:system-property')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"function-available('fn:document')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"function-available('fn:current')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"function-available('fn:doc')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"function-available('fn:count')\"/>"
        "</xsl:template>"
    )
    assert text_of(body) == "false|false|false|true|true|true|true|true|true|false|true"


def test_native_evaluates_the_xpath_1_0_core_functions() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:value-of select="last()"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="position()"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="count(//a)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="local-name(/*)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="namespace-uri(/*)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="name(/*)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string(//b)"/>'
        "<xsl:text>|</xsl:text><xsl:value-of select=\"concat('a', 'b')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"starts-with('abc', 'a')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"contains('abc', 'b')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"substring-before('a-b', '-')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"substring-after('a-b', '-')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"substring('12345', 1.5, 2.6)\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"string-length('abc')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"normalize-space('  a  b ')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"translate('abc', 'ac', 'AC')\"/>"
        '<xsl:text>|</xsl:text><xsl:value-of select="boolean(//a)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="not(false())"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="true()"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="false()"/>'
        "<xsl:text>|</xsl:text><xsl:value-of select=\"lang('en')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"number('4')\"/>"
        '<xsl:text>|</xsl:text><xsl:value-of select="sum(//a)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="floor(1.9)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="ceiling(1.1)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="round(1.5)"/>'
        "</xsl:template>"
    )
    result = text_of(body)
    assert result.split("|")[0:4] == ["1", "1", "1", "doc"]
    assert result == (
        "1|1|1|doc||doc|two|ab|true|true|a|b|234|3|a b|AbC|true|true|true|false|false|4|1|1|2|2"
    )


def test_native_rejects_later_sequence_type_syntax() -> None:
    context = StaticContext(profile=ProfileOptions(processor_profile="xslt-1.0"))
    assert context.xpath_version == "1.0"
    with pytest.raises(XsltStaticError) as caught:
        compile_sequence_type("(xs:string)", context)
    assert caught.value.code == "XPST0003"


# ---------------------------------------------------------------------------
# XPath 1.0 conversions and IEEE-754 number semantics


def test_native_string_conversions() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:value-of select="string(1 div 0)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string(-1 div 0)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string(0 div 0)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string(2.0)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string(0.5)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string(-0)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string(1 div 10000000)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string(true())"/>'
        "</xsl:template>"
    )
    assert text_of(body) == "Infinity|-Infinity|NaN|2|0.5|0|0.0000001|true"


def test_native_number_conversions() -> None:
    expressions = {
        "number('')": "NaN",
        "number('  ')": "NaN",
        "number('1e3')": "NaN",
        "number('Infinity')": "NaN",
        "number('+1')": "NaN",
        "number('-2.5')": "-2.5",
        "number(' 12 ')": "12",
        "number('.5')": "0.5",
        "number(true())": "1",
        "number(false())": "0",
    }
    for expression, expected in expressions.items():
        assert (
            text_of(
                f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
            )
            == expected
        ), expression


def test_native_ieee_rounding_and_special_values() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:value-of select="round(2.5)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="round(-2.5)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="round(-0.5)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="round(0 div 0)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="round(1 div 0)"/>'
        "</xsl:template>"
    )
    assert text_of(body) == "3|-2|0|NaN|Infinity"


def test_native_general_comparison_conversions() -> None:
    body = (
        '<xsl:template name="main">'
        "<xsl:value-of select=\"1 = '1'\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"'1' != 1\"/>"
        '<xsl:text>|</xsl:text><xsl:value-of select="true() = 1"/>'
        "<xsl:text>|</xsl:text><xsl:value-of select=\"'10' &gt; '9'\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"1 = 'x'\"/>"
        '<xsl:text>|</xsl:text><xsl:value-of select="0 div 0 = 0 div 0"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="0 div 0 != 0 div 0"/>'
        "</xsl:template>"
    )
    assert text_of(body) == "true|false|true|true|false|false|true"


def test_native_boolean_conversions() -> None:
    body = (
        '<xsl:template name="main">'
        "<xsl:value-of select=\"boolean('')\"/>"
        '<xsl:text>|</xsl:text><xsl:value-of select="boolean(0 div 0)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="boolean(0)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="boolean(-0)"/>'
        "<xsl:text>|</xsl:text><xsl:value-of select=\"boolean('x')\"/>"
        '<xsl:text>|</xsl:text><xsl:value-of select="boolean(//a)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="boolean(//missing)"/>'
        "</xsl:template>"
    )
    assert text_of(body) == "false|false|false|false|true|true|false"


def test_native_string_function_number_conversions() -> None:
    body = (
        '<xsl:template name="main">'
        "<xsl:value-of select=\"concat(1 div 0, '')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"contains(1 div 0, 'Infinity')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"starts-with(1 div 0, 'Inf')\"/>"
        '<xsl:text>|</xsl:text><xsl:value-of select="substring(1 div 0, 1, 8)"/>'
        "<xsl:text>|</xsl:text><xsl:value-of select=\"substring-before(1 div 0, 'init')\"/>"
        "<xsl:text>|</xsl:text><xsl:value-of select=\"substring-after(1 div 0, 'Inf')\"/>"
        '<xsl:text>|</xsl:text><xsl:value-of select="string-length(1 div 0)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="normalize-space(1 div 0)"/>'
        "<xsl:text>|</xsl:text><xsl:value-of select=\"translate(1 div 0, 'I', 'i')\"/>"
        '<xsl:text>|</xsl:text><xsl:value-of select="string-length()"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="normalize-space()"/>'
        "</xsl:template>"
    )
    assert text_of(body, source=b"<doc>  1  </doc>") == (
        "Infinity|true|true|Infinity|Inf|inity|8|Infinity|infinity|5|1"
    )


def test_native_no_argument_string_and_number() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:value-of select="string()"/>'
        "<xsl:text>|</xsl:text>"
        '<xsl:value-of select="number()"/>'
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"concat(//missing, 'x')\"/>"
        "</xsl:template>"
    )
    assert text_of(body, source=b"<doc> 12 </doc>") == " 12 |12|x"


def test_native_substring_rounding_and_edges() -> None:
    cases = {
        "substring('12345', 1.5, 2.6)": "234",
        "substring('12345', 2.5, 2.5)": "345",
        "substring('12345', 2)": "2345",
        "substring('12345', 0 div 0)": "",
        "substring('12345', 1 div 0)": "",
        "substring('12345', 1, 0 div 0)": "",
        "substring('12345', 1, 0)": "",
        "substring('12345', 1, 1 div 0)": "12345",
        "substring('12345', 0, 3)": "12",
        "substring('12345', -1, 2)": "",
    }
    for expression, expected in cases.items():
        assert (
            text_of(
                f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
            )
            == expected
        ), expression


def test_native_translate_mapping_lengths() -> None:
    cases = {
        "translate('abc', 'abc', 'ABC')": "ABC",
        "translate('abc', 'abc', 'A')": "A",
        "translate('abc', 'a', 'AB')": "Abc",
    }
    for expression, expected in cases.items():
        assert (
            text_of(
                f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
            )
            == expected
        ), expression


def test_native_sum_conversions() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:value-of select="sum(//a)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="sum(//missing)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="sum(//bad)"/>'
        "</xsl:template>"
    )
    assert text_of(body, source=b"<doc><a>1</a><a>2</a><bad>1e3</bad></doc>") == "3|0|NaN"


# ---------------------------------------------------------------------------
# Node-set identity and document order


def test_native_node_sets_use_identity_and_document_order() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:value-of select="count(//n)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="count(//n | //n)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="count(//n | //a)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="name((//n | //a)[1])"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="(//n)[2]"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string(//n)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="number(//n)"/>'
        "</xsl:template>"
    )
    assert text_of(body, source=ORDERED_SOURCE) == "2|2|3|a|2|1|1"


def test_native_document_order_across_reverse_axes() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:value-of select="name((//n)[2]/preceding::*[1])"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="name(//a/following::*[1])"/>'
        "</xsl:template>"
    )
    assert text_of(body, source=ORDERED_SOURCE) == "n|b"


def test_native_document_function_returns_a_node_set(tmp_path: Path) -> None:
    loaded = tmp_path / "loaded.xml"
    loaded.write_text("<root><item>loaded</item></root>", encoding="utf-8")
    uri = loaded.as_uri()
    straight = (
        '<xsl:template name="main">'
        f"<xsl:value-of select=\"document('{uri}')/root/item\"/>"
        "</xsl:template>"
    )
    assert text_of(straight) == "loaded"
    variable = (
        '<xsl:template name="main">'
        f'<xsl:variable name="d" select="document(\'{uri}\')"/>'
        '<xsl:value-of select="count($d/root/item)"/>'
        "</xsl:template>"
    )
    assert text_of(variable) == "1"


def test_native_xslt_1_0_functions() -> None:
    body = (
        '<xsl:key name="k" match="a" use="."/>'
        '<xsl:template name="main">'
        "<xsl:value-of select=\"count(key('k', '1'))\"/>"
        "<xsl:text>|</xsl:text>"
        '<xsl:value-of select="string-length(generate-id(//a)) &gt; 0"/>'
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"format-number(1234.5, '#,##0.00')\"/>"
        "<xsl:text>|</xsl:text>"
        '<xsl:value-of select="count(current())"/>'
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"element-available('xsl:for-each')\"/>"
        "<xsl:text>|</xsl:text>"
        "<xsl:value-of select=\"element-available('xsl:for-each-group')\"/>"
        "</xsl:template>"
    )
    assert text_of(body) == "1|true|1,234.50|1|true|false"


# ---------------------------------------------------------------------------
# Result tree fragments


def test_native_result_tree_fragment_conversions() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="fragment"><b>one</b><i>two</i></xsl:variable>'
        '<xsl:value-of select="$fragment"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string($fragment)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="boolean($fragment)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="not($fragment)"/>'
        "<xsl:text>|</xsl:text><xsl:value-of select=\"$fragment = 'onetwo'\"/>"
        '<xsl:text>|</xsl:text><xsl:value-of select="number($fragment) + 1"/>'
        "</xsl:template>"
    )
    assert text_of(body) == "onetwo|onetwo|true|false|true|NaN"


def test_native_empty_result_tree_fragment_is_true() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="fragment"></xsl:variable>'
        '<xsl:value-of select="boolean($fragment)"/>'
        '<xsl:text>|</xsl:text><xsl:value-of select="string($fragment)"/>'
        "<xsl:text>|</xsl:text><xsl:value-of select=\"$fragment = ''\"/>"
        "</xsl:template>"
    )
    assert text_of(body) == "true||true"


def test_native_value_of_and_attribute_value_templates_stringify_a_fragment() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="fragment"><b>text</b></xsl:variable>'
        '<out a="{$fragment}"><xsl:value-of select="$fragment"/></out>'
        "</xsl:template>"
    )
    result = native_transform(body, delivery="document").principal_tree
    out = result.children[0]
    assert out.attribute("a") is not None
    assert out.attribute("a").string_value == "text"
    assert element_text(out) == "text"


def test_native_copy_of_a_fragment_copies_its_children() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="fragment"><b>one</b><i>two</i></xsl:variable>'
        '<out><xsl:copy-of select="$fragment"/></out>'
        "</xsl:template>"
    )
    serialized = native_transform(body).serialize()
    assert "<b>one</b><i>two</i></out>" in serialized
    assert "result" not in serialized


def test_native_global_variable_is_a_result_tree_fragment() -> None:
    body = (
        '<xsl:variable name="fragment"><b>global</b></xsl:variable>'
        '<xsl:template name="main">'
        '<xsl:value-of select="boolean($fragment)"/>'
        "</xsl:template>"
    )
    assert text_of(body) == "true"


def test_native_global_fragment_restriction_at_runtime() -> None:
    body = (
        '<xsl:variable name="fragment"><b>global</b></xsl:variable>'
        '<xsl:template name="main"><xsl:apply-templates select="$fragment"/></xsl:template>'
    )
    compiled = native_compile(body)
    with pytest.raises(XsltDynamicError, match="result tree fragment"):
        compiled.transform(initial_template="main")


def test_native_parameter_default_is_a_result_tree_fragment() -> None:
    body = (
        '<xsl:template name="named">'
        '<xsl:param name="p"><b>default</b></xsl:param>'
        '<xsl:value-of select="boolean($p)"/>'
        "</xsl:template>"
        '<xsl:template name="main"><xsl:call-template name="named"/></xsl:template>'
    )
    assert text_of(body) == "true"


def test_native_with_parameter_copies_a_fragment() -> None:
    body = (
        '<xsl:template name="named">'
        '<xsl:param name="p"/>'
        '<xsl:value-of select="$p"/>'
        "</xsl:template>"
        '<xsl:template name="main">'
        '<xsl:variable name="fragment"><b>passed</b></xsl:variable>'
        '<xsl:call-template name="named"><xsl:with-param name="p" select="$fragment"/></xsl:call-template>'
        "</xsl:template>"
    )
    assert text_of(body) == "passed"


def test_native_fragment_boolean_in_conditionals_and_sort() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="fragment"><b>x</b></xsl:variable>'
        '<xsl:if test="$fragment"><xsl:text>if</xsl:text></xsl:if>'
        "<xsl:choose>"
        '<xsl:when test="$fragment"><xsl:text>when</xsl:text></xsl:when>'
        "<xsl:otherwise><xsl:text>otherwise</xsl:text></xsl:otherwise>"
        "</xsl:choose>"
        '<xsl:for-each select="//b"><xsl:sort select="$fragment"/>'
        '<xsl:value-of select="."/></xsl:for-each>'
        "</xsl:template>"
    )
    assert text_of(body) == "ifwhentwo"


def test_native_rejects_a_fragment_where_a_node_set_is_required() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="fragment"><b>text</b></xsl:variable>'
        "<out>"
        '<xsl:value-of select="count($fragment)"/>'
        "</out>"
        "</xsl:template>"
    )
    with pytest.raises(XsltDynamicError, match="result tree fragment"):
        native_transform(body)


def test_native_rejects_a_fragment_in_for_each() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="fragment"><b>text</b></xsl:variable>'
        '<xsl:for-each select="$fragment"><xsl:value-of select="."/></xsl:for-each>'
        "</xsl:template>"
    )
    with pytest.raises(XsltDynamicError, match="result tree fragment"):
        native_transform(body)


def test_native_rejects_a_fragment_in_a_path() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="fragment"><b>text</b></xsl:variable>'
        '<xsl:value-of select="$fragment/b"/>'
        "</xsl:template>"
    )
    with pytest.raises(Exception, match="Intermediate step contains an atomic value"):
        native_transform(body)


def test_native_rejects_a_fragment_in_id() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="fragment"><b>text</b></xsl:variable>'
        '<xsl:value-of select="count(id($fragment))"/>'
        "</xsl:template>"
    )
    with pytest.raises(XsltDynamicError, match="fn:id"):
        native_transform(body)
    assert (
        text_of(
            '<xsl:template name="main"><xsl:value-of select="count(id(\'missing\'))"/></xsl:template>'
        )
        == "0"
    )


def test_native_empty_sequence_conversions() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:value-of select="string(//missing)"/>'
        "<xsl:text>|</xsl:text>"
        '<xsl:value-of select="number(//missing)"/>'
        "</xsl:template>"
    )
    assert text_of(body) == "|NaN"


def test_native_does_not_provide_exslt_node_set() -> None:
    body = (
        '<xsl:template name="main" xmlns:exsl="http://exslt.org/common">'
        '<xsl:value-of select="exsl:node-set(//a)"/>'
        "</xsl:template>"
    )
    error = static_error(body)
    assert error.code == "XPST0017"
    assert (
        text_of(
            '<xsl:template name="main" xmlns:exsl="http://exslt.org/common">'
            "<xsl:value-of select=\"function-available('exsl:node-set')\"/>"
            "</xsl:template>"
        )
        == "false"
    )


# ---------------------------------------------------------------------------
# No schema annotations, maps, or packages


def test_native_has_no_schema_annotations() -> None:
    for declaration in (
        '<xsl:import-schema namespace="urn:schema"/>',
        '<xsl:element name="e" type="xs:string"/>',
        '<xsl:attribute name="a" type="xs:string"/>',
        '<xsl:variable name="v" as="xs:string" select="\'x\'"/>',
        '<xsl:copy validation="strict" copy-namespaces="no"/>',
    ):
        error = static_error(f'<xsl:template name="main">{declaration}</xsl:template>')
        assert error.code in ("XTSE0010", "XTSE0090")


def test_native_rejects_maps_arrays_and_packages() -> None:
    declarations = (
        "map {'a': 1}",
        "[1, 2]",
        "array { 1, 2 }",
        "map:merge(())",
    )
    for expression in declarations:
        error = static_error(
            f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
        )
        assert error.code in ("XPST0003", "XPST0017", "XPST0081")
    assert static_error('<xsl:package name="urn:p" version="1.0"/>').code == "XTSE0010"
    assert static_error('<xsl:map/><xsl:template name="main"/>').code == "XTSE0010"


# ---------------------------------------------------------------------------
# Serialization 1.0


def test_native_serialization_1_0_methods() -> None:
    xml = native_transform(
        '<xsl:output method="xml" indent="no"/>'
        '<xsl:template name="main"><out>ok</out></xsl:template>'
    ).serialize()
    assert xml.startswith('<?xml version="1.0"')
    html = native_transform(
        '<xsl:output method="html" indent="no"/>'
        '<xsl:template name="main"><out>ok</out></xsl:template>'
    ).serialize()
    assert not html.startswith("<?xml")
    text = native_transform(
        '<xsl:output method="text"/><xsl:template name="main"><out>ok</out></xsl:template>'
    ).serialize()
    assert text.strip() == "ok"


def test_native_serialization_1_0_parameters() -> None:
    assert native_compile('<xsl:output/><xsl:template name="main"/>') is not None
    serialized = native_transform(
        '<xsl:output method="xml" version="1.0" encoding="UTF-8" '
        'omit-xml-declaration="yes" standalone="yes" '
        'doctype-public="-//W3C//DTD XHTML 1.0//EN" doctype-system="x.dtd" '
        'cdata-section-elements="out" indent="yes" media-type="text/xml"/>'
        '<xsl:template name="main"><out>ok</out></xsl:template>'
    ).serialize()
    assert serialized.startswith("<!DOCTYPE out PUBLIC")
    assert "<?xml" not in serialized


# ---------------------------------------------------------------------------
# Namespaces


def test_native_namespaces_on_literal_result_elements() -> None:
    compiled = native_compile(
        '<xsl:template name="main"><p:out xmlns:p="urn:p"><p:inner/></p:out></xsl:template>',
        attributes=' exclude-result-prefixes="my"',
    )
    serialized = compiled.transform(
        source=SOURCE, initial_template="main", delivery="raw"
    ).serialize()
    assert '<p:out xmlns:p="urn:p"' in serialized
    assert "xmlns:my" not in serialized


def test_native_element_and_attribute_namespace_attributes() -> None:
    serialized = native_transform(
        '<xsl:template name="main">'
        '<xsl:element name="p:out" namespace="urn:p">'
        '<xsl:attribute name="p:a" namespace="urn:a">v</xsl:attribute>'
        "</xsl:element>"
        "</xsl:template>"
    ).serialize()
    assert "urn:p" in serialized
    assert "urn:a" in serialized
    assert 'p:a="v"' in serialized


# ---------------------------------------------------------------------------
# Python mode-rule customization


def _mode_rule_source() -> bytes:
    return stylesheet(
        '<xsl:template match="doc/item" mode="my:process" priority="1"><base-item/></xsl:template>'
        '<xsl:template match="doc" mode="my:process"><wrap>'
        '<xsl:apply-templates mode="my:process"/></wrap></xsl:template>',
        version="1.0",
    )


def _native_mode_rules(callback: Any) -> Any:
    extensions = XsltExtensions().with_python_rules(
        PythonRules.empty().add_mode_rule("my:process", "doc/item", callback)
    )
    compiled = native_processor().compile(io.BytesIO(_mode_rule_source()), extensions=extensions)
    assert compiled.profile.processor_profile == "xslt-1.0"
    return compiled


def _run_mode(compiled: Any) -> Any:
    selection = XdmDocument.parse(b"<doc><item>one</item></doc>").root_element
    return compiled.transform(
        initial_match_selection=selection,
        initial_mode="Q{urn:my}process",
        delivery="raw",
    )


def test_python_mode_rule_next_match_is_unsupported_under_native_1_0() -> None:
    def python_rule(context: PythonRuleContext) -> Any:
        return context.next_match()

    compiled = _native_mode_rules(python_rule)
    with pytest.raises(XsltDynamicError, match="next_match is not supported") as caught:
        _run_mode(compiled)
    assert caught.value.code == "XTDE0560"


def test_python_mode_rule_apply_imports_through_native_1_0(tmp_path: Path) -> None:
    imported = stylesheet(
        '<xsl:template match="doc/item" mode="my:process" priority="10"><imported/></xsl:template>'
        '<xsl:template match="doc" mode="my:process"><wrap>'
        '<xsl:apply-templates mode="my:process"/></wrap></xsl:template>',
        version="1.0",
    )
    principal = stylesheet(
        '<xsl:import href="base.xsl"/>'
        '<xsl:template match="doc/item" mode="my:process" priority="1">'
        "<kept/></xsl:template>"
        '<xsl:template match="doc" mode="my:process"><wrap>'
        '<xsl:apply-templates mode="my:process"/></wrap></xsl:template>',
        version="1.0",
    )
    (tmp_path / "base.xsl").write_bytes(imported)
    main = tmp_path / "main.xsl"
    main.write_bytes(principal)

    def python_rule(context: PythonRuleContext) -> Any:
        return context.apply_imports()

    extensions = XsltExtensions().with_python_rules(
        PythonRules.empty().add_mode_rule("my:process", "doc/item", python_rule)
    )
    compiled = native_processor().compile(main, extensions=extensions)
    assert compiled.profile.processor_profile == "xslt-1.0"
    result = _run_mode(compiled)
    assert "<kept/>" in result.serialize()
    assert "<imported/>" not in result.serialize()


# ---------------------------------------------------------------------------
# Direct legacy-model unit tests


def _ordered_nodes() -> tuple[Any, Any]:
    document = XdmDocument.parse(b"<doc><a/><b/></doc>")
    root = document.children[0]
    return root.children[0], root.children[1]


def test_legacy_result_tree_fragment_requires_a_document() -> None:
    document = XdmDocument.parse(b"<doc><a/></doc>")
    fragment = ResultTreeFragment(document)
    assert fragment.string_value == ""
    assert str(fragment) == ""
    with pytest.raises(TypeError, match="document node"):
        ResultTreeFragment("not a document")  # type: ignore[arg-type]


def test_legacy_node_set_deduplicates_and_orders() -> None:
    first, second = _ordered_nodes()
    assert legacy_node_set([second, first, second]) == (first, second)
    with pytest.raises(TypeError, match="only XDM nodes"):
        legacy_node_set([first, "x"])  # type: ignore[list-item]


def test_legacy_string_conversions() -> None:
    document = XdmDocument.parse(b"<doc><a>one</a><b>two</b></doc>")
    first, second = document.children[0].children[0], document.children[0].children[1]
    fragment = ResultTreeFragment(document)
    assert legacy_string(fragment) == "onetwo"
    assert legacy_string(first) == "one"
    assert legacy_string(XdmAtomic.string("s")) == "s"
    assert legacy_string(XdmAtomic.boolean(True)) == "true"
    assert legacy_string(XdmAtomic.double(float("inf"))) == "Infinity"
    assert legacy_string(XdmAtomic.double(-0.0)) == "0"
    assert legacy_string_sequence(()) == ""
    assert legacy_string_sequence((second, first)) == "one"
    assert legacy_string_sequence((XdmAtomic.integer(3), XdmAtomic.integer(4))) == "3"
    with pytest.raises(TypeError, match="string value"):
        legacy_string(object())  # type: ignore[arg-type]


def test_legacy_boolean_conversions() -> None:
    document = XdmDocument.parse(b"<doc><a/></doc>")
    node = document.children[0]
    fragment = ResultTreeFragment(document)
    assert legacy_boolean(fragment) is True
    assert legacy_boolean(node) is True
    assert legacy_boolean(XdmAtomic.string("")) is False
    assert legacy_boolean(XdmAtomic.string("x")) is True
    assert legacy_boolean(XdmAtomic.boolean(True)) is True
    assert legacy_boolean(XdmAtomic.boolean(False)) is False
    assert legacy_boolean(XdmAtomic.integer(0)) is False
    assert legacy_boolean(XdmAtomic.double(float("nan"))) is False
    assert legacy_boolean(XdmAtomic.double(1.0)) is True
    assert legacy_boolean_sequence(()) is False
    assert legacy_boolean_sequence((XdmAtomic.string("x"),)) is True
    with pytest.raises(TypeError, match="boolean value"):
        legacy_boolean(object())  # type: ignore[arg-type]


def test_legacy_number_conversions() -> None:
    document = XdmDocument.parse(b"<doc><a>12</a></doc>")
    node = document.children[0]
    fragment = ResultTreeFragment(document)
    assert legacy_number(fragment) == 12.0
    assert legacy_number(node) == 12.0
    assert legacy_number(XdmAtomic.string(" 2.5 ")) == 2.5
    assert legacy_number(XdmAtomic.boolean(True)) == 1.0
    assert legacy_number(XdmAtomic.boolean(False)) == 0.0
    assert legacy_number(XdmAtomic.integer(3)) == 3.0
    assert legacy_number_from_string("1e3") != legacy_number_from_string("1e3")
    assert legacy_number_sequence(()) != legacy_number_sequence(())
    with pytest.raises(TypeError, match="number value"):
        legacy_number(object())  # type: ignore[arg-type]


def test_legacy_number_string_round_trip() -> None:
    assert number_to_string(float("nan")) == "NaN"
    assert number_to_string(float("inf")) == "Infinity"
    assert number_to_string(float("-inf")) == "-Infinity"
    assert number_to_string(-0.0) == "0"
    assert number_to_string(2.0) == "2"
    assert number_to_string(0.5) == "0.5"
    assert number_to_string(0.0000001) == "0.0000001"
    assert legacy_round(-0.5) == -0.0
    assert legacy_round(2.5) == 3.0
    assert legacy_round(-2.5) == -2.0
    assert legacy_round(float("nan")) != legacy_round(float("nan"))
    assert legacy_round(float("inf")) == float("inf")
    assert is_legacy_number(1) is True
    assert is_legacy_number(True) is False
    assert is_legacy_number("1") is False


def test_legacy_decimal_and_evaluator_conversions() -> None:
    assert legacy_string(XdmAtomic.boolean(False)) == "false"
    assert legacy_string(XdmAtomic.decimal(Decimal("2.50"))) == "2.5"
    assert legacy_string(XdmAtomic.decimal(Decimal("2"))) == "2"
    assert _legacy_string_value(UntypedAtomic("text")) == "text"
    assert _legacy_string_value(None) == ""
    assert _legacy_number_value(UntypedAtomic("12")) == 12.0
    assert math.isnan(_legacy_number_value(None))
    assert math.isnan(_legacy_number_value(object()))
    assert legacy_number(XdmAtomic.boolean(False)) == 0.0
    assert legacy_number(XdmAtomic.boolean(True)) == 1.0


def test_legacy_node_set_error_code() -> None:
    assert LegacyNodeSetError.code == "XPTY0004"
