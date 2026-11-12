"""XSLT 3.0 typed-mode pattern behavior.

Pins the semantics of ``xsl:mode/@typed`` (section 6.6.3): boolean
synonyms, the dynamic ``XTTE3100``/``XTTE3110`` transition checks, the
implicit ``schema-element`` interpretation of first pattern steps in
strict and lax modes, and the static ``XTSE3105`` error.  These tests are
corpus-independent and exercise public compilation/transformation
behavior plus the pattern compilation API.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

import pytest

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema
from pyxsd.xdm.document import parse_typed_document
from pyxsd.xdm.nodes import NodeKind, XdmDocument
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xdm.validation import ValidationRequest, validate_occurrence
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.context import StaticContext, StaticVariable
from pyxsd.xslt.compiler.packages import PackageResolver
from pyxsd.xslt.compiler.patterns import PatternMatcher, compile_pattern
from pyxsd.xslt.errors import XsltError, XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
DOC = "urn:example:doc"

SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{DOC}"
    xmlns:doc="{DOC}" elementFormDefault="qualified">
  <xs:complexType name="RootType">
    <xs:sequence>
      <xs:element name="amount" type="xs:integer"/>
      <xs:element name="name" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="doc:RootType"/>
  <xs:element name="amount" type="xs:integer"/>
  <xs:element name="name" type="xs:string"/>
  <xs:element name="base" type="xs:string"/>
  <xs:element name="derived" type="xs:string" substitutionGroup="doc:base"/>
  <xs:attribute name="code" type="xs:integer"/>
</xs:schema>"""

INT_SOURCE = f'<doc:amount xmlns:doc="{DOC}">5</doc:amount>'.encode()
NAME_SOURCE = f'<doc:name xmlns:doc="{DOC}">x</doc:name>'.encode()
DERIVED_SOURCE = f'<doc:derived xmlns:doc="{DOC}">v</doc:derived>'.encode()
NESTED_SOURCE = (
    f'<doc:root xmlns:doc="{DOC}"><doc:amount>5</doc:amount><doc:name>x</doc:name></doc:root>'
).encode()
OTHER_SOURCE = f'<doc:other xmlns:doc="{DOC}">v</doc:other>'.encode()

UNTYPED_INTEGER = f"{{{XSD_NS}}}integer"
UNTYPED_ATOMIC = f"{{{XSD_NS}}}untypedAtomic"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schema_pool() -> SchemaPool:
    schema = Schema.compile(
        io.StringIO(SCHEMA),
        mode=ParseModes.LAX.replace(namespaces="strict"),
    )
    assert not schema.report.has_errors, schema.report
    return SchemaPool.from_schema(schema)


def _stylesheet(body: str, *, import_schema: bool = True) -> bytes:
    schema = f"<xsl:import-schema>{SCHEMA}</xsl:import-schema>" if import_schema else ""
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:doc="{DOC}" version="3.0">{schema}{body}</xsl:stylesheet>'
    ).encode()


def _compile(body: str, *, import_schema: bool = True, **options: Any):
    return XsltProcessor(**options).compile(
        io.BytesIO(_stylesheet(body, import_schema=import_schema))
    )


def _transform(
    body: str,
    *,
    source: Any = INT_SOURCE,
    validation: str = "strict",
    import_schema: bool = True,
    **options: Any,
):
    compiled = _compile(body, import_schema=import_schema)
    return compiled.transform(
        source,
        initial_template="main",
        delivery="document",
        validation=validation,
        **options,
    )


def _error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def _element_names(result: Any) -> tuple[str, ...]:
    root = result.principal_tree.root_element
    return tuple(node.local_name for node in root.descendants if node.kind is NodeKind.ELEMENT)


def _pattern_context(
    schema: SchemaPool | None, *, variables: tuple[str, ...] = ()
) -> StaticContext:
    context = StaticContext(
        namespaces={"doc": DOC, "xs": XSD_NS},
        schema=schema,
        version="3.0",
        uri="urn:example:module",
    )
    for name in variables:
        context = context.with_variable(StaticVariable(name=name))
    return context


def _annotated(source: bytes, *, type_name: str | None = None) -> XdmDocument:
    """Build a typed node with no governing element declaration.

    The empty schema pool still provides every built-in type; validating
    against a requested type annotates the node without supplying the
    global element declaration that ``schema-element`` requires.
    """
    if type_name is None:
        return parse_typed_document(source, schema_pool=_schema_pool(), validation="strict")
    document = XdmDocument.parse(source)
    validate_occurrence(
        document.root_element,
        ValidationRequest(type_name=type_name),
        schema_pool=SchemaPool(),
    )
    assert document.root_element.annotation.governing_declaration is None
    return document


def _matcher(pattern: str, *, typed: str, schema: SchemaPool | None = None) -> PatternMatcher:
    return PatternMatcher(compile_pattern(pattern, _pattern_context(schema), typed=typed))


def _matches(matcher: PatternMatcher, item: Any) -> bool:
    return matcher.matches(item, ExpressionEnvironment())


# ---------------------------------------------------------------------------
# element(*, T) matching under every typed policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["yes", "no", "strict", "lax", "unspecified"])
def test_element_type_test_matches_only_annotated_integer_nodes(typed: str) -> None:
    pool = _schema_pool()
    context = _pattern_context(pool)
    typed_integer = _annotated(INT_SOURCE, type_name=UNTYPED_INTEGER).root_element
    typed_string = _annotated(NAME_SOURCE, type_name=f"{{{XSD_NS}}}string").root_element
    untyped = XdmDocument.parse(INT_SOURCE).root_element
    untyped_atomic = _annotated(INT_SOURCE, type_name=UNTYPED_ATOMIC).root_element

    compiled = compile_pattern("element(*, xs:integer)", context, typed=typed)
    assert compiled.default_priorities == (Decimal("0"),)
    matcher = PatternMatcher(compiled)

    assert _matches(matcher, typed_integer)
    assert not _matches(matcher, typed_string)
    assert not _matches(matcher, untyped)
    assert not _matches(matcher, untyped_atomic)


# ---------------------------------------------------------------------------
# Strict and lax first-step NameTest reinterpretation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["strict", "lax"])
def test_name_test_matches_a_schema_validated_element(typed: str) -> None:
    pool = _schema_pool()
    measured = _annotated(INT_SOURCE).root_element
    matcher = _matcher("doc:amount", typed=typed, schema=pool)
    assert _matches(matcher, measured)
    assert not _matches(matcher, XdmDocument.parse(INT_SOURCE).root_element)
    assert _matches(_matcher("doc:amount[1]", typed=typed, schema=pool), measured)


@pytest.mark.parametrize("typed", ["strict", "lax"])
def test_name_test_matches_substitution_group_members(typed: str) -> None:
    pool = _schema_pool()
    matcher = _matcher("doc:base", typed=typed, schema=pool)
    assert _matches(matcher, _annotated(DERIVED_SOURCE).root_element)


def test_lax_reinterprets_only_names_with_a_global_declaration() -> None:
    pool = _schema_pool()
    context = _pattern_context(pool)

    # The name is declared: schema-element(doc:amount) requires the node to
    # have been validated against that declaration.
    declared = compile_pattern("doc:amount", context, typed="lax")
    assert not _matches(
        PatternMatcher(declared),
        _annotated(INT_SOURCE, type_name=UNTYPED_INTEGER).root_element,
    )

    # The name is not declared: the original name test is retained, so a
    # node annotated with that name still matches.
    undeclared = compile_pattern("doc:other", context, typed="lax")
    assert _matches(
        PatternMatcher(undeclared),
        _annotated(OTHER_SOURCE, type_name=f"{{{XSD_NS}}}string").root_element,
    )


def test_lax_without_a_schema_keeps_plain_name_tests() -> None:
    context = _pattern_context(None)
    matcher = PatternMatcher(compile_pattern("item", context, typed="lax"))
    assert _matches(matcher, XdmDocument.parse(b"<item/>").root_element)


def test_strict_requires_a_global_element_declaration() -> None:
    pool = _schema_pool()
    context = _pattern_context(pool, variables=("v",))
    for pattern in (
        "doc:missing",
        "/doc:missing",
        "//doc:missing",
        "self::doc:missing",
        "doc:missing/doc:amount",
        "doc:amount intersect doc:missing",
        "doc:root/(doc:missing)",
        "$v/doc:missing",
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_pattern(pattern, context, typed="strict")
        assert caught.value.code == "XTSE3105", pattern


def test_strict_does_not_restrict_exempt_pattern_forms() -> None:
    pool = _schema_pool()
    context = _pattern_context(pool, variables=("v",))
    for pattern in (
        "*",
        "*:missing",
        "doc:*",
        "@doc:missing",
        "node()",
        "element(*)",
        "element(*, xs:integer)",
        "doc:amount/doc:missing",
        "doc:root//doc:missing",
        "doc:root/(doc:amount)",
        "/",
        "$v",
        ".",
        ".[. instance of xs:integer]",
    ):
        compile_pattern(pattern, context, typed="strict")


def test_strict_without_a_schema_rejects_every_name_test() -> None:
    context = _pattern_context(None)
    with pytest.raises(XsltStaticError) as caught:
        compile_pattern("item", context, typed="strict")
    assert caught.value.code == "XTSE3105"

    # Wildcards and attribute tests do not require a declaration.
    compile_pattern("*", context, typed="strict")
    compile_pattern("@item", context, typed="strict")


def test_name_test_priority_remains_syntax_based() -> None:
    pool = _schema_pool()
    context = _pattern_context(pool)
    assert compile_pattern("doc:amount", context, typed="strict").default_priorities == (
        Decimal("0"),
    )
    assert compile_pattern("doc:base", context, typed="strict").default_priorities == (
        Decimal("0"),
    )
    assert compile_pattern("doc:amount", context, typed="lax").default_priorities == (Decimal("0"),)
    assert compile_pattern(
        "element(*, xs:integer)", context, typed="strict"
    ).default_priorities == (Decimal("0"),)
    assert compile_pattern("doc:amount/doc:name", context, typed="strict").default_priorities == (
        Decimal("0.5"),
    )


def test_compile_pattern_validates_the_typed_policy() -> None:
    context = _pattern_context(_schema_pool())
    with pytest.raises(ValueError, match="typed"):
        compile_pattern("doc:amount", context, typed="sometimes")
    with pytest.raises(TypeError, match="typed"):
        compile_pattern("doc:amount", context, typed=1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Transformation behavior in strict and lax modes
# ---------------------------------------------------------------------------


def test_strict_mode_matches_a_substitution_group_member() -> None:
    body = (
        '<xsl:mode name="s" typed="strict"/>'
        '<xsl:template name="main"><out><xsl:apply-templates select="/*" mode="s"/>'
        "</out></xsl:template>"
        '<xsl:template match="doc:base" mode="s"><base/></xsl:template>'
    )
    assert _element_names(_transform(body, source=DERIVED_SOURCE)) == ("base",)


@pytest.mark.parametrize("typed", ["strict", "lax"])
def test_declared_name_does_not_match_an_unvalidated_typed_element(typed: str) -> None:
    body = (
        f'<xsl:mode name="s" typed="{typed}"/>'
        '<xsl:template name="main"><out><xsl:apply-templates select="/*" mode="s"/>'
        "</out></xsl:template>"
        '<xsl:template match="doc:amount" mode="s"><int/></xsl:template>'
    )
    document = _annotated(INT_SOURCE, type_name=UNTYPED_INTEGER)
    result = _compile(body).transform(
        document,
        initial_template="main",
        delivery="document",
        validation="preserve",
    )
    assert _element_names(result) == ()


def test_lax_mode_matches_an_undeclared_name() -> None:
    body = (
        '<xsl:mode name="s" typed="lax"/>'
        '<xsl:template name="main"><out><xsl:apply-templates select="/*" mode="s"/>'
        "</out></xsl:template>"
        '<xsl:template match="doc:other" mode="s"><other/></xsl:template>'
    )
    document = _annotated(OTHER_SOURCE, type_name=f"{{{XSD_NS}}}string")
    result = _compile(body).transform(
        document,
        initial_template="main",
        delivery="document",
        validation="preserve",
    )
    assert _element_names(result) == ("other",)


def test_strict_mode_does_not_reinterpret_later_steps() -> None:
    body = (
        '<xsl:mode name="s" typed="strict"/>'
        '<xsl:template name="main"><out><xsl:apply-templates select="/*" mode="s"/>'
        "</out></xsl:template>"
        '<xsl:template match="doc:root/doc:amount" mode="s"><nested/></xsl:template>'
    )
    assert _element_names(_transform(body, source=NESTED_SOURCE)) == ("nested",)


@pytest.mark.parametrize("typed", ["strict", "lax"])
def test_element_test_in_a_typed_mode_uses_type_annotations(typed: str) -> None:
    body = (
        f'<xsl:mode name="s" typed="{typed}"/>'
        '<xsl:template name="main"><out><xsl:apply-templates select="/*" mode="s"/>'
        "</out></xsl:template>"
        '<xsl:template match="element(*, xs:integer)" mode="s"><int/></xsl:template>'
    )
    compiled = _compile(body)

    integer = compiled.transform(
        INT_SOURCE, initial_template="main", validation="strict", delivery="document"
    )
    assert _element_names(integer) == ("int",)

    string = compiled.transform(
        NAME_SOURCE, initial_template="main", validation="strict", delivery="document"
    )
    assert _element_names(string) == ()

    assert (
        _error_code(
            lambda: compiled.transform(
                b"<in/>", initial_template="main", validation="strip", delivery="raw"
            )
        )
        == "XTTE3100"
    )


@pytest.mark.parametrize("typed", ["strict", "lax"])
def test_untyped_atomic_rejected_by_a_typed_mode(typed: str) -> None:
    body = (
        f'<xsl:mode name="s" typed="{typed}"/>'
        '<xsl:template name="main">'
        '<xsl:variable name="ua" as="element()"><xsl:element name="doc:u" '
        'type="xs:untypedAtomic">x</xsl:element></xsl:variable>'
        '<xsl:apply-templates select="$ua" mode="s"/></xsl:template>'
        '<xsl:template match="node()" mode="s"><out/></xsl:template>'
    )
    assert _error_code(lambda: _transform(body)) == "XTTE3100"


# ---------------------------------------------------------------------------
# Dynamic transition checks: initial and explicit apply-templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["yes", "true", "1", "strict", "lax"])
def test_initial_mode_rejects_untyped_input(typed: str) -> None:
    body = (
        f'<xsl:mode name="m" typed="{typed}"/>'
        '<xsl:template match="*" mode="m"><out/></xsl:template>'
    )
    assert (
        _error_code(lambda: _compile(body).transform(b"<in/>", initial_mode="m", delivery="raw"))
        == "XTTE3100"
    )


@pytest.mark.parametrize("typed", ["yes", "true", "1", "strict", "lax"])
def test_initial_mode_accepts_typed_input(typed: str) -> None:
    body = (
        f'<xsl:mode name="m" typed="{typed}"/>'
        '<xsl:template match="*" mode="m"><out/></xsl:template>'
    )
    result = _compile(body).transform(
        INT_SOURCE, initial_mode="m", validation="strict", delivery="document"
    )
    assert result.principal_tree.root_element.local_name == "out"


@pytest.mark.parametrize("typed", ["yes", "true", "1", "strict", "lax"])
def test_explicit_apply_templates_rejects_untyped_input(typed: str) -> None:
    body = (
        f'<xsl:mode name="m" typed="{typed}"/>'
        '<xsl:template name="main"><xsl:apply-templates select="/*" mode="m"/>'
        "</xsl:template>"
        '<xsl:template match="*" mode="m"><out/></xsl:template>'
    )
    assert _error_code(lambda: _transform(body, source=b"<in/>", validation="strip")) == (
        "XTTE3100"
    )


@pytest.mark.parametrize("typed", ["yes", "true", "1", "strict", "lax"])
def test_explicit_apply_templates_accepts_typed_input(typed: str) -> None:
    body = (
        f'<xsl:mode name="m" typed="{typed}"/>'
        '<xsl:template name="main"><out><xsl:apply-templates select="/*" mode="m"/>'
        "</out></xsl:template>"
        '<xsl:template match="*" mode="m"><matched/></xsl:template>'
    )
    assert _element_names(_transform(body)) == ("matched",)


@pytest.mark.parametrize("typed", ["no", "false", "0"])
def test_untyped_mode_rejects_typed_input(typed: str) -> None:
    body = (
        f'<xsl:mode name="m" typed="{typed}"/>'
        '<xsl:template name="main"><xsl:apply-templates select="/*" mode="m"/>'
        "</xsl:template>"
        '<xsl:template match="*" mode="m"><out/></xsl:template>'
    )
    assert _error_code(lambda: _transform(body)) == "XTTE3110"


@pytest.mark.parametrize("typed", ["no", "false", "0"])
def test_untyped_mode_accepts_untyped_input(typed: str) -> None:
    body = (
        f'<xsl:mode name="m" typed="{typed}"/>'
        '<xsl:template name="main"><out><xsl:apply-templates select="/*" mode="m"/>'
        "</out></xsl:template>"
        '<xsl:template match="*" mode="m"><matched/></xsl:template>'
    )
    assert _element_names(_transform(body, source=b"<in/>", validation="strip")) == ("matched",)


@pytest.mark.parametrize("typed", ["no", "false", "0"])
def test_untyped_mode_accepts_untyped_atomic(typed: str) -> None:
    body = (
        f'<xsl:mode name="m" typed="{typed}"/>'
        '<xsl:template name="main"><out>'
        '<xsl:variable name="ua" as="element()"><xsl:element name="u" '
        'type="xs:untypedAtomic">x</xsl:element></xsl:variable>'
        '<xsl:apply-templates select="$ua" mode="m"/></out></xsl:template>'
        '<xsl:template match="*" mode="m"><matched/></xsl:template>'
    )
    assert _element_names(_transform(body, import_schema=False, validation="strip")) == ("matched",)


def test_unspecified_mode_accepts_typed_and_untyped_input() -> None:
    for declaration in (
        "",
        '<xsl:mode name="m" typed="unspecified"/>',
        '<xsl:mode name="m" on-multiple-match="use-last"/>',
    ):
        body = (
            f"{declaration}"
            '<xsl:template name="main"><out><xsl:apply-templates select="/*" mode="m"/>'
            "</out></xsl:template>"
            '<xsl:template match="*" mode="m"><matched/></xsl:template>'
        )
        compiled = _compile(body)
        untyped = compiled.transform(
            b"<in/>", initial_template="main", validation="strip", delivery="document"
        )
        assert _element_names(untyped) == ("matched",)
        typed = compiled.transform(
            INT_SOURCE, initial_template="main", validation="strict", delivery="document"
        )
        assert _element_names(typed) == ("matched",)


def test_invalid_typed_value_is_a_static_error() -> None:
    body = '<xsl:mode name="m" typed="sometimes"/><xsl:template name="main"/>'
    assert _error_code(lambda: _compile(body)) == "XTSE0020"


# ---------------------------------------------------------------------------
# Package-local schema context for XTSE3105
# ---------------------------------------------------------------------------


def _package(body: str, *, name: str = "urn:lib") -> bytes:
    return (
        f'<xsl:package name="{name}" package-version="1.0" version="3.0" '
        f'xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" xmlns:doc="{DOC}" '
        f'xmlns:lib="{name}">{body}</xsl:package>'
    ).encode()


def _principal(body: str) -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:doc="{DOC}" xmlns:lib="urn:lib" version="3.0">'
        f"{body}</xsl:stylesheet>"
    ).encode()


def test_library_strict_rule_uses_the_library_schema_context() -> None:
    library = _package(
        f"<xsl:import-schema>{SCHEMA}</xsl:import-schema>"
        '<xsl:mode name="lib:m" typed="strict" visibility="public"/>'
        '<xsl:template match="doc:amount" mode="lib:m"><int/></xsl:template>'
    )
    principal = _principal(
        '<xsl:use-package name="urn:lib">'
        '<xsl:accept component="mode" names="lib:m" visibility="public"/>'
        "</xsl:use-package>"
        '<xsl:template name="main"><xsl:apply-templates select="/*" mode="lib:m"/>'
        "</xsl:template>"
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(
        io.BytesIO(principal)
    )
    dependency = compiled.package.dependencies[0]
    mode = dependency.mode("{urn:lib}m")
    assert mode is not None
    assert mode.typed == "strict"


def test_principal_strict_rule_does_not_borrow_the_library_schema() -> None:
    library = _package(
        f"<xsl:import-schema>{SCHEMA}</xsl:import-schema>"
        '<xsl:mode name="lib:m" typed="strict" visibility="public"/>'
    )
    principal = _principal(
        '<xsl:use-package name="urn:lib"/>'
        '<xsl:mode name="s" typed="strict"/>'
        '<xsl:template match="doc:amount" mode="s"><int/></xsl:template>'
    )
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(io.BytesIO(principal))
    assert caught.value.code == "XTSE3105"
