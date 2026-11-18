"""Schema-aware kind tests: ``element``/``attribute`` and ``schema-*``.

XPath 3.1 sections 2.5.5.3-2.5.5.6 define the four tests as predicates over a
node's type annotation and its governing schema declaration, never over its
typed value.  ``schema-element``/``schema-attribute`` additionally name a
declaration, so a name absent from the in-scope schema components is a static
``XPST0008``.

The corpus cases (``nodetest``, ``import-schema``, ``strip-type-annotations``)
are the end-to-end regression; these tests pin each clause of the shared
predicates and the three call sites that use them: expression kind tests,
patterns, and declared sequence types.
"""

from __future__ import annotations

import io
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema
from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.document import parse_typed_document
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xdm.schema import PyxsdElement, SchemaPool
from pyxsd.xdm.type_tests import (
    ANY_TYPE,
    UNTYPED,
    UNTYPED_ATOMIC,
    builtin_derives,
    declaration_exists,
    derives_from,
    element_declaration_names,
    matches_attribute_test,
    matches_element_test,
    matches_schema_attribute,
    matches_schema_element,
)
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
SUP = "urn:example:kind"
STRICT = ParseModes.LAX.replace(namespaces="strict")

SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{SUP}" xmlns:s="{SUP}"
    elementFormDefault="qualified" attributeFormDefault="qualified">
  <xs:simpleType name="PartNumber">
    <xs:restriction base="xs:string">
      <xs:pattern value="[0-9]{{3}}-[A-Z]{{2}}"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="Small">
    <xs:restriction base="xs:int"><xs:maxInclusive value="10"/></xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="Base"><xs:restriction base="xs:string"/></xs:simpleType>
  <xs:simpleType name="Derived">
    <xs:restriction base="s:Base"><xs:maxLength value="3"/></xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="PartNumberOrInt">
    <xs:union memberTypes="s:PartNumber xs:integer"/>
  </xs:simpleType>
  <xs:simpleType name="TokensOrPart">
    <xs:union memberTypes="xs:NMTOKENS s:PartNumber"/>
  </xs:simpleType>
  <xs:simpleType name="InnerUnion">
    <xs:union memberTypes="xs:NMTOKENS xs:integer"/>
  </xs:simpleType>
  <xs:simpleType name="OuterUnion">
    <xs:union memberTypes="s:InnerUnion s:PartNumber"/>
  </xs:simpleType>
  <xs:simpleType name="RestrictedUnion">
    <xs:restriction base="s:PartNumberOrInt"><xs:pattern value=".*"/></xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="MixedRestricted">
    <xs:union memberTypes="s:RestrictedUnion s:PartNumber"/>
  </xs:simpleType>
  <xs:complexType name="Doc">
    <xs:sequence>
      <xs:any namespace="##any" processContents="skip" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="code" type="s:Derived"/>
  </xs:complexType>
  <xs:complexType name="Holder">
    <xs:sequence><xs:element name="base" type="xs:string"/></xs:sequence>
    <xs:attribute name="code" type="s:Derived"/>
  </xs:complexType>
  <xs:complexType name="Wrap">
    <xs:sequence>
      <xs:any namespace="##any" processContents="skip" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="AnonHolder">
    <xs:attribute ref="s:anon"/>
  </xs:complexType>
  <xs:element name="doc" type="s:PartNumber"/>
  <xs:element name="small" type="s:Small"/>
  <xs:element name="holder" type="s:Holder"/>
  <xs:element name="wrap" type="s:Wrap"/>
  <xs:element name="anonHolder" type="s:AnonHolder"/>
  <xs:element name="base" type="s:Base"/>
  <xs:element name="derived" type="s:Derived"/>
  <xs:element name="integer" type="xs:integer"/>
  <xs:element name="long" type="xs:long"/>
  <xs:element name="flag" type="xs:boolean"/>
  <xs:element name="id" type="xs:ID"/>
  <xs:element name="tokens" type="xs:NMTOKENS"/>
  <xs:element name="book" type="s:Base"/>
  <xs:element name="audioBook" type="s:Base" substitutionGroup="s:book"/>
  <xs:element name="paperBook" type="s:Derived" substitutionGroup="s:audioBook"/>
  <xs:element name="abstractBook" type="s:Base" abstract="true" substitutionGroup="s:book"/>
  <xs:element name="blockedBook" type="s:Base" block="substitution"
      substitutionGroup="s:book"/>
  <xs:element name="audioMaybe" type="s:Base" nillable="true" substitutionGroup="s:book"/>
  <xs:element name="maybe" type="xs:string" nillable="true"/>
  <xs:element name="plainMaybe" type="xs:string"/>
  <xs:element name="anonymous">
    <xs:simpleType><xs:restriction base="xs:string"/></xs:simpleType>
  </xs:element>
  <xs:attribute name="code" type="s:Base"/>
  <xs:attribute name="plain" type="xs:string"/>
  <xs:attribute name="anon">
    <xs:simpleType><xs:restriction base="xs:string"/></xs:simpleType>
  </xs:attribute>
</xs:schema>"""


def xs(local: str) -> str:
    return f"{{{XSD_NS}}}{local}"


def sup(local: str) -> str:
    return f"{{{SUP}}}{local}"


@pytest.fixture(scope="module")
def pool() -> SchemaPool:
    compiled = Schema.compile(io.StringIO(SCHEMA), mode=STRICT)
    assert not compiled.report.has_errors, compiled.report
    return SchemaPool.from_schema(compiled)


def _typed(xml: str, pool: SchemaPool, validation: str = "strict") -> XdmDocument:
    return parse_typed_document(xml.encode(), schema_pool=pool, validation=validation)


def _stylesheet(body: str, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:s="{SUP}" version="3.0"{attributes}>'
        f'<xsl:template name="main">{body}</xsl:template></xsl:stylesheet>'
    ).encode()


def _compile(body: str, pool: SchemaPool | None, attributes: str = ""):
    return XsltProcessor().compile(io.BytesIO(_stylesheet(body, attributes)), schema=pool)


def _run(
    body: str,
    pool: SchemaPool | None,
    *,
    source: bytes = b"<v/>",
    validation: str | None = None,
    attributes: str = "",
) -> tuple[str, ...]:
    compiled = _compile(body, pool, attributes)
    options = {} if validation is None else {"validation": validation}
    result = compiled.transform(io.BytesIO(source), initial_template="main", **options)
    return tuple(
        item.lexical if isinstance(item, XdmAtomic) else str(item) for item in result.principal
    )


def _annotation(type_name: str | None, actual_type: object = None) -> SimpleNamespace:
    return SimpleNamespace(type_name=type_name, actual_type=actual_type)


# ---------------------------------------------------------------------------
# The shared derivation table


def test_builtin_derivation_follows_xsd() -> None:
    assert builtin_derives(None, xs("string")) is False
    assert builtin_derives(xs("integer"), xs("integer")) is True
    # xs:integer is a restriction of xs:decimal, and xs:long of xs:integer.
    assert builtin_derives(xs("integer"), xs("decimal")) is True
    assert builtin_derives(xs("long"), xs("integer")) is True
    assert builtin_derives(xs("integer"), xs("long")) is False
    assert builtin_derives(xs("boolean"), xs("integer")) is False
    assert builtin_derives(xs("string"), xs("integer")) is False
    assert builtin_derives(xs("untyped"), xs("anyType")) is True
    assert builtin_derives(xs("untyped"), xs("anySimpleType")) is False
    assert builtin_derives(xs("untypedAtomic"), xs("anyAtomicType")) is True
    assert builtin_derives(xs("anyType"), xs("anySimpleType")) is False
    # Type identity is by expanded name; the local-name fallback is the
    # pattern compiler's historical behaviour.
    assert builtin_derives("{urn:other}x", xs("x")) is True
    assert builtin_derives(xs("integer"), "{urn:other}decimal") is False


def test_untyped_annotations_follow_xdm_2_7_2(pool: SchemaPool) -> None:
    untyped = _annotation(UNTYPED)
    assert derives_from(untyped, UNTYPED, None)
    assert derives_from(untyped, ANY_TYPE, None)
    assert not derives_from(untyped, xs("anySimpleType"), None)
    assert not derives_from(untyped, sup("Base"), pool)

    untyped_atomic = _annotation(UNTYPED_ATOMIC)
    for declared in (UNTYPED_ATOMIC, xs("anyAtomicType"), xs("anySimpleType"), ANY_TYPE):
        assert derives_from(untyped_atomic, declared, None)
    assert not derives_from(untyped_atomic, xs("string"), None)
    assert not derives_from(untyped_atomic, sup("Base"), pool)


def test_named_annotations_resolve_through_the_pool(pool: SchemaPool) -> None:
    assert derives_from(_annotation(sup("Derived")), sup("Base"), pool)
    assert not derives_from(_annotation(sup("Base")), sup("Derived"), pool)
    assert derives_from(_annotation(xs("integer")), xs("decimal"), None)
    assert not derives_from(_annotation("{urn:missing}Type"), xs("string"), None)
    assert not derives_from(_annotation(None), xs("string"), None)


def test_class_annotations_use_xsd_derivation(pool: SchemaPool) -> None:
    derived = pool.get_type(sup("Derived"))
    assert derived is not None
    derived_class = derived._type_class
    assert derives_from(_annotation(None, derived_class), sup("Base"), pool)
    assert not derives_from(_annotation(None, derived_class), "{urn:missing}Type", pool)
    assert derives_from(_annotation(None, derived_class), ANY_TYPE, pool)
    assert derives_from(_annotation(None, derived_class), xs("anySimpleType"), pool)
    assert derives_from(_annotation(None, derived_class), xs("anyAtomicType"), pool)

    holder = pool.get_type(sup("Holder"))
    assert holder is not None
    assert not derives_from(_annotation(None, holder._type_class), xs("anySimpleType"), pool)
    assert not derives_from(_annotation(None, holder._type_class), xs("anyAtomicType"), pool)

    # Without a pool the ur-type clauses fall back to the compiled classes.
    assert derives_from(_annotation(None, derived_class), xs("anySimpleType"), None)
    assert derives_from(_annotation(None, derived_class), xs("anyAtomicType"), None)


# ---------------------------------------------------------------------------
# element(N, T) and attribute(N, T)


def test_element_test_predicate_clauses(pool: SchemaPool) -> None:
    document = _typed(f'<s:doc xmlns:s="{SUP}">123-AB</s:doc>', pool)
    node = document.root_element
    assert matches_element_test(node, sup("doc"), None, False, pool)
    assert not matches_element_test(node, sup("other"), None, False, pool)
    assert matches_element_test(node, None, None, False, pool)
    assert matches_element_test(node, None, sup("PartNumber"), False, pool)
    assert not matches_element_test(node, None, sup("Base"), False, pool)

    holder = _typed(f'<s:holder xmlns:s="{SUP}"><s:base>x</s:base></s:holder>', pool)
    assert not matches_element_test(holder, None, None, False, pool)


def test_element_test_nilled_rule(pool: SchemaPool) -> None:
    document = _typed(
        f'<s:maybe xmlns:s="{SUP}" xmlns:xsi="{XSI_NS}" xsi:nil="true"/>',
        pool,
    )
    node = document.root_element
    assert node.nilled is True
    # The nilled rule applies only to a typed test, and only without ``?``.
    assert matches_element_test(node, None, None, False, pool)
    assert not matches_element_test(node, None, xs("string"), False, pool)
    assert matches_element_test(node, None, xs("string"), True, pool)


def test_the_union_clause_requires_a_pure_union(pool: SchemaPool) -> None:
    document = _typed(f'<s:doc xmlns:s="{SUP}">123-AB</s:doc>', pool)
    member = document.root_element
    # A member of a pure union matches the union type.
    assert matches_element_test(member, None, sup("PartNumberOrInt"), False, pool)
    # A union with a list member is not pure, so the member clause is off.
    assert not matches_element_test(member, None, sup("TokensOrPart"), False, pool)
    # Neither is a union restricted from another union, nor a union that
    # contains one as a member.
    assert not matches_element_test(member, None, sup("RestrictedUnion"), False, pool)
    assert not matches_element_test(member, None, sup("MixedRestricted"), False, pool)
    # A list in the transitive membership also makes the union impure.
    integer = _typed(f'<s:integer xmlns:s="{SUP}">5</s:integer>', pool).root_element
    assert not matches_element_test(integer, None, sup("OuterUnion"), False, pool)
    assert matches_element_test(integer, None, sup("PartNumberOrInt"), False, pool)


def test_attribute_test_predicate_clauses(pool: SchemaPool) -> None:
    document = _typed(f'<s:holder xmlns:s="{SUP}" s:code="ab"><s:base>x</s:base></s:holder>', pool)
    node = document.root_element
    code = node.attribute(sup("code"))
    assert code is not None
    assert matches_attribute_test(code, sup("code"), None, pool)
    assert not matches_attribute_test(code, sup("plain"), None, pool)
    assert matches_attribute_test(code, None, sup("Base"), pool)
    assert matches_attribute_test(code, None, sup("Derived"), pool)
    assert not matches_attribute_test(node, None, None, pool)

    untyped = XdmDocument.parse(f'<v xmlns:s="{SUP}" s:code="5"/>'.encode()).root_element
    untyped_code = untyped.attribute(sup("code"))
    assert untyped_code is not None
    assert not matches_attribute_test(untyped_code, None, xs("string"), pool)
    assert matches_attribute_test(untyped_code, None, xs("untypedAtomic"), None)


# ---------------------------------------------------------------------------
# schema-element and schema-attribute


def test_substitution_group_is_transitive_and_filters_the_actual_group(pool: SchemaPool) -> None:
    assert element_declaration_names(pool, sup("book")) == (
        sup("audioBook"),
        sup("audioMaybe"),
        sup("book"),
        sup("paperBook"),
    )
    # A name with no substitution group covers the absent-edge arm.
    assert element_declaration_names(pool, sup("doc")) == (sup("doc"),)

    direct = _typed(f'<s:audioBook xmlns:s="{SUP}">abc</s:audioBook>', pool).root_element
    transitive = _typed(f'<s:paperBook xmlns:s="{SUP}">abc</s:paperBook>', pool).root_element
    assert matches_schema_element(direct, sup("book"), pool)
    assert matches_schema_element(transitive, sup("book"), pool)
    assert not matches_schema_element(direct, sup("paperBook"), pool)

    abstract = XdmDocument.parse(f'<s:abstractBook xmlns:s="{SUP}"/>'.encode()).root_element
    blocked = XdmDocument.parse(f'<s:blockedBook xmlns:s="{SUP}"/>'.encode()).root_element
    assert not matches_schema_element(abstract, sup("book"), pool)
    assert not matches_schema_element(blocked, sup("book"), pool)


def test_schema_element_requires_the_declared_type(pool: SchemaPool) -> None:
    document = _typed(
        f'<s:holder xmlns:s="{SUP}"><s:base>x</s:base></s:holder>',
        pool,
    )
    local = next(iter(document.root_element.children))
    assert local.name == sup("base")
    assert local.type_name == xs("string")
    # The local declaration shadows the global one, whose type is s:Base.
    assert not matches_schema_element(local, sup("base"), pool)
    assert not matches_element_test(local, None, sup("Base"), False, pool)

    global_base = _typed(f'<s:base xmlns:s="{SUP}">abc</s:base>', pool).root_element
    assert matches_schema_element(global_base, sup("base"), pool)
    assert not matches_schema_element(global_base, sup("missing"), pool)
    assert not matches_schema_element(global_base, sup("book"), None)


def test_schema_element_nilled_rule_uses_the_candidate_declaration(pool: SchemaPool) -> None:
    document = _typed(
        f'<s:audioMaybe xmlns:s="{SUP}" xmlns:xsi="{XSI_NS}" xsi:nil="true"/>',
        pool,
    )
    node = document.root_element
    assert node.nilled is True
    # A nilled substitute may match the head when its own declaration is
    # nillable (XPath 3.1 section 2.5.5.4 names the candidate's declaration).
    assert matches_schema_element(node, sup("book"), pool)
    assert matches_schema_element(node, sup("audioMaybe"), pool)

    maybe = _typed(f'<s:maybe xmlns:s="{SUP}" xmlns:xsi="{XSI_NS}" xsi:nil="true"/>', pool)
    assert matches_schema_element(maybe.root_element, sup("maybe"), pool)

    # The predicate fails closed for a nilled node whose declaration is not
    # nillable, even when the type clause would pass.
    plain = _typed(f'<s:plainMaybe xmlns:s="{SUP}">x</s:plainMaybe>', pool)
    forged = replace(plain.root_element.annotation, nilled=True)
    plain.root_element._apply_occurrence_annotation(forged)
    assert not matches_schema_element(plain.root_element, sup("plainMaybe"), pool)


def test_anonymous_declared_types_match_without_a_type_name(pool: SchemaPool) -> None:
    element = _typed(f'<s:anonymous xmlns:s="{SUP}">x</s:anonymous>', pool).root_element
    assert matches_schema_element(element, sup("anonymous"), pool)

    holder = _typed(f'<s:anonHolder xmlns:s="{SUP}" s:anon="x"/>', pool)
    attribute = holder.root_element.attribute(sup("anon"))
    assert attribute is not None
    assert attribute.annotation.actual_type is not None
    assert matches_schema_attribute(attribute, sup("anon"), pool)


def test_schema_attribute_predicate_clauses(pool: SchemaPool) -> None:
    document = _typed(f'<s:holder xmlns:s="{SUP}" s:code="ab"><s:base>x</s:base></s:holder>', pool)
    code = document.root_element.attribute(sup("code"))
    assert code is not None
    assert matches_schema_attribute(code, sup("code"), pool)
    assert not matches_schema_attribute(code, sup("plain"), pool)
    assert not matches_schema_attribute(code, sup("missing"), pool)
    assert not matches_schema_attribute(document.root_element, sup("code"), pool)
    assert not matches_schema_attribute(code, sup("code"), None)

    untyped = XdmDocument.parse(f'<v xmlns:s="{SUP}" s:code="5"/>'.encode()).root_element
    untyped_code = untyped.attribute(sup("code"))
    assert untyped_code is not None
    assert not matches_schema_attribute(untyped_code, sup("code"), pool)


def test_declaration_exists(pool: SchemaPool) -> None:
    assert not declaration_exists(None, "schema-element", sup("book"))
    assert declaration_exists(pool, "schema-element", sup("book"))
    assert not declaration_exists(pool, "schema-element", sup("missing"))
    assert declaration_exists(pool, "schema-attribute", sup("code"))
    assert not declaration_exists(pool, "schema-attribute", sup("missing"))


def test_element_declaration_metadata_is_guarded(pool: SchemaPool) -> None:
    abstract = pool.get_element(sup("abstractBook"))
    blocked = pool.get_element(sup("blockedBook"))
    maybe = pool.get_element(sup("maybe"))
    assert abstract is not None and abstract.abstract is True
    assert blocked is not None and blocked.block == "substitution"
    assert maybe is not None and maybe.nillable is True
    assert pool.get_element(sup("book")) is not None
    assert pool.get_element(sup("book")).nillable is False

    bare = object.__new__(PyxsdElement)
    bare._er = object()
    assert bare.nillable is False
    assert bare.abstract is False
    assert bare.block is None


# ---------------------------------------------------------------------------
# Expression kind tests end to end


def test_schema_element_is_a_filter_not_an_error(pool: SchemaPool) -> None:
    values = _run(
        '<xsl:sequence select="count(schema-element(s:small))"/>'
        '<xsl:sequence select="count(schema-element(s:doc))"/>',
        pool,
        source=f'<s:doc xmlns:s="{SUP}">123-AB</s:doc>'.encode(),
        validation="strict",
    )
    assert values == ("0", "1")


def test_schema_element_matches_substitution_group_members(pool: SchemaPool) -> None:
    for member in ("audioBook", "paperBook"):
        values = _run(
            '<xsl:sequence select="boolean(schema-element(s:book))"/>',
            pool,
            source=f'<s:{member} xmlns:s="{SUP}">abc</s:{member}>'.encode(),
            validation="strict",
        )
        assert values == ("true",), member


def test_schema_element_requires_the_declared_type_end_to_end(pool: SchemaPool) -> None:
    values = _run(
        '<xsl:sequence select="count(schema-element(s:base))"/>'
        '<xsl:sequence select="count(element(*, s:Base))"/>',
        pool,
        source=f'<s:holder xmlns:s="{SUP}"><s:base>x</s:base></s:holder>'.encode(),
        validation="strict",
    )
    assert values == ("0", "0")


def test_stripped_nodes_fail_closed(pool: SchemaPool) -> None:
    element_values = _run(
        '<xsl:sequence select="count(schema-element(s:doc))"/>',
        pool,
        source=f'<s:doc xmlns:s="{SUP}">123-AB</s:doc>'.encode(),
        validation="strip",
    )
    assert element_values == ("0",)

    body = (
        '<xsl:for-each select="/s:holder">'
        '<xsl:sequence select="count(schema-attribute(s:code))"/>'
        "</xsl:for-each>"
    )
    source = f'<s:holder xmlns:s="{SUP}" s:code="ab"><s:base>x</s:base></s:holder>'.encode()
    assert _run(body, pool, source=source, validation="strip") == ("0",)
    assert _run(body, pool, source=source, validation="strict") == ("1",)


def test_schema_attribute_uses_the_default_and_explicit_axes(pool: SchemaPool) -> None:
    body = (
        '<xsl:for-each select="/s:holder">'
        '<xsl:sequence select="count(schema-attribute(s:code))"/>'
        '<xsl:sequence select="count(attribute::schema-attribute(s:code))"/>'
        '<xsl:sequence select="count(self::schema-attribute(s:code))"/>'
        '<xsl:sequence select="count(attribute::attribute(s:code))"/>'
        "</xsl:for-each>"
    )
    values = _run(
        body,
        pool,
        source=f'<s:holder xmlns:s="{SUP}" s:code="ab"><s:base>x</s:base></s:holder>'.encode(),
        validation="strict",
    )
    assert values == ("1", "1", "0", "1")


def test_a_missing_schema_declaration_is_a_static_error(pool: SchemaPool) -> None:
    for kind, name in (("schema-element", "s:missing"), ("schema-attribute", "s:missing")):
        with pytest.raises(XsltError) as caught:
            _compile(f'<xsl:value-of select="{kind}({name})"/>', pool)
        assert caught.value.code == "XPST0008"

    # No in-scope schema components: an empty component set is a static error.
    with pytest.raises(XsltError) as caught:
        _compile('<xsl:value-of select="schema-element(s:book)"/>', None)
    assert caught.value.code == "XPST0008"


def test_document_node_child_and_default_namespaces(pool: SchemaPool) -> None:
    values = _run(
        '<xsl:sequence select="(/) instance of document-node(schema-element(s:holder))"/>'
        '<xsl:sequence select="/s:holder instance of element(holder)"/>',
        pool,
        source=f'<s:holder xmlns:s="{SUP}" s:code="x"><s:base>abc</s:base></s:holder>'.encode(),
        validation="strict",
        attributes=f' xpath-default-namespace="{SUP}"',
    )
    assert values == ("true", "true")

    # An unprefixed attribute name is in no namespace even when the default
    # element/type namespace is set (XPath 3.1 section 2.5.5.5).
    attribute = _run(
        '<xsl:sequence select="count(/*/@*[self::attribute(code)])"/>',
        pool,
        source=b'<v code="x"/>',
        attributes=f' xpath-default-namespace="{SUP}"',
    )
    assert attribute == ("1",)


def test_element_kind_test_without_arguments(pool: SchemaPool) -> None:
    # Text and comment children are not selected by ``element()``.
    values = _run(
        '<xsl:for-each select="/s:wrap"><xsl:sequence select="count(child::element())"/>'
        "</xsl:for-each>",
        pool,
        source=f'<s:wrap xmlns:s="{SUP}">text<a/><!--c--><b/></s:wrap>'.encode(),
        validation="strip",
    )
    assert values == ("2",)


def test_kind_test_tokens_require_a_context(pool: SchemaPool) -> None:
    from elementpath.exceptions import MissingContextError

    from pyxsd.xpath.engine import _build_parser
    from pyxsd.xpath.environment import XPathStaticContext

    static = XPathStaticContext(namespaces={"s": SUP}, schema=pool)
    parser = _build_parser("3.1", static, functions=None, key=False)
    for source in (
        "element()",
        "attribute()",
        "schema-element(s:book)",
        "schema-attribute(s:code)",
    ):
        token = parser.parse(source)
        with pytest.raises(MissingContextError):
            list(token.select(None))


# ---------------------------------------------------------------------------
# element(*, T) / attribute(*, T) annotation rules


def test_an_untyped_element_matches_untyped_and_any_type_only(pool: SchemaPool) -> None:
    body = (
        '<xsl:variable name="e"><v>123-AB</v></xsl:variable>'
        '<xsl:sequence select="$e/v instance of element(*, s:PartNumber)"/>'
        '<xsl:sequence select="$e/v instance of element(*, xs:untyped)"/>'
        '<xsl:sequence select="$e/v instance of element(*, xs:anyType)"/>'
        '<xsl:sequence select="$e/v instance of element(*, xs:anySimpleType)"/>'
        '<xsl:sequence select="$e/v instance of element(*, xs:anyAtomicType)"/>'
        '<xsl:sequence select="$e/v instance of element(*, xs:string)"/>'
    )
    assert _run(body, pool) == ("false", "true", "true", "false", "false", "false")


def test_an_untyped_attribute_matches_the_untyped_atomic_chain(pool: SchemaPool) -> None:
    body = (
        '<xsl:for-each select="/v/@a">'
        '<xsl:sequence select=". instance of attribute(*, xs:string)"/>'
        '<xsl:sequence select=". instance of attribute(*, s:Base)"/>'
        '<xsl:sequence select=". instance of attribute(*, xs:untypedAtomic)"/>'
        '<xsl:sequence select=". instance of attribute(*, xs:anyAtomicType)"/>'
        '<xsl:sequence select=". instance of attribute(*, xs:anySimpleType)"/>'
        '<xsl:sequence select=". instance of attribute(*, xs:anyType)"/>'
        "</xsl:for-each>"
    )
    assert _run(body, pool, source=b'<v a="5"/>') == (
        "false",
        "false",
        "true",
        "true",
        "true",
        "true",
    )


def test_a_derived_type_matches_its_base_in_both_directions(pool: SchemaPool) -> None:
    values = _run(
        '<xsl:sequence select="/s:derived instance of element(*, s:Base)"/>'
        '<xsl:sequence select="/s:derived instance of element(*, s:Derived)"/>',
        pool,
        source=f'<s:derived xmlns:s="{SUP}">abc</s:derived>'.encode(),
        validation="strict",
    )
    assert values == ("true", "true")

    reverse = _run(
        '<xsl:sequence select="/s:base instance of element(*, s:Derived)"/>',
        pool,
        source=f'<s:base xmlns:s="{SUP}">abc</s:base>'.encode(),
        validation="strict",
    )
    assert reverse == ("false",)


def test_builtin_derivations_are_xsd_derivations(pool: SchemaPool) -> None:
    def probe(name: str, expression: str) -> str:
        values = _run(
            f'<xsl:sequence select="/s:{name} instance of {expression}"/>',
            pool,
            source=f'<s:{name} xmlns:s="{SUP}">{_LEXICAL[name]}</s:{name}>'.encode(),
            validation="strict",
        )
        return values[0]

    assert probe("long", "element(*, xs:integer)") == "true"
    assert probe("long", "element(*, xs:decimal)") == "true"
    assert probe("long", "element(*, xs:long)") == "true"
    assert probe("integer", "element(*, xs:long)") == "false"
    assert probe("flag", "element(*, xs:integer)") == "false"
    assert probe("id", "element(*, xs:anyAtomicType)") == "true"
    assert probe("id", "element(*, xs:anySimpleType)") == "true"
    # A list type must not raise XPST0051 from the old value-space test.
    assert probe("tokens", "element(*, xs:NMTOKENS)") == "true"
    assert probe("tokens", "element(*, xs:anyAtomicType)") == "false"


_LEXICAL = {
    "long": "5",
    "integer": "5",
    "flag": "true",
    "id": "A1",
    "tokens": "a b",
}


def test_any_type_is_accepted_by_a_declared_sequence_type(pool: SchemaPool) -> None:
    body = (
        '<xsl:variable name="e" as="element(*, xs:anyType)"><v/></xsl:variable>'
        '<xsl:variable name="u" as="element(*, xs:untyped)"><v/></xsl:variable>'
        '<xsl:sequence select="count($e)"/>'
        '<xsl:sequence select="count($u)"/>'
    )
    assert _run(body, pool) == ("1", "1")


def test_a_declared_sequence_type_fails_a_named_type_for_an_untyped_node(
    pool: SchemaPool,
) -> None:
    body = (
        '<xsl:variable name="e" as="element(*, s:PartNumber)"><v>123-AB</v></xsl:variable>'
        '<xsl:sequence select="count($e)"/>'
    )
    compiled = _compile(body, pool)
    with pytest.raises(XsltError) as caught:
        compiled.transform(io.BytesIO(b"<v/>"), initial_template="main")
    assert caught.value.code == "XTTE0570"

    for atomic in (
        '<xsl:variable name="x" as="element()" select="1"/>',
        '<xsl:variable name="x" as="attribute()" select="1"/>',
    ):
        with pytest.raises(XsltError) as caught:
            _compile(atomic, pool).transform(io.BytesIO(b"<v/>"), initial_template="main")
        assert caught.value.code == "XTTE0570"


def test_declared_schema_kind_sequence_types(pool: SchemaPool) -> None:
    element = _run(
        '<xsl:variable name="b" as="schema-element(s:book)" select="/s:audioBook"/>'
        '<xsl:sequence select="count($b)"/>',
        pool,
        source=f'<s:audioBook xmlns:s="{SUP}">abc</s:audioBook>'.encode(),
        validation="strict",
    )
    assert element == ("1",)

    attribute = _run(
        '<xsl:variable name="a" as="schema-attribute(s:code)" select="/s:holder/@s:code"/>'
        '<xsl:sequence select="count($a)"/>',
        pool,
        source=f'<s:holder xmlns:s="{SUP}" s:code="ab"><s:base>x</s:base></s:holder>'.encode(),
        validation="strict",
    )
    assert attribute == ("1",)


def test_a_declaration_absent_from_the_pool_does_not_match(pool: SchemaPool) -> None:
    element = XdmDocument.parse(b"<doc/>").root_element
    assert not matches_schema_element(element, sup("missing"), pool)
