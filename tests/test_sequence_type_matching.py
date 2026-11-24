"""Sequence-type matching over XDM type annotations.

``instance of`` cannot rely on the substrate's value-space test for typed nodes
or schema-defined atomic values, and a schema-aware parser has no
``schema-element``/``schema-attribute`` kind test.  These tests pin the
annotation-aware matchers directly, including the branches the W3C corpus
reaches only through its schema-aware cases.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from elementpath.datatypes import builtin_atomic_types

from pyxsd.schema import Schema
from pyxsd.xdm.document import parse_typed_document
from pyxsd.xdm.legacy import ResultTreeFragment
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xdm.values import XdmAtomic
from pyxsd.xpath.engine import (
    _DECLINED,
    _UNTYPED,
    _UNTYPED_ATOMIC,
    _annotate_atomic_value,
    _build_parser,
    _match_annotated_atomic_type,
    _match_tolerant_sequence_type,
    _PoolSchemaProxy,
    _record_annotation,
    _ValueBridge,
    _XdmAdapterRegistry,
)
from pyxsd.xpath.environment import ExpressionEnvironment, XPathStaticContext
from pyxsd.xpath.functions import _root_builtin_local
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.processor import _is_user_derived_subtype
from pyxsd.xslt.runtime.instructions import (
    _item_string,
    _simple_content_string,
    _text_nodes_are_adjacent,
)

XSD_NS = "http://www.w3.org/2001/XMLSchema"
SUP = "urn:example:sequence-types"

_SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:s="{SUP}"
    targetNamespace="{SUP}" elementFormDefault="qualified">
  <xs:simpleType name="myint"><xs:restriction base="xs:int"/></xs:simpleType>
  <xs:simpleType name="mydouble"><xs:restriction base="xs:double"/></xs:simpleType>
  <xs:simpleType name="mydec"><xs:restriction base="xs:decimal"/></xs:simpleType>
  <xs:simpleType name="myuri"><xs:restriction base="xs:anyURI"/></xs:simpleType>
  <xs:simpleType name="Base"><xs:restriction base="xs:string"/></xs:simpleType>
  <xs:simpleType name="Derived"><xs:restriction base="s:Base"/></xs:simpleType>
  <xs:element name="doc" type="s:myint"/>
  <xs:element name="when" type="xs:date"/>
  <xs:attribute name="kind" type="xs:string"/>
  <xs:attribute name="id" type="xs:string"/>
  <xs:element name="rec">
    <xs:complexType><xs:attribute ref="s:id"/></xs:complexType>
  </xs:element>
  <xs:complexType name="C">
    <xs:sequence><xs:element name="b" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:element name="a" type="s:C"/>
</xs:schema>"""


@pytest.fixture(scope="module")
def pool() -> SchemaPool:
    return SchemaPool(
        (Schema.compile(io.StringIO(_SCHEMA), xsd_version="1.0"),),
        xsd_version="1.1",
        merge_namespaces=True,
    )


@pytest.fixture(scope="module")
def parser(pool: SchemaPool) -> object:
    static = XPathStaticContext(namespaces={"s": SUP, "xs": XSD_NS}, schema=pool, xsd_version="1.1")
    return _build_parser("3.1", static, key=False)


def _adapter(pool: SchemaPool, name: str) -> object:
    return pool.get_type(name)


def test_annotation_derivation_covers_ur_types_and_builtin_subtypes(
    pool: SchemaPool,
) -> None:
    proxy = _PoolSchemaProxy(pool, {}, None)
    any_type = _adapter(pool, f"{{{XSD_NS}}}anyType")
    any_simple = _adapter(pool, f"{{{XSD_NS}}}anySimpleType")
    any_atomic = _adapter(pool, f"{{{XSD_NS}}}anyAtomicType")
    token = _adapter(pool, f"{{{XSD_NS}}}token")
    string = _adapter(pool, f"{{{XSD_NS}}}string")

    # An untyped element matches only xs:anyType; an untypedAtomic attribute
    # matches the whole atomic ur-type chain but never xs:string.
    assert proxy._annotation_is_user_type(_UNTYPED, any_type)
    assert not proxy._annotation_is_user_type(_UNTYPED, any_simple)
    assert proxy._annotation_is_user_type(_UNTYPED_ATOMIC, any_atomic)
    assert proxy._annotation_is_user_type(_UNTYPED_ATOMIC, any_simple)
    assert proxy._annotation_is_user_type(_UNTYPED_ATOMIC, any_type)
    assert not proxy._annotation_is_user_type(_UNTYPED_ATOMIC, string)

    # Built-in derivation (NMTOKEN derives from token) is XSD's, not Python's
    # class hierarchy, which has no such inheritance.
    assert proxy._annotation_is_user_type(f"{{{XSD_NS}}}NMTOKEN", token)
    assert not proxy._annotation_is_user_type(f"{{{XSD_NS}}}int", string)
    assert proxy._annotation_is_user_type(f"{{{SUP}}}myint", _adapter(pool, f"{{{XSD_NS}}}int"))
    assert not proxy._annotation_is_user_type("{urn:example:missing}type", string)
    assert proxy._annotation_is_user_type(string.name, string)


def test_annotated_atomic_type_applies_occurrence_and_rejects_untyped(
    parser: object,
) -> None:
    one = builtin_atomic_types["xs:int"](1)
    other = builtin_atomic_types["xs:int"](1)
    two = [one, other]
    parser._pyxsd_bridge_holder.bridge = SimpleNamespace(
        _annotations={
            id(one): (one, f"{{{XSD_NS}}}int"),
            id(other): (other, f"{{{SUP}}}myint"),
        }
    )

    assert _match_annotated_atomic_type([one], "xs:int", parser) is True
    assert _match_annotated_atomic_type(two, "xs:int", parser) is False
    assert _match_annotated_atomic_type([], "xs:int", parser) is False
    assert _match_annotated_atomic_type([], "xs:int?", parser) is True
    assert _match_annotated_atomic_type(two, "xs:int?", parser) is False
    assert _match_annotated_atomic_type([], "xs:int+", parser) is False
    assert _match_annotated_atomic_type([one], "xs:int*", parser) is True
    assert _match_annotated_atomic_type([other], f"{{{SUP}}}myint", parser) is True


def test_annotated_atomic_values_are_distinct_and_bounded() -> None:
    from decimal import Decimal

    from pyxsd.xpath import engine

    first = _annotate_atomic_value("x", f"{{{SUP}}}A")
    second = _annotate_atomic_value("x", f"{{{SUP}}}B")
    assert first is not second
    assert (first._pyxsd_type, second._pyxsd_type) == (f"{{{SUP}}}A", f"{{{SUP}}}B")
    assert _annotate_atomic_value(Decimal("1.5"), f"{{{SUP}}}A")._pyxsd_type == f"{{{SUP}}}A"
    assert _annotate_atomic_value(1.5, f"{{{SUP}}}A")._pyxsd_type == f"{{{SUP}}}A"
    assert _annotate_atomic_value(True, f"{{{SUP}}}A") is True
    assert _annotate_atomic_value(3, f"{{{SUP}}}A") == 3
    derived = builtin_atomic_types["xs:normalizedString"]("x")
    annotated = _annotate_atomic_value(derived, f"{{{SUP}}}A")
    assert type(annotated) is type(derived)
    assert annotated is not derived and annotated._pyxsd_type == f"{{{SUP}}}A"

    annotations: dict[int, tuple[object, str]] = {}
    monkey = engine._ANNOTATION_LIMIT
    engine._ANNOTATION_LIMIT = 1
    try:
        _record_annotation(annotations, True, f"{{{SUP}}}A")
        _record_annotation(annotations, False, f"{{{SUP}}}B")
    finally:
        engine._ANNOTATION_LIMIT = monkey
    assert list(annotations.values()) == [(False, f"{{{SUP}}}B")]


def test_annotated_atomic_type_declines_unrecorded_values(parser: object) -> None:
    unrecorded = builtin_atomic_types["xs:int"](1)

    assert _match_annotated_atomic_type([unrecorded], "xs:int", parser) is _DECLINED


def test_annotated_atomic_type_declines_other_sequence_types(parser: object) -> None:
    assert _match_annotated_atomic_type([1], "{urn:example:missing}type", parser) is None
    assert _match_annotated_atomic_type([1], "missing:type", parser) is None
    assert _match_annotated_atomic_type([1], "element(doc)", parser) is None
    assert _match_annotated_atomic_type([1], "xs:int", SimpleNamespace(schema=None)) is None


def test_registry_annotations_cross_evaluation_boundaries(parser: object) -> None:
    registry = _XdmAdapterRegistry()
    static = XPathStaticContext(
        namespaces={"s": SUP, "xs": XSD_NS}, schema=parser.schema._pool, xsd_version="1.1"
    )
    environment = ExpressionEnvironment(adapter_registry=registry)
    bridge = _ValueBridge(parser, static, environment, version="3.1", registry=registry)
    value = builtin_atomic_types["xs:int"](1)
    registry.annotations[id(value)] = (value, f"{{{SUP}}}myint")

    # A value already adapted in this transformation keeps its schema type
    # both as a variable binding and when it crosses back to XDM.
    assert bridge._variable_value(value) is value
    converted = bridge.to_xdm_sequence(value)
    assert registry.annotations[id(converted[0])][1] == f"{{{SUP}}}myint"

    attached = _annotate_atomic_value(builtin_atomic_types["xs:int"](1), f"{{{SUP}}}myint")
    assert bridge._variable_value(attached) is attached
    attached_xdm = bridge.to_xdm_sequence(attached)
    assert registry.annotations[id(attached_xdm[0])][1] == f"{{{SUP}}}myint"

    document = XdmDocument.parse(f'<s:doc xmlns:s="{SUP}">5</s:doc>'.encode())
    adapter = bridge.adapt_item(document)
    assert _match_tolerant_sequence_type([adapter.children[0]], "element(s:doc)", parser)


def test_untyped_atomic_values_are_not_derived_from_other_atomics(
    pool: SchemaPool,
) -> None:
    from elementpath.datatypes import UntypedAtomic

    proxy = _PoolSchemaProxy(pool, {}, None)
    assert proxy.is_instance(UntypedAtomic("1"), f"{{{XSD_NS}}}anyAtomicType")
    assert not proxy.is_instance(UntypedAtomic("1"), f"{{{XSD_NS}}}int")


def test_tolerant_matcher_applies_names_types_and_occurrence(parser: object) -> None:
    document = XdmDocument.parse(f'<s:doc xmlns:s="{SUP}">5</s:doc>'.encode())
    root = document.root_element

    assert _match_tolerant_sequence_type([document], "document-node()", parser) is True
    assert (
        _match_tolerant_sequence_type([document], "document-node(element(s:doc))", parser) is True
    )
    assert _match_tolerant_sequence_type([document], "document-node(element(*))?", parser) is True
    assert (
        _match_tolerant_sequence_type([document, document], "document-node(element(*))?", parser)
        is False
    )
    assert _match_tolerant_sequence_type([root], "element(s:doc)", parser) is True
    assert _match_tolerant_sequence_type([root], "element(other)", parser) is False
    assert _match_tolerant_sequence_type([root], "element(*, xs:untyped)", parser) is True
    assert _match_tolerant_sequence_type([root], "element(*, xs:anyType)", parser) is True
    assert _match_tolerant_sequence_type([root], "element(*, xs:string)", parser) is False
    assert _match_tolerant_sequence_type([root], "attribute(*)", parser) is False
    assert _match_tolerant_sequence_type([root], "element(*)+", parser) is True
    assert _match_tolerant_sequence_type([], "element(*)+", parser) is False
    assert _match_tolerant_sequence_type([root, root], "element(*)*", parser) is True
    assert _match_tolerant_sequence_type([1], "element(*)", parser) is False
    assert _match_tolerant_sequence_type([1], "xs:int", parser) is None


def test_tolerant_matcher_handles_attribute_namespace_and_declarations(
    pool: SchemaPool, parser: object
) -> None:
    document = XdmDocument.parse(f'<s:doc xmlns:s="{SUP}" kind="x">5</s:doc>'.encode())
    attribute = document.root_element.attributes[0]
    typed = parse_typed_document(
        f'<s:doc xmlns:s="{SUP}">5</s:doc>'.encode(),
        schema_pool=pool,
        validation="strict",
    )

    assert _match_tolerant_sequence_type([attribute], "attribute(kind)", parser) is True
    assert _match_tolerant_sequence_type([attribute], "attribute(other)", parser) is False
    assert (
        _match_tolerant_sequence_type([attribute], "attribute(*, xs:untypedAtomic)", parser) is True
    )
    assert _match_tolerant_sequence_type([attribute], "element(*)", parser) is False
    assert (
        _match_tolerant_sequence_type([typed], "document-node(schema-element(s:doc))", parser)
        is True
    )
    assert (
        _match_tolerant_sequence_type([typed.root_element], "schema-element(s:doc)", parser) is True
    )
    typed_attribute = parse_typed_document(
        f'<s:rec xmlns:s="{SUP}" s:id="x"/>'.encode(),
        schema_pool=pool,
        validation="strict",
    ).root_element.attributes[0]
    assert (
        _match_tolerant_sequence_type([typed_attribute], "schema-attribute(s:id)", parser) is True
    )
    assert _match_tolerant_sequence_type([1], "schema-element(s:doc)", parser) is False
    assert _match_tolerant_sequence_type([document], "namespace-node()", parser) is False
    assert _match_tolerant_sequence_type([typed.root_element], "document-node()", parser) is False


def test_tolerant_matcher_without_a_schema_uses_the_plain_predicates() -> None:
    parser = _build_parser(
        "3.1", XPathStaticContext(namespaces={"s": SUP}, xsd_version="1.1"), key=False
    )
    attribute = XdmDocument.parse(f'<s:doc xmlns:s="{SUP}" kind="x"/>'.encode()).root_element
    attribute = attribute.attributes[0]

    root = XdmDocument.parse(f'<s:doc xmlns:s="{SUP}">x</s:doc>'.encode()).root_element
    assert _match_tolerant_sequence_type([attribute], "attribute(*, xs:untypedAtomic)", parser)
    assert not _match_tolerant_sequence_type([attribute], "attribute(kind, xs:string)", parser)
    assert _match_tolerant_sequence_type([root], "element(*, xs:untyped)", parser)
    assert not _match_tolerant_sequence_type([root], "element(*, xs:string)", parser)


def test_user_derived_subtype_excludes_promotions(
    pool: SchemaPool,
) -> None:
    def item(local: str, lexical: str) -> XdmAtomic:
        adapter = _adapter(pool, f"{{{SUP}}}{local}")
        value = adapter.cast(lexical)
        return XdmAtomic(value, adapter.name, type(value))

    int_item = item("myint", "5")
    double_item = item("mydouble", "5")
    decimal_item = item("mydec", "5")
    uri_item = item("myuri", "urn:x")
    derived_item = item("Derived", "x")

    assert _is_user_derived_subtype(int_item, f"{{{XSD_NS}}}int", pool, "int")
    assert _is_user_derived_subtype(int_item, f"{{{XSD_NS}}}decimal", pool, "decimal")
    assert not _is_user_derived_subtype(decimal_item, f"{{{XSD_NS}}}double", pool, "double")
    assert not _is_user_derived_subtype(double_item, f"{{{XSD_NS}}}float", pool, "float")
    assert not _is_user_derived_subtype(uri_item, f"{{{XSD_NS}}}string", pool, "string")
    assert _is_user_derived_subtype(derived_item, f"{{{SUP}}}Base", pool, "Base")
    assert not _is_user_derived_subtype(int_item, f"{{{SUP}}}Base", pool, "Base")
    assert not _is_user_derived_subtype(
        XdmAtomic(1, "{urn:example:missing}type", int), f"{{{XSD_NS}}}int", pool, "int"
    )
    assert not _is_user_derived_subtype(int_item, "{urn:example:missing}type", pool, "type")

    class _NotAType:
        _type_class = None
        root_type = None

    fake_pool = SimpleNamespace(get_type=lambda _: _NotAType())
    assert not _is_user_derived_subtype(int_item, f"{{{XSD_NS}}}int", fake_pool, "int")


def test_simple_content_uses_string_value_for_complex_typed_elements(
    pool: SchemaPool,
) -> None:
    typed = parse_typed_document(
        f'<a xmlns="{SUP}"><b>x</b></a>'.encode(),
        schema_pool=pool,
        validation="strict",
    )

    assert _item_string(typed.root_element) == "x"
    fragment = ResultTreeFragment(XdmDocument.parse(b"<a>fragment</a>"))
    assert _item_string(fragment) == "fragment"
    with pytest.raises(XsltError, match="function item"):
        _item_string(SimpleNamespace())  # type: ignore[arg-type]


def test_simple_content_merges_parentless_text_and_resets_after_it() -> None:
    first = XdmDocument.parse(b"<a>first</a>").root_element.children[0]
    second = XdmDocument.parse(b"<a>second</b>".replace(b"</b>", b"</a>")).root_element.children[0]
    first._parent = None
    second._parent = None

    assert _simple_content_string((first, second), "|") == "firstsecond"
    assert _simple_content_string((first, XdmAtomic.string("x")), "|") == "first|x"


def test_root_builtin_local_uses_real_datatype_bases() -> None:
    from pyxsd import xsd_data_types as xsd

    assert _root_builtin_local(xsd.AnySimpleType("x")) == "string"
    assert _root_builtin_local(object()) is None


def test_text_nodes_are_adjacent_only_within_one_parent_chain() -> None:
    left = SimpleNamespace(parent=None)
    right = SimpleNamespace(parent=None)
    assert _text_nodes_are_adjacent(left, right)  # type: ignore[arg-type]

    parent = SimpleNamespace(children=[left, SimpleNamespace(parent=None), right])
    left.parent = parent
    right.parent = parent
    assert not _text_nodes_are_adjacent(left, right)  # type: ignore[arg-type]
    adjacent = SimpleNamespace(parent=parent)
    parent.children = [left, adjacent]
    assert _text_nodes_are_adjacent(left, adjacent)  # type: ignore[arg-type]
    assert not _text_nodes_are_adjacent(left, SimpleNamespace(parent=None))  # type: ignore[arg-type]


def test_equal_string_values_keep_distinct_type_annotations(tmp_path: Path) -> None:
    """Two user string types with the same payload do not overwrite each other."""
    from pyxsd.xslt import XsltProcessor

    (tmp_path / "types.xsd").write_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="{SUP}"
               targetNamespace="{SUP}">
             <xs:simpleType name="A"><xs:restriction base="xs:string"/></xs:simpleType>
             <xs:simpleType name="B"><xs:restriction base="xs:string"/></xs:simpleType>
           </xs:schema>""",
        encoding="utf-8",
    )
    (tmp_path / "types.xsl").write_text(
        f"""<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
               xmlns:xs="{XSD_NS}" xmlns:t="{SUP}" version="3.0">
             <xsl:import-schema namespace="{SUP}" schema-location="types.xsd"/>
             <xsl:template name="main">
               <xsl:param name="t:a" as="t:A"/>
               <xsl:param name="t:b" as="t:B"/>
               <out>
                 <xsl:value-of select="$t:a instance of t:A"/>
                 <xsl:value-of select="$t:a instance of t:B"/>
                 <xsl:value-of select="$t:b instance of t:A"/>
                 <xsl:value-of select="$t:b instance of t:B"/>
               </out>
             </xsl:template>
           </xsl:stylesheet>""",
        encoding="utf-8",
    )
    compiled = XsltProcessor(language="3.0", xpath="3.1", schema_aware=True).compile(
        tmp_path / "types.xsl"
    )
    result = compiled.transform(
        initial_template="main",
        template_parameters={
            "t:a": XdmAtomic("x", f"{{{SUP}}}A", str),
            "t:b": XdmAtomic("x", f"{{{SUP}}}B", str),
        },
    )
    assert result.principal_entry.serialized.endswith(">truefalsefalsetrue</out>")


def test_date_time_argument_enforces_cardinality_and_node_atomization(
    pool: SchemaPool,
) -> None:
    from pyxsd.xslt.runtime.functions import _date_time_argument

    with pytest.raises(XsltError) as caught:
        _date_time_argument(((XdmAtomic.string("a"), XdmAtomic.string("b")),), "date")
    assert caught.value.code == "XPTY0004"

    typed = parse_typed_document(
        f'<a xmlns="{SUP}"><b>x</b></a>'.encode(), schema_pool=pool, validation="strict"
    )
    with pytest.raises(XsltError) as caught:
        _date_time_argument(((typed.root_element,),), "date")
    assert caught.value.code == "XPTY0004"

    simple = parse_typed_document(
        f'<when xmlns="{SUP}">2003-12-07</when>'.encode(),
        schema_pool=pool,
        validation="strict",
    )
    argument = _date_time_argument(((simple.root_element,),), "date")
    assert str(argument) == "2003-12-07"

    with pytest.raises(XsltError) as caught:
        _date_time_argument((("not-an-atomic",),), "date")
    assert caught.value.code == "XPTY0004"


def test_temporal_sort_key_errors_and_implicit_timezone() -> None:
    from datetime import UTC

    from pyxsd import xsd_data_types as xsd
    from pyxsd.xslt.runtime.iteration import _compare_datetime

    invalid = SimpleNamespace(lexical="not-a-date", type_name=f"{{{XSD_NS}}}date")
    with pytest.raises(XsltError) as caught:
        _compare_datetime(invalid, invalid, None)
    assert caught.value.code == "XTDE1030"

    early = XdmAtomic(xsd.DateTime("2000-01-01T00:00:00"), f"{{{XSD_NS}}}dateTime", xsd.DateTime)
    late = XdmAtomic(xsd.DateTime("2000-01-02T00:00:00"), f"{{{XSD_NS}}}dateTime", xsd.DateTime)
    assert _compare_datetime(early, late, None) < 0
    assert _compare_datetime(early, late, UTC) < 0
    assert _compare_datetime(late, early, UTC) > 0
