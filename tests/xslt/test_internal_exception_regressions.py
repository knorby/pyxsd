"""Regressions for raw internal exceptions that escaped public XSLT/XDM seams.

Each test names the W3C case family that exposed the failure and asserts the
specified public error code or result, not merely the absence of a Python
exception.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from tests.xslt.support import stylesheet, transform

from pyxsd.schema import Schema
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.packages import PackageResolver
from pyxsd.xslt.errors import XsltDynamicError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
FN_NS = "http://www.w3.org/2005/xpath-functions"
APP = "urn:example:app"
BASE = "urn:example:base"


def _serialized(source: bytes, input_source: bytes | None = None) -> str:
    initial = "xsl:initial-template" if b'name="xsl:initial-template"' in source else None
    result = transform(
        source,
        input_source,
        delivery="stylesheet",
        initial_template=initial,
    )
    serialized = result.principal_entry.serialized
    assert serialized is not None
    return serialized


def _code(call: object) -> str | None:
    with pytest.raises(Exception) as caught:
        call()  # type: ignore[operator]
    return str(getattr(caught.value, "code", "")).rsplit(":", 1)[-1]


def test_untyped_atomic_comparison_uses_expression_namespaces() -> None:
    # choose-0106: the local xmlns:my on xsl:when must cast the untyped value
    # to a QName in that branch's static context, not the parameter's.
    body = """
      <xsl:param name="u" select="xs:untypedAtomic('my:problem')"/>
      <xsl:param name="q" select="QName('http://uri.three/', 'problem')"/>
      <xsl:template name="xsl:initial-template" visibility="public">
        <out>
          <xsl:choose>
            <xsl:when test="$u = $q" xmlns:my="http://uri.one/">One</xsl:when>
            <xsl:when test="$u = $q" xmlns:my="http://uri.two/">Two</xsl:when>
            <xsl:when test="$u = $q" xmlns:my="http://uri.three/">Three</xsl:when>
            <xsl:otherwise>Fail</xsl:otherwise>
          </xsl:choose>
        </out>
      </xsl:template>
    """
    assert ">Three</out>" in _serialized(stylesheet(body))


def test_subsequence_empty_starting_position_is_XPTY0004() -> None:
    # function-1022: F&O requires one numeric starting position.
    body = """
      <xsl:template name="xsl:initial-template" visibility="public">
        <xsl:sequence select="subsequence((1, 2), ())"/>
      </xsl:template>
    """
    compiled = XsltProcessor().compile(io.BytesIO(stylesheet(body)))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="xsl:initial-template", delivery="raw")
    assert caught.value.code == "XPTY0004"


def test_format_integer_applies_integer_function_conversion() -> None:
    # function-1701: an untypedAtomic text node is cast to xs:integer, while a
    # genuine double is a public type error.
    body = """
      <xsl:template match="/">
        <out>
          <xsl:for-each select="doc/a"><a><xsl:value-of select="format-integer(., 'A')"/></a></xsl:for-each>
        </out>
      </xsl:template>
    """
    assert "<a>A</a><a>B</a>" in _serialized(stylesheet(body), b"<doc><a>1</a><a>2</a></doc>")
    body = """
      <xsl:template name="xsl:initial-template" visibility="public">
        <xsl:sequence select="format-integer(1.0, 'A')"/>
      </xsl:template>
    """
    compiled = XsltProcessor().compile(io.BytesIO(stylesheet(body)))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="xsl:initial-template", delivery="raw")
    assert caught.value.code == "XPTY0004"


def test_analyze_string_result_is_grounded_for_copy_of() -> None:
    # analyze-string-097a: elementpath's result element must cross into XDM
    # with its fn namespace as the default, not as a host EtreeElementNode.
    body = """
      <xsl:template name="xsl:initial-template" visibility="public">
        <xsl:copy-of select="analyze-string('ab', '(a)(b)')"/>
      </xsl:template>
    """
    serialized = _serialized(stylesheet(body))
    assert f'<analyze-string-result xmlns="{FN_NS}"' in serialized
    assert '<group nr="1">a</group>' in serialized


def test_static_parse_xml_keeps_all_node_kinds() -> None:
    # static-032: a static parse-xml//node() sees the PI, elements, comment,
    # and text that elementpath's built-in parser drops.
    body = """
      <xsl:variable name="keys" static="true"
        select="parse-xml('&lt;doc>&lt;?Process fun?>&lt;item>&lt;!--comment-->hello&lt;/item>&lt;/doc>')//node()"/>
      <xsl:template name="xsl:initial-template" visibility="public">
        <result count="{count($keys)}"/>
      </xsl:template>
    """
    assert 'count="5"' in _serialized(stylesheet(body))


def test_schema_pool_keeps_the_builtin_when_a_schema_redeclares_it() -> None:
    # catalog-005b/009: XMLSchema.xsd redeclares xs:anyType; the pool's
    # authoritative builtin must win instead of a conflicting duplicate.
    schema = Schema.compile(
        io.BytesIO(
            f'''<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{XSD_NS}">
              <xs:complexType name="anyType" mixed="true"/>
            </xs:schema>'''.encode()
        )
    )
    pool = SchemaPool([schema])
    adapter = pool.get_type(f"{{{XSD_NS}}}anyType")
    assert adapter is not None and adapter.is_builtin


def test_two_versions_of_one_library_override_independently() -> None:
    # override-t-015: the two override copies own separate plans, and each
    # xsl:original resolves the library version it overrides.
    def package(name: str, body: str, version: str) -> str:
        return (
            f'<xsl:package name="{name}" package-version="{version}" version="3.0" '
            f'xmlns:xsl="{XSLT_NS}" xmlns:app="{APP}">{body}</xsl:package>'
        )

    base1 = package(
        BASE, '<xsl:template name="app:t" visibility="public"><ttt/></xsl:template>', "1.0.0"
    )
    base2 = package(
        BASE, '<xsl:template name="app:u" visibility="public"><uuu/></xsl:template>', "2.0.0"
    )
    using = package(
        "urn:example:using",
        f'''
        <xsl:use-package name="{BASE}" package-version="1.0.0"><xsl:override>
          <xsl:template name="app:t" visibility="public"><override><xsl:call-template name="xsl:original"/></override></xsl:template>
        </xsl:override></xsl:use-package>
        <xsl:use-package name="{BASE}" package-version="2.0.0"><xsl:override>
          <xsl:template name="app:u" visibility="public"><override><xsl:call-template name="xsl:original"/></override></xsl:template>
        </xsl:override></xsl:use-package>
        <xsl:template name="app:main" visibility="public">
          <out><xsl:call-template name="app:t"/><xsl:call-template name="app:u"/></out>
        </xsl:template>
        ''',
        "1.0.0",
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(base1, base2)).compile(
        io.BytesIO(using.encode())
    )
    result = compiled.transform(initial_template="app:main", delivery="stylesheet")
    assert result.principal_entry.serialized is not None
    assert "<override><ttt/></override><override><uuu/></override></out>" in (
        result.principal_entry.serialized
    )


def test_deep_stylesheet_recursion_completes_within_budget() -> None:
    # function-0701, higher-order-functions-068, seqtor-027..035: finite deep
    # recursion is bounded by the scheduler budget, not Python's 1000 frames.
    body = """
      <xsl:function name="app:countdown" as="xs:integer">
        <xsl:param name="n" as="xs:integer"/>
        <xsl:sequence select="if ($n = 0) then 0 else app:countdown($n - 1) + 1"/>
      </xsl:function>
      <xsl:template name="xsl:initial-template" visibility="public">
        <out><xsl:value-of select="app:countdown(400)"/></out>
      </xsl:template>
    """
    assert ">400</out>" in _serialized(stylesheet(body, namespaces={"app": APP}))


def test_cache_yes_function_is_memoized() -> None:
    # function-1031: the recursive cached function evaluates once per argument.
    body = """
      <xsl:function name="app:fib" cache="yes" as="xs:integer">
        <xsl:param name="n" as="xs:integer"/>
        <xsl:sequence select="if ($n lt 2) then $n else app:fib($n - 1) + app:fib($n - 2)"/>
      </xsl:function>
      <xsl:template name="xsl:initial-template" visibility="public">
        <out><xsl:value-of select="app:fib(20)"/></out>
      </xsl:template>
    """
    assert ">6765</out>" in _serialized(stylesheet(body, namespaces={"app": APP}))


def test_doc_pattern_matches_the_loaded_document(tmp_path: Path) -> None:
    # match-071: a doc() pattern is a higher-priority match than / for the
    # loaded document, so the template does not recurse through the source.
    source = tmp_path / "loaded.xml"
    source.write_text("<doc><a/></doc>", encoding="utf-8")
    uri = source.resolve().as_uri()
    body = f"""
      <xsl:mode on-no-match="deep-skip"/>
      <xsl:template match="/"><out><xsl:apply-templates select="doc('{uri}')"/></out></xsl:template>
      <xsl:template match="doc('{uri}')"><ok/></xsl:template>
    """
    serialized = _serialized(stylesheet(body), b"<dummy/>")
    assert "<out" in serialized and "<ok/>" in serialized


def test_function_step_result_is_not_sorted_as_a_node() -> None:
    # built-in-templates-0202: //name() returns strings; document-order sorting
    # must apply only when every step result is a node.
    body = """
      <xsl:template match="/"><out><xsl:value-of select="//name()"/></out></xsl:template>
    """
    assert "> doc a</out>" in _serialized(stylesheet(body), b"<doc><a/></doc>")


def test_doc_pattern_uses_the_package_stripped_variant(tmp_path: Path) -> None:
    # match-052: strip-space makes doc() deliver a filtered variant; the
    # pattern's doc() must match that same tree, not the unfiltered registry.
    source = tmp_path / "loaded.xml"
    source.write_text("<doc> <a/> </doc>", encoding="utf-8")
    uri = source.resolve().as_uri()
    body = f"""
      <xsl:strip-space elements="*"/>
      <xsl:template match="/"><out><xsl:apply-templates select="doc('{uri}')"/></out></xsl:template>
      <xsl:template match="doc('{uri}')"><first><xsl:apply-templates/></first></xsl:template>
      <xsl:template match="doc('{uri}')/doc/a"><two/></xsl:template>
    """
    serialized = _serialized(stylesheet(body), b"<dummy/>")
    assert "<first>" in serialized and "<two/>" in serialized


def test_private_component_reference_inside_original_stays_in_its_package() -> None:
    # override-t-007/as-002: the base body's private template/attribute-set
    # reference resolves in the base package even when invoked through
    # xsl:original from the overriding package.
    def package(name: str, body: str, version: str = "1.0.0") -> str:
        return (
            f'<xsl:package name="{name}" package-version="{version}" version="3.0" '
            f'xmlns:xsl="{XSLT_NS}" xmlns:app="{APP}">{body}</xsl:package>'
        )

    base = package(
        BASE,
        """
        <xsl:template name="app:t" visibility="public"><xsl:call-template name="app:private"/></xsl:template>
        <xsl:template name="app:private"><base/></xsl:template>
        <xsl:attribute-set name="app:private-as" visibility="private"><xsl:attribute name="a" select="'base'"/></xsl:attribute-set>
        <xsl:template name="app:uses-private-as" visibility="public"><x xsl:use-attribute-sets="app:private-as"/></xsl:template>
        """,
    )
    using = package(
        "urn:example:using",
        f"""
        <xsl:use-package name="{BASE}"><xsl:override>
          <xsl:template name="app:t" visibility="public"><override><xsl:call-template name="xsl:original"/></override></xsl:template>
        </xsl:override></xsl:use-package>
        <xsl:template name="app:main" visibility="public">
          <out><xsl:call-template name="app:t"/><xsl:call-template name="app:uses-private-as"/></out>
        </xsl:template>
        """,
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(base)).compile(
        io.BytesIO(using.encode())
    )
    result = compiled.transform(initial_template="app:main", delivery="stylesheet")
    serialized = result.principal_entry.serialized
    assert serialized is not None
    assert "<override><base/></override>" in serialized
    assert 'a="base"' in serialized
