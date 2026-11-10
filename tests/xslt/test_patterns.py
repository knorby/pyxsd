"""Normative XSLT 3.0 Pattern30 compilation and matching.

Patterns are their own grammar, not XPath expressions or sequence types.  These
tests pin the grammar boundaries, the ``root(.)//(EE)`` semantics (including
parentless-node adjustment and positional sibling focus), rooted functions,
variables, schema tests, default priorities, and candidate-aware caching.
"""

from __future__ import annotations

import io
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from pyxsd.binding import ParseModes
from pyxsd.xdm.annotations import capture_schema_annotations
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xdm.values import XdmAtomic, XdmMap
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xslt.compiler.context import StaticContext, StaticVariable
from pyxsd.xslt.compiler.patterns import (
    AxisStep,
    CandidateFilter,
    CandidateShape,
    CompiledPattern,
    NameTest,
    PatternAlternative,
    PatternMatcher,
    PatternStatistics,
    RelativePath,
    _axis_nodes,
    _builtin_class,
    _builtin_derives,
    _declaration_key,
    _effective_boolean_value,
    _error_code,
    _is_descendant_or_self,
    _local_name,
    _namespace_of,
    _numeric_equals_position,
    compile_pattern,
    default_priority,
)
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.runtime.templates import PatternIndex, TemplateRule

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
DOC_NS = "urn:example:doc"
OTHER_NS = "urn:example:other"
STRICT = ParseModes.LAX.replace(namespaces="strict")


def _context(
    *,
    default_namespace: str | None = None,
    schema: SchemaPool | None = None,
    variables: tuple[str, ...] = (),
) -> StaticContext:
    context = StaticContext(
        namespaces={"doc": DOC_NS, "other": OTHER_NS},
        xpath_default_namespace=default_namespace,
        schema=schema,
        version="3.0",
        uri="urn:example:module",
    )
    for name in variables:
        context = context.with_variable(StaticVariable(name=name))
    return context


def _matches(
    pattern: str,
    item: object,
    *,
    context: StaticContext | None = None,
    environment: ExpressionEnvironment | None = None,
) -> bool:
    compiled = compile_pattern(pattern, context or _context())
    return PatternMatcher(compiled).matches(
        item,  # type: ignore[arg-type]
        environment or ExpressionEnvironment(),
    )


def _document(xml: str) -> XdmDocument:
    return XdmDocument.parse(xml.encode("utf-8"))


def _element_children(node: XdmNode) -> tuple[XdmNode, ...]:
    return tuple(child for child in node.children if child.kind is NodeKind.ELEMENT)


def _standalone_element(xml: str) -> XdmNode:
    return _document(xml).root_element


def _parentless_element(name: str) -> XdmNode:
    document = _document("<placeholder/>")
    return XdmNode(
        NodeKind.ELEMENT,
        document=document,
        node_id=100,
        source_name=document.source_name,
        source_index=document.source_index,
        name=name,
    )


def _parentless_attribute(name: str, value: str = "x") -> XdmNode:
    document = _document("<placeholder/>")
    return XdmNode(
        NodeKind.ATTRIBUTE,
        document=document,
        node_id=101,
        source_name=document.source_name,
        source_index=document.source_index,
        name=name,
        value=value,
    )


def _parentless_namespace(prefix: str = "p", uri: str = "urn:p") -> XdmNode:
    document = _document("<placeholder/>")
    return XdmNode(
        NodeKind.NAMESPACE,
        document=document,
        node_id=102,
        source_name=document.source_name,
        source_index=document.source_index,
        name=prefix,
        value=uri,
        prefix=prefix,
    )


def _parentless_node(
    kind: NodeKind, *, name: str | None = None, value: str = "", target: str | None = None
) -> XdmNode:
    document = _document("<placeholder/>")
    return XdmNode(
        kind,
        document=document,
        node_id=103,
        source_name=document.source_name,
        source_index=document.source_index,
        name=name,
        value=value,
        target=target,
    )


def _code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


def _assert_code(exc: BaseException, code: str) -> None:
    assert _code(exc) == code


def _schema_pool(tmp_path: Path) -> tuple[Any, SchemaPool]:
    del tmp_path
    source = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{DOC_NS}"
        xmlns:t="{DOC_NS}" elementFormDefault="qualified">
      <xs:simpleType name="Payload">
        <xs:restriction base="xs:string"/>
      </xs:simpleType>
      <xs:complexType name="Holder">
        <xs:simpleContent>
          <xs:extension base="xs:string">
            <xs:attribute ref="t:code"/>
          </xs:extension>
        </xs:simpleContent>
      </xs:complexType>
      <xs:attribute name="code" type="xs:ID"/>
      <xs:element name="base" type="t:Holder"/>
      <xs:element name="derived" type="t:Holder" substitutionGroup="t:base"/>
      <xs:element name="item" type="t:Holder"/>
      <xs:element name="keyed" type="xs:ID"/>
      <xs:element name="payload" type="t:Payload" nillable="true"/>
    </xs:schema>"""
    from pyxsd.schema import Schema

    schema = Schema.compile(io.StringIO(source), mode=STRICT)
    assert not schema.report.has_errors, schema.report
    return schema, SchemaPool.from_schema(schema)


def _annotated(compiled: Any, schema: SchemaPool, xml: str) -> XdmDocument:
    capture = capture_schema_annotations(compiled, io.BytesIO(xml.encode("utf-8")))
    document = XdmDocument.parse(xml.encode("utf-8"), schema_pool=schema)
    capture.apply(document)
    return document


# ---------------------------------------------------------------------------
# Normative examples and grammar


def test_pattern_semantics_and_sibling_focus() -> None:
    document = _document("<a><b><c/><d/></b></a>")
    d_node = _element_children(_element_children(document.root_element)[0])[1]

    first_document = _document("<root><item/><item/></root>")
    first_item, second_item = _element_children(first_document.root_element)
    parentless_item = _parentless_element("item")

    assert _matches(".[. instance of map(*)]", XdmMap.empty())
    assert _matches("a//b/(c | d)", d_node)
    assert _matches("item[1]", first_item)
    assert not _matches("item[1]", second_item)
    assert _matches("item[1]", parentless_item)
    with pytest.raises(XsltStaticError, match="XTSE0340"):
        compile_pattern("map(*)", _context())


def test_pattern_grammar_boundaries() -> None:
    context = _context()
    invalid = [
        "",
        "map(*)",
        "item()",
        "empty-sequence()",
        "function(*)",
        "array(*)",
        "a/",
        "a//",
        "a b",
        "|a",
        "a|.",
        "root(.)",
        "root(1)",
        "doc('x', 'y')",
        "id()",
        "unknown:doc('x')",
        "a[1",
        "a[]",
        "(a",
    ]
    for pattern in invalid:
        with pytest.raises(XsltStaticError) as caught:
            compile_pattern(pattern, context)
        code = _code(caught.value)
        assert code in {"XTSE0340", "XPST0003", "XPST0017", "XPST0081"}, (pattern, code)


def test_pattern_value_pattern_union_and_types() -> None:
    context = _context(variables=("v",))
    assert _matches(".", XdmAtomic.integer(1))
    assert _matches(". [. instance of xs:integer]", XdmAtomic.integer(1))
    assert not _matches(".[. instance of xs:integer]", XdmAtomic.string("1"))
    compiled = compile_pattern("($v)", context)
    assert compiled.source == "($v)"
    assert isinstance(compiled, CompiledPattern)


# ---------------------------------------------------------------------------
# Axes, node tests, and names


def test_axes_and_node_tests() -> None:
    document = _document("<r>lead<a><b/><b/></a><c/><!--note--><?pi data?><text>t</text></r>")
    root = document.root_element
    text, a, c, comment, pi, text_element = root.children
    second_b = _element_children(a)[1]

    assert _matches("b", second_b)
    assert _matches("a/b", second_b)
    assert _matches("child::b", second_b)
    assert _matches("descendant::b", second_b)
    assert _matches("descendant-or-self::b", second_b)
    assert _matches("self::b", second_b)
    assert _matches("r//b", second_b)
    assert _matches("a//b", second_b)
    assert not _matches("a/b", a)  # an a element is not a b
    assert _matches("node()", text)
    assert not _matches("node()", document)
    assert _matches("text()", text)
    assert not _matches("text()", text_element)
    assert not _matches("text()", c)
    assert _matches("comment()", comment)
    assert _matches("processing-instruction()", pi)
    assert _matches("processing-instruction('pi')", pi)
    assert _matches("processing-instruction(pi)", pi)
    assert not _matches("processing-instruction('other')", pi)
    assert _matches("document-node()", document)
    assert _matches("document-node(element(r))", document)
    assert not _matches("document-node(element(other))", document)
    assert _matches("element()", c)
    assert _matches("element(r)", root)
    assert _matches("element(*)", root)
    assert not _matches("attribute()", c)


def test_attribute_axis_and_attributes_are_not_children() -> None:
    document = _document('<r code="1"><a code="2"/></r>')
    root = document.root_element
    root_code = root.attribute("code")
    child_code = _element_children(root)[0].attribute("code")
    assert root_code is not None and child_code is not None

    assert _matches("@code", root_code)
    assert _matches("attribute::code", child_code)
    assert _matches("@attribute()", root_code)
    assert not _matches("attribute()", root_code)
    assert not _matches("attribute(*, xs:string)", root_code)
    assert not _matches("node()", root_code)
    assert not _matches("self::code", root_code)
    assert _matches("self::attribute(code)", root_code)

    parentless = _parentless_attribute("code")
    assert _matches("@code", parentless)
    assert _matches("@attribute()", parentless)
    assert not _matches("node()", parentless)
    assert not _matches("child::code", parentless)


def test_name_tests_default_namespace_and_wildcards() -> None:
    document = _document(f'<root xmlns="{DOC_NS}"><item code="1"/></root>')
    item = _element_children(document.root_element)[0]

    namespaced = _context(default_namespace=DOC_NS)
    assert _matches("item", item, context=namespaced)
    assert _matches("doc:item", item, context=namespaced)
    assert _matches("*:item", item, context=namespaced)
    assert _matches("doc:*", item, context=namespaced)
    assert _matches("Q{urn:example:doc}item", item, context=namespaced)
    assert _matches("Q{urn:example:doc}*", item, context=namespaced)
    assert _matches("*", item, context=namespaced)

    plain = _context()
    assert _matches("item", item, context=plain) is False
    assert _matches("other:item", item, context=plain) is False
    with pytest.raises(XsltStaticError) as caught:
        compile_pattern("missing:item", plain)
    assert _code(caught.value) == "XPST0081"

    code = item.attribute("code")
    assert code is not None
    # An unprefixed attribute name never uses the XPath default namespace.
    assert _matches("@code", code, context=namespaced)
    with pytest.raises(XsltStaticError) as caught:
        compile_pattern("@missing:code", namespaced)
    assert _code(caught.value) == "XPST0081"


def test_parentless_adjustment_and_absolute_paths() -> None:
    parentless = _parentless_element("item")
    assert parentless.parent is None
    assert _matches("item", parentless)
    assert _matches("self::item", parentless)
    assert _matches("descendant-or-self::item", parentless)
    assert not _matches("//item", parentless)
    assert not _matches("descendant::item", parentless)
    assert not _matches("/item", parentless)

    document = _document("<item/>")
    assert _matches("/", document)
    assert _matches("document-node()", document)
    assert not _matches("node()", document)
    assert _matches("/item", document.root_element)
    assert not _matches("/", document.root_element)

    namespaced = _document(f'<r xmlns="{DOC_NS}"><a><b/></a></r>')
    assert _matches(
        "//doc:b",
        _element_children(_element_children(namespaced.root_element)[0])[0],
        context=_context(default_namespace=DOC_NS),
    )


def test_namespace_nodes_and_namespace_axis() -> None:
    document = _document("<r xmlns:p='urn:p' p:x='1'/>")
    root = document.root_element
    namespace = root.namespace_node("p")
    assert namespace is not None
    assert _matches("namespace::*", namespace)
    assert _matches("namespace::p", namespace)
    assert not _matches("namespace::other", namespace)
    assert not _matches("self::p", namespace)


# ---------------------------------------------------------------------------
# Predicates and focus


def test_predicates_use_correct_sibling_focus() -> None:
    document = _document('<r><a><item code="1"/><item/><item code="3"/></a><item/></r>')
    a, outer_item = _element_children(document.root_element)
    first, second, third = _element_children(a)
    assert _matches("a/item[1]", first)
    assert not _matches("a/item[1]", second)
    assert _matches("a/item[2]", second)
    assert _matches("a/item[last()]", third)
    assert _matches("a/item[position() = 2]", second)
    assert _matches("a/item[@code]", first)
    assert not _matches("a/item[@code]", second)
    assert _matches("a/item[@code = '3']", third)
    assert _matches("a/item[1][@code]", first)
    assert not _matches("a/item[1][@code]", second)
    assert _matches("item[1]", outer_item)

    parentless = _parentless_element("item")
    assert _matches("item[1]", parentless)
    assert _matches("item[last()]", parentless)


def test_predicate_dynamic_errors_are_non_matches() -> None:
    document = _document("<r><item/><item/></r>")
    first, second = _element_children(document.root_element)
    compiled = compile_pattern("item[1 div 0]", _context())
    matcher = PatternMatcher(compiled)
    environment = ExpressionEnvironment()
    assert matcher.matches(first, environment) is False
    assert matcher.matches(second, environment) is False
    assert matcher.statistics.errors_ignored >= 1


def test_current_is_the_candidate_item() -> None:
    document = _document("<r><a><item/></a><b><item/></b></r>")
    a, b = _element_children(document.root_element)
    item_under_a = _element_children(a)[0]
    item_under_b = _element_children(b)[0]

    assert _matches(".[current() instance of node()]", item_under_a)
    assert not _matches(".[current() instance of xs:integer]", item_under_a)
    assert _matches("a/item[current()/self::item]", item_under_a)
    assert not _matches("a/item[current()/self::b]", item_under_a)
    assert _matches("item[current()/self::item]", item_under_b)
    assert not _matches("item[current()/self::item]", XdmAtomic.integer(1))


# ---------------------------------------------------------------------------
# Union, intersect, and except


def test_union_intersect_except() -> None:
    document = _document("<r><a><b/><c/></a><b/><c/><d/></r>")
    root = document.root_element
    a, outer_b, outer_c, outer_d = _element_children(root)
    inner_b, inner_c = _element_children(a)

    assert _matches("a | b", a)
    assert _matches("a union b", outer_b)
    assert _matches("a | b | c", outer_c)
    assert _matches("* except a", outer_b)
    assert not _matches("* except a", a)
    assert not _matches("a intersect b", a)
    assert _matches("a/b intersect a/*", inner_b)
    assert _matches("(a | d)/*", inner_c)
    assert _matches("(a | d)", outer_d)
    assert not _matches("(a | d)/*", outer_d)
    assert not _matches("a except a", a)
    assert _matches("a | a", a)
    assert not _matches("b | c", outer_d)


# ---------------------------------------------------------------------------
# Rooted paths, functions, and variables


def test_root_and_rooted_functions(tmp_path: Path) -> None:
    parentless = _parentless_element("a")
    assert _matches("root()", parentless)
    assert _matches("root()", parentless.document)
    assert _matches("root()/self::a", parentless)
    nested = _element_children(_document("<a><b/></a>").root_element)[0]
    assert not _matches("root()/self::a", nested)

    document = _document("<r><item/><item/></r>")
    item = _element_children(document.root_element)[0]
    assert not _matches("root()/self::r", document.root_element)
    assert not _matches("root()/self::item", item)

    other_document = _document("<catalog><product/><product/></catalog>")
    product = _element_children(other_document.root_element)[1]
    environment = ExpressionEnvironment(documents={"urn:other": other_document})
    assert _matches("doc('urn:other')//product", product, environment=environment)
    assert _matches("doc('urn:other')/catalog/product[2]", product, environment=environment)
    assert not _matches("doc('urn:missing')//product", product, environment=environment)


def test_variable_rooted_patterns() -> None:
    eq_context = StaticContext(
        namespaces={"doc": DOC_NS},
        variables={
            f"{{{DOC_NS}}}v": StaticVariable(name=f"{{{DOC_NS}}}v"),
            f"{{{DOC_NS}}}w": StaticVariable(name=f"{{{DOC_NS}}}w"),
        },
        version="3.0",
    )
    eq_document = _document(f'<r xmlns:t="{DOC_NS}"><t:a/></r>')
    eq_a = _element_children(eq_document.root_element)[0]
    eq_environment = ExpressionEnvironment(
        variables={f"{{{DOC_NS}}}v": (eq_a,), f"{{{DOC_NS}}}w": (eq_a,)}
    )
    assert _matches("$Q{urn:example:doc}v", eq_a, context=eq_context, environment=eq_environment)
    assert _matches("$doc:w", eq_a, context=eq_context, environment=eq_environment)
    document = _document("<r><a><b/><b/></a><c/></r>")
    a = _element_children(document.root_element)[0]
    b = _element_children(a)[1]
    context = _context(variables=("v",))
    environment = ExpressionEnvironment(variables={"v": (a,)})

    assert _matches("$v", a, context=context, environment=environment)
    assert not _matches("$v", b, context=context, environment=environment)
    assert _matches("$v/b", b, context=context, environment=environment)
    assert _matches("$v/b[2]", b, context=context, environment=environment)
    assert not _matches("$v/b[1]", b, context=context, environment=environment)
    assert not _matches("$v", b, context=context, environment=ExpressionEnvironment())

    with pytest.raises(XsltStaticError) as caught:
        compile_pattern("$undeclared", context)
    assert _code(caught.value) == "XPST0008"


def test_key_and_id_rooted_functions(tmp_path: Path) -> None:
    compiled, schema = _schema_pool(tmp_path)
    document = _annotated(compiled, schema, f'<t:item xmlns:t="{DOC_NS}" t:code="x">value</t:item>')
    item = document.root_element
    context = _context(default_namespace=DOC_NS, schema=schema, variables=("ids",))

    assert _matches("id('x')", item, context=context)
    assert _matches("element-with-id('x')", item, context=context)
    assert not _matches("id('missing')", item, context=context)

    ids_environment = ExpressionEnvironment(variables={"ids": (XdmAtomic.string("x"),)})
    assert _matches("id($ids)", item, context=context, environment=ids_environment)

    key_environment = ExpressionEnvironment(key_indexes={"k": {"v": (item,)}})
    assert _matches("key('k', 'v')", item, context=context, environment=key_environment)
    assert _matches(
        "key('k', $ids)",
        item,
        context=context,
        environment=ExpressionEnvironment(
            variables={"ids": (XdmAtomic.string("v"),)},
            key_indexes={"k": {"v": (item,)}},
        ),
    )
    assert not _matches("key('missing', 'v')", item, context=context, environment=key_environment)


# ---------------------------------------------------------------------------
# Schema-aware tests


def test_schema_element_and_attribute_tests(tmp_path: Path) -> None:
    compiled, schema = _schema_pool(tmp_path)
    context = _context(default_namespace=DOC_NS, schema=schema)
    base = _annotated(compiled, schema, f'<t:base xmlns:t="{DOC_NS}" t:code="a"/>').root_element
    derived = _annotated(
        compiled, schema, f'<t:derived xmlns:t="{DOC_NS}" t:code="b"/>'
    ).root_element
    item = _annotated(compiled, schema, f'<t:item xmlns:t="{DOC_NS}" t:code="c"/>').root_element

    assert _matches("schema-element(doc:item)", item, context=context)
    assert _matches("schema-element(doc:base)", base, context=context)
    assert _matches("schema-element(doc:base)", derived, context=context)
    assert not _matches("schema-element(doc:item)", derived, context=context)

    code = item.attribute(f"{{{DOC_NS}}}code")
    assert code is not None
    assert _matches("@schema-attribute(doc:code)", code, context=context)
    assert _matches("@attribute(doc:code, xs:ID)", code, context=context)

    assert _matches("element(doc:item)", item, context=context)
    assert _matches("element(doc:item, doc:Holder)", item, context=context)
    assert _matches("element(*, doc:Holder)", item, context=context)
    assert not _matches("element(doc:item, doc:Payload)", item, context=context)
    assert _matches(
        "element(doc:payload, doc:Payload)",
        _annotated(compiled, schema, f'<t:payload xmlns:t="{DOC_NS}"/>').root_element,
        context=context,
    )

    with pytest.raises(XsltStaticError) as caught:
        compile_pattern("schema-element(doc:missing)", context)
    assert _code(caught.value) == "XPST0008"
    with pytest.raises(XsltStaticError) as caught:
        compile_pattern("schema-attribute(doc:missing)", context)
    assert _code(caught.value) == "XPST0008"

    # An unvalidated node has no governing declaration.
    untyped = _standalone_element(f'<t:item xmlns:t="{DOC_NS}" t:code="c"/>')
    assert not _matches("schema-element(doc:item)", untyped, context=context)
    assert not _matches(
        "@schema-attribute(doc:code)", untyped.attribute(f"{{{DOC_NS}}}code"), context=context
    )


def test_nilled_and_typed_element_tests(tmp_path: Path) -> None:
    compiled, schema = _schema_pool(tmp_path)
    context = _context(default_namespace=DOC_NS, schema=schema)
    payload = _annotated(
        compiled,
        schema,
        f'<t:payload xmlns:t="{DOC_NS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:nil="true"/>',
    ).root_element
    assert payload.annotation.nilled is True
    assert _matches("element(doc:payload)", payload, context=context)
    assert _matches("element(doc:payload, doc:Payload?)", payload, context=context)
    assert not _matches("element(doc:payload, doc:Payload)", payload, context=context)
    assert _matches("schema-element(doc:payload)", payload, context=context)


# ---------------------------------------------------------------------------
# Default priorities


def test_default_priorities(tmp_path: Path) -> None:
    _, schema = _schema_pool(tmp_path)
    context = _context(default_namespace=DOC_NS, schema=schema)

    cases = {
        "a": Decimal("0"),
        "doc:a": Decimal("0"),
        "a/b": Decimal("0.5"),
        "a[1]": Decimal("0.5"),
        "a | b": Decimal("0"),
        "*": Decimal("-0.5"),
        "doc:*": Decimal("-0.25"),
        "*:a": Decimal("-0.25"),
        "node()": Decimal("-0.5"),
        "text()": Decimal("-0.5"),
        "element()": Decimal("-0.5"),
        "element(*)": Decimal("-0.5"),
        "element(a)": Decimal("0"),
        "element(*, doc:Payload)": Decimal("0"),
        "element(a, doc:Payload)": Decimal("0.25"),
        "schema-element(doc:item)": Decimal("0.25"),
        "attribute()": Decimal("-0.5"),
        "attribute(code)": Decimal("0"),
        "attribute(*, xs:ID)": Decimal("0"),
        "attribute(code, xs:ID)": Decimal("0.25"),
        "schema-attribute(doc:code)": Decimal("0.25"),
        "/": Decimal("-0.5"),
        "document-node()": Decimal("-0.5"),
        "document-node(element(a))": Decimal("0"),
        "processing-instruction()": Decimal("-0.5"),
        "processing-instruction('x')": Decimal("0"),
        ".": Decimal("-1"),
        ".[1]": Decimal("1"),
        "(a)": Decimal("0"),
        "(a | b)": Decimal("0"),
    }
    for pattern, expected in cases.items():
        assert default_priority(pattern, context) == expected, pattern
        assert default_priority(compile_pattern(pattern, context)) == expected, pattern

    assert compile_pattern("a | b | c", context).default_priorities == (
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )
    assert default_priority("a") == Decimal("0")  # a single name needs no context
    with pytest.raises(XsltStaticError):
        default_priority("element(*, doc:Payload)")  # a prefixed name needs context
    with pytest.raises(TypeError):
        default_priority(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compile_pattern(1, context)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PatternIndex selection and rules


def _rule(
    pattern: str,
    *,
    context: StaticContext | None = None,
    priority: str | int | None = None,
    order: int = 0,
    precedence: tuple[int, ...] = (),
    mode: str | None = None,
    layer: object | None = None,
    name: str | None = None,
) -> TemplateRule:
    return TemplateRule(
        compile_pattern(pattern, context or _context()),
        priority=priority,
        declaration_order=order,
        precedence=precedence,
        mode=mode,
        package_layer=layer,
        name=name,
    )


def test_pattern_index_selects_by_priority_precedence_and_order() -> None:
    document = _document("<r><a code='1'/></r>")
    a = _element_children(document.root_element)[0]
    environment = ExpressionEnvironment()

    index = PatternIndex([_rule("a", order=1), _rule("*", order=2)])
    selected = index.select(a, environment=environment)
    assert selected is not None and selected.pattern.source == "a"

    index = PatternIndex([_rule("a", order=1), _rule("a", order=2)])
    selected = index.select(a, environment=environment)
    assert selected is not None and selected.declaration_order == 2

    index = PatternIndex([_rule("a", order=2), _rule("*", order=1, priority=5)])
    selected = index.select(a, environment=environment)
    assert selected is not None and selected.pattern.source == "*"

    index = PatternIndex(
        [_rule("a", order=2, precedence=(0, 1)), _rule("*", order=1, precedence=(0,))]
    )
    selected = index.select(a, environment=environment)
    assert selected is not None and selected.pattern.source == "*"

    index = PatternIndex([_rule("a[1]", order=1), _rule("a", order=2, priority=5)])
    selected = index.select(a, environment=environment)
    assert selected is not None and selected.pattern.source == "a"

    assert index.select(a, mode="other", environment=environment) is None
    assert index.select(a, package_layer="other", environment=environment) is None


def test_pattern_index_candidates_are_filtered_ordered_and_deduped() -> None:
    document = _document("<r><a/><b/></r>")
    a, b = _element_children(document.root_element)
    rule_a = _rule("a", order=5)
    rule_union = _rule("a | b", order=1)
    rule_other = _rule("c", order=2)
    index = PatternIndex([rule_other, rule_union, rule_a])

    candidates = index.candidates(None, None, a)
    assert candidates[0] is rule_a
    assert rule_union in candidates
    assert rule_other not in candidates
    assert len({id(rule) for rule in candidates}) == len(candidates)

    assert index.candidates(None, None, b) == (rule_union,)
    assert index.candidates(None, None, XdmAtomic.integer(1)) == ()
    assert index.candidates("missing", None, a) == ()


def test_pattern_index_predicate_patterns_match_non_nodes() -> None:
    rule = _rule(".[. instance of map(*)]", order=1)
    other = _rule("a", order=2)
    index = PatternIndex([other, rule])
    assert index.select(XdmMap.empty()) is rule
    assert index.select(XdmAtomic.integer(1), environment=ExpressionEnvironment()) is None
    assert index.select(XdmAtomic.integer(1)) is None


def test_pattern_index_statistics_and_cache() -> None:
    document = _document("<r><item/><item/></r>")
    first, second = _element_children(document.root_element)
    environment = ExpressionEnvironment()
    index = PatternIndex([_rule("item[1]")])
    assert index.select(first, environment=environment) is not None
    assert index.select(second, environment=environment) is None
    statistics = index.statistics
    assert statistics.cache_misses == 2
    assert statistics.equivalent_expression_evaluations == 0
    assert statistics.predicate_evaluations == 4

    assert index.select(first, environment=environment) is not None
    assert statistics.cache_hits == 1
    index.select(first, environment=ExpressionEnvironment())
    assert statistics.cache_invalidations == 1

    assert index.candidates(None, None, first)[0].pattern.source == "item[1]"


def test_template_rule_validation_and_immutability() -> None:
    pattern = compile_pattern("a", _context())
    rule = TemplateRule(pattern, priority="1.5", declaration_order=2, precedence=(1, 2))
    assert rule.priority == Decimal("1.5")
    assert rule.precedence == (1, 2)
    with pytest.raises(FrozenInstanceError):
        rule.priority = Decimal("2")  # type: ignore[misc]
    with pytest.raises(ValueError):
        TemplateRule(pattern, priority="not-a-number")
    with pytest.raises(ValueError):
        TemplateRule(pattern, priority=Decimal("Infinity"))
    with pytest.raises(TypeError):
        TemplateRule(pattern, priority=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TemplateRule(pattern, declaration_order=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TemplateRule(pattern, precedence=(1, "2"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TemplateRule(pattern, mode=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TemplateRule(pattern, package_layer=["not", "hashable"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TemplateRule(pattern, name=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TemplateRule(pattern, module=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TemplateRule("a")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PatternIndex([pattern])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        PatternIndex([], strategy="magic")
    with pytest.raises(TypeError):
        PatternIndex([]).select(XdmAtomic.integer(1), environment="not-an-environment")  # type: ignore[arg-type]


def test_precedence_comparison_and_alternative_order() -> None:
    document = _document("<r><a/></r>")
    a = _element_children(document.root_element)[0]
    # Different integer steps at the same depth: the later import wins.
    left = _rule("a", order=1, precedence=(0, 2))
    right = _rule("*", order=2, precedence=(0, 1))
    index = PatternIndex([left, right])
    selected = index.select(a, environment=ExpressionEnvironment())
    assert selected is not None and selected.pattern.source == "a"
    # Identical precedence compares equal.
    twin = _rule("a", order=9)
    assert PatternIndex([twin, twin]).select(a, environment=ExpressionEnvironment()) is twin

    union = _rule("a | a", order=1)
    candidates = PatternIndex([union]).candidates(None, None, a)
    assert candidates == (union,)


def test_pattern_index_iteration_and_selection_defaults() -> None:
    rule = _rule("a")
    index = PatternIndex([rule])
    assert list(index) == [rule]
    assert len(index) == 1
    document = _document("<r><a/></r>")
    a = _element_children(document.root_element)[0]
    # select() creates its own environment when none is supplied.
    assert index.select(a) is rule
    assert index.candidates(None, None, XdmAtomic.integer(1)) == ()


# ---------------------------------------------------------------------------
# Differential testing against the formal whole-tree plan


def _candidates(document: XdmDocument) -> tuple[XdmNode, ...]:
    root = document.root_element
    nodes: list[XdmNode] = [document, root, *root.descendants]
    for node in (root, *root.descendants):
        if node.kind is NodeKind.ELEMENT:
            nodes.extend(node.attributes)
            nodes.extend(node.namespace_nodes)
    return tuple(nodes)


def test_optimized_matchers_agree_with_formal_whole_tree_plan() -> None:
    document = _document(
        "<r><a code='1'><b><c/><d/></b></a><b code='2'/><c/>"
        "<item/><item/>text<!--note--><?pi data?></r>"
    )
    other_document = _document("<catalog><product/><product/><item/></catalog>")
    variables = {
        "v": (_element_children(document.root_element)[0],),
        "ids": (XdmAtomic.string("2"),),
    }
    key_node = _element_children(document.root_element)[1]
    environment = ExpressionEnvironment(
        variables=variables,
        documents={"urn:other": other_document},
        key_indexes={"k": {"2": (key_node,)}},
    )
    context = _context(variables=("v", "ids"))

    patterns = [
        ".",
        ".[. instance of node()]",
        ".[. instance of xs:integer]",
        "a",
        "*",
        "node()",
        "text()",
        "comment()",
        "processing-instruction()",
        "processing-instruction('pi')",
        "document-node()",
        "a/b",
        "a//b",
        "r//b",
        "a/b/(c | d)",
        "a/(b | d)",
        "(a | b)/*",
        "a/b/c",
        "a/b/d",
        "//b",
        "/r",
        "/",
        "@code",
        "attribute::code",
        "a[@code]",
        "a[1]",
        "item[1]",
        "item[last()]",
        "item[2]",
        "a | b",
        "a intersect b",
        "* except a",
        "a except a",
        "$v",
        "$v/b",
        "$v/b[2]",
        "doc('urn:other')//product",
        "doc('urn:other')/catalog/item",
        "key('k', '2')",
        "key('k', $ids)",
        "item[current()/self::item]",
        ".[current() instance of node()]",
        "element()",
        "attribute()",
        "self::a",
        "descendant-or-self::b",
        "namespace::*",
    ]
    candidates = (*_candidates(document), *_candidates(other_document), XdmAtomic.integer(1))
    for pattern in patterns:
        compiled = compile_pattern(pattern, context)
        optimized = PatternMatcher(compiled)
        formal = PatternMatcher(compiled, strategy="forward")
        for item in candidates:
            assert optimized.matches(item, environment) == formal.matches(item, environment), (
                pattern,
                item.kind if isinstance(item, XdmNode) else type(item).__name__,
            )


def test_forward_plan_reports_whole_tree_operations() -> None:
    document = _document("<r><a><b/></a></r>")
    pattern = compile_pattern("a/b", _context())
    matcher = PatternMatcher(pattern, strategy="forward")
    environment = ExpressionEnvironment()
    target = _element_children(_element_children(document.root_element)[0])[0]
    assert matcher.matches(target, environment)
    assert matcher.statistics.equivalent_expression_evaluations == 1
    # Re-selecting the same candidate uses the memoized boolean result.
    assert matcher.matches(target, environment)
    assert matcher.statistics.cache_hits == 1


def test_pattern_statistics_shape() -> None:
    statistics = PatternStatistics()
    assert statistics.equivalent_expression_evaluations == 0
    assert statistics.candidate_filters == 0
    assert statistics.ancestor_steps == 0
    assert statistics.errors_ignored == 0


# ---------------------------------------------------------------------------
# Grammar, scanner, and invariant edges


def test_scanner_lexical_edges() -> None:
    document = _document("<r><a code='1'/></r>")
    a = _element_children(document.root_element)[0]

    assert _matches("a (: a comment :) ", a)
    assert _matches("a[(: outer (: inner :) :) true()]", a)
    assert not _matches("doc('it''s')", a)
    assert _matches("a['it''s']", a)
    assert _matches("a[current]", a) is False
    with pytest.raises(XsltStaticError):
        compile_pattern("key('k')", _context())
    assert _matches("Q{http://www.w3.org/2005/xpath-functions}doc('missing')", a) is False
    assert not _matches("key('k', 1)", a)
    assert not _matches("key('k', 1.5)", a)
    assert not _matches("key('k', 2e3)", a)

    invalid = [
        "doc('x)",
        "Q{urn:x",
        "Q{urn:x}",
        "*:",
        "a:",
        "a (: x",
        "$",
        "doc($)",
        "doc(current())",
        "Q{}doc('x')",
        "other:doc('x')",
        "key('k', 1, .)",
        "id('x', 1, 2)",
        ".[.] | a",
        "item[current(1)]",
    ]
    for pattern in invalid:
        with pytest.raises(XsltStaticError):
            compile_pattern(pattern, _context(variables=("v",)))

    assert _error_code(ValueError("plain")) == "XPST0003"
    assert _error_code(Exception("wrapped [err:XPST0008] here")) == "XPST0008"
    assert _error_code(type("Coded", (Exception,), {"code": "XPST0017"})()) == "XPST0017"
    assert _local_name("{urn:a}b") == "b"
    assert _local_name("b") == "b"
    assert _namespace_of("b") is None
    assert _namespace_of("{urn:a}b") == "urn:a"
    assert NameTest(None, "other", None).matches(a) is False
    assert NameTest(None, "a", None).matches(a) is True
    assert _builtin_class("urn:not-xsd") is None
    assert _builtin_derives(f"{{{XSD_NS}}}x", "{urn:other}x") is True
    with pytest.raises(TypeError):
        compile_pattern("a", object())  # type: ignore[arg-type]
    assert _effective_boolean_value((XdmAtomic.double(float("nan")),)) is False
    assert _effective_boolean_value((XdmAtomic.double(0.0),)) is False
    assert _effective_boolean_value((XdmAtomic.double(1.0),)) is True


def test_dataclass_and_matcher_invariants() -> None:
    context = _context()
    pattern = compile_pattern("a", context)
    predicate = compile_pattern(".[.]", context).alternatives[0].predicate
    assert predicate is not None

    with pytest.raises(ValueError):
        NameTest("a", "b", None)
    with pytest.raises(ValueError):
        RelativePath(())
    with pytest.raises(ValueError):
        PatternAlternative(0, Decimal(0))
    with pytest.raises(ValueError):
        PatternAlternative(0, Decimal(0), predicate=predicate, path=pattern.alternatives[0].path)
    with pytest.raises(ValueError):
        CompiledPattern("", context, pattern.alternatives)
    with pytest.raises(TypeError):
        CompiledPattern("a", object(), pattern.alternatives)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CompiledPattern("a", context, ())
    with pytest.raises(TypeError):
        PatternMatcher("a")  # type: ignore[arg-type]
    matcher = PatternMatcher(pattern)
    with pytest.raises(TypeError):
        matcher.matches(XdmAtomic.integer(1), "not-an-environment")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        matcher.matches_alternative(0, XdmAtomic.integer(1), "bad")  # type: ignore[arg-type]

    element = _parentless_element("a")
    shape = CandidateShape(frozenset({NodeKind.ELEMENT}))
    assert shape.matches(XdmAtomic.integer(1)) is False
    assert CandidateShape(frozenset({NodeKind.TEXT})).matches(element) is False
    assert CandidateShape(frozenset({NodeKind.ELEMENT}), name="b").matches(element) is False
    assert CandidateShape(frozenset({NodeKind.ELEMENT}), local="b").matches(element) is False
    assert (
        CandidateShape(
            frozenset({NodeKind.ELEMENT}), namespace="urn:b", any_namespace=False
        ).matches(element)
        is False
    )
    assert CandidateShape(frozenset({NodeKind.ELEMENT}), target="b").matches(element) is False
    assert CandidateFilter().matches(element) is False


def test_axes_kind_tests_and_reverse_context_branches() -> None:
    document = _document('<r xmlns:p="urn:p"><a code="1"><b/></a><c/><!--n--><?pi d?>text</r>')
    root = document.root_element
    a, _c, _comment, pi, text = root.children
    code = a.attribute("code")
    assert code is not None

    assert _matches("a/@code", code)
    assert _matches("a/namespace::*", a.namespace_node("p"))
    assert _matches("a/namespace::namespace-node()", a.namespace_node("p"))
    assert _matches("a/descendant::text()", _document("<a>x</a>").root_element.children[0])
    assert _matches("a/descendant-or-self::*", a)
    assert _matches("a/self::a", a)
    assert _matches("a/child::b", _element_children(a)[0])
    parentless_namespace = _parentless_namespace()
    assert _matches("namespace::*", parentless_namespace)
    assert not _matches("node()", parentless_namespace)
    assert _matches("text()", text)
    assert _matches("processing-instruction('pi')", pi)
    assert not _matches("processing-instruction('other')", pi)

    matcher = PatternMatcher(compile_pattern("a", _context()))
    attribute_step = compile_pattern("@code", _context()).alternatives[0].path.first.steps[0][1]
    assert isinstance(attribute_step, AxisStep)
    assert matcher._reverse_contexts(attribute_step, a, top=False) == ()
    namespace_step = (
        compile_pattern("namespace::*", _context()).alternatives[0].path.first.steps[0][1]
    )
    assert isinstance(namespace_step, AxisStep)
    assert matcher._reverse_contexts(namespace_step, a, top=False) == ()
    descendant_step = (
        compile_pattern("descendant::text()", _context()).alternatives[0].path.first.steps[0][1]
    )
    assert isinstance(descendant_step, AxisStep)
    assert matcher._reverse_contexts(descendant_step, code, top=False) == ()
    union_step = (
        compile_pattern("descendant-or-self::*", _context()).alternatives[0].path.first.steps[0][1]
    )
    assert isinstance(union_step, AxisStep)
    assert matcher._reverse_contexts(union_step, code, top=False) == ()
    child_step = compile_pattern("child::*", _context()).alternatives[0].path.first.steps[0][1]
    assert isinstance(child_step, AxisStep)
    assert matcher._reverse_contexts(child_step, code, top=False) == ()
    self_step = compile_pattern("self::*", _context()).alternatives[0].path.first.steps[0][1]
    assert isinstance(self_step, AxisStep)
    assert _axis_nodes(self_step, a, False) == (a,)
    descendant_nodes = _axis_nodes(descendant_step, a, False)
    assert descendant_nodes
    assert _axis_nodes(union_step, a, False)[0] is a
    assert _axis_nodes(attribute_step, a, False) == (code,)
    parentless_attribute = _parentless_attribute("code")
    assert _axis_nodes(attribute_step, parentless_attribute, True) == (parentless_attribute,)
    assert not _matches("child::document-node()", document)


def test_type_and_declaration_helper_branches(tmp_path: Path) -> None:
    _compiled, schema = _schema_pool(tmp_path)
    context = _context(default_namespace=DOC_NS, schema=schema)
    untyped = _standalone_element("<item/>")
    untyped_attribute = _document("<r code='1'/>").root_element.attribute("code")
    assert untyped_attribute is not None
    assert _matches("element(*, xs:untyped)", untyped, context=context)
    assert not _matches("element(*, xs:untypedAtomic)", untyped, context=context)

    assert _matches("element(*, xs:anyType)", untyped, context=context)
    assert not _matches("element(*, xs:anySimpleType)", untyped, context=context)
    assert _matches("@attribute(*, xs:anyAtomicType)", untyped_attribute, context=context)
    assert not _matches("element(*, xs:string)", untyped, context=context)
    assert _builtin_derives(None, f"{{{XSD_NS}}}string") is False
    assert _builtin_derives(f"{{{XSD_NS}}}int", f"{{{XSD_NS}}}integer") is True
    assert _builtin_derives(f"{{{XSD_NS}}}string", f"{{{XSD_NS}}}integer") is False
    assert _builtin_derives(f"{{{XSD_NS}}}untyped", f"{{{XSD_NS}}}anyType") is True
    assert _builtin_derives(f"{{{XSD_NS}}}untyped", f"{{{XSD_NS}}}anySimpleType") is False
    assert _builtin_derives(f"{{{XSD_NS}}}untypedAtomic", f"{{{XSD_NS}}}anyAtomicType") is True

    assert _declaration_key(None, require_global=False) is None
    assert _declaration_key(type("NoName", (), {"name": None})(), require_global=False) is None

    class Local:
        name = "item"
        parent = type("Parent", (), {"target_namespace": "urn:x"})()

        def isGlobalDeclaration(self) -> bool:
            return False

    assert _declaration_key(Local(), require_global=True) is None
    assert _declaration_key(Local(), require_global=False) == "{urn:x}item"
    assert _declaration_key(type("Clark", (), {"name": "{urn:x}item"})(), require_global=False) == (
        "{urn:x}item"
    )

    # A document-node test with a schema-element child uses declaration identity.
    annotated = _annotated(_compiled, schema, f'<t:item xmlns:t="{DOC_NS}" t:code="x">v</t:item>')
    assert _matches("document-node(schema-element(doc:item))", annotated, context=context)
    assert not _matches("document-node(schema-element(doc:payload))", annotated, context=context)


def test_id_key_and_document_edge_branches(tmp_path: Path) -> None:
    compiled, schema = _schema_pool(tmp_path)
    context = _context(
        default_namespace=DOC_NS, schema=schema, variables=("ids", "node", "top", "missing")
    )
    item = _annotated(
        compiled, schema, f'<t:item xmlns:t="{DOC_NS}" t:code="x">value</t:item>'
    ).root_element
    keyed = _annotated(compiled, schema, f'<t:keyed xmlns:t="{DOC_NS}">k</t:keyed>').root_element
    documents = {"alias": XdmDocument.parse(b"<catalog/>", document_uri="urn:direct")}
    environment = ExpressionEnvironment(
        variables={"ids": (XdmAtomic.string("x"),), "node": (item,), "top": (item,)},
        documents=documents,
    )

    assert _matches("id($ids, $node)", item, context=context, environment=environment)
    assert _matches("id('x', $top)", item, context=context, environment=environment)
    assert not _matches(
        "id('x', $ids)",
        item,
        context=context,
        environment=environment,
    )
    assert _matches(
        "id($node)",
        keyed,
        context=context,
        environment=ExpressionEnvironment(variables={"node": (keyed,)}),
    )
    assert not _matches("id('')", item, context=context)
    assert _matches(
        "id($ids)",
        keyed,
        context=context,
        environment=ExpressionEnvironment(variables={"ids": (XdmAtomic.string("k"),)}),
    )
    assert not _matches(
        "id($missing)",
        item,
        context=context,
        environment=ExpressionEnvironment(),
    )

    key_indexes = {
        "k": {
            "x": (item,),
            "value": (item,),
            "k": (keyed,),
        }
    }
    key_environment = ExpressionEnvironment(
        variables={"ids": (XdmAtomic.string("x"),), "node": (item,), "top": (item,)},
        key_indexes=key_indexes,
    )
    assert _matches("key('k', $node)", item, context=context, environment=key_environment)
    assert not _matches("key('k', 'k')", item, context=context, environment=key_environment)
    assert _matches("key('k', $ids, $top)", item, context=context, environment=key_environment)
    assert not _matches("key('k', $ids, $top)", keyed, context=context, environment=key_environment)
    assert not _matches(
        "key('k', $missing)",
        item,
        context=context,
        environment=key_environment,
    )
    assert not _matches(
        "key('k', $node)",
        item,
        context=context,
        environment=ExpressionEnvironment(
            variables={"node": (XdmMap.empty(),)},
            key_indexes=key_indexes,
        ),
    )

    # doc() resolves against the environment base URI and document URIs.
    relative = ExpressionEnvironment(
        documents={"file:///tmp/base/doc.xml": XdmDocument.parse(b"<x/>")},
        base_uri="file:///tmp/base/",
    )
    assert _matches(
        "doc('doc.xml')",
        relative.documents["file:///tmp/base/doc.xml"],
        context=context,
        environment=relative,
    )
    assert _matches(
        "doc('urn:direct')",
        documents["alias"],
        context=context,
        environment=ExpressionEnvironment(documents=documents),
    )
    assert not _matches(
        "doc($ids)",
        item,
        context=context,
        environment=ExpressionEnvironment(
            variables={"ids": (XdmAtomic.string("a"), XdmAtomic.string("b"))}
        ),
    )
    assert not _matches(
        "doc($node)",
        item,
        context=context,
        environment=ExpressionEnvironment(variables={"node": (XdmMap.empty(),)}),
    )
    assert not _matches(
        "doc($node)",
        item,
        context=context,
        environment=ExpressionEnvironment(variables={"node": (item,)}),
    )
    assert not _matches(
        "id($node)",
        item,
        context=context,
        environment=ExpressionEnvironment(variables={"node": (XdmMap.empty(),)}),
    )
    parentless = _parentless_element("x")
    assert not PatternMatcher(compile_pattern("/x", context), strategy="forward").matches(
        parentless, ExpressionEnvironment()
    )
    assert not PatternMatcher(compile_pattern("//x", context), strategy="forward").matches(
        parentless, ExpressionEnvironment()
    )


def test_predicates_on_parentless_non_element_nodes() -> None:
    text = _parentless_node(NodeKind.TEXT, value="text")
    comment = _parentless_node(NodeKind.COMMENT, value="comment")
    pi = _parentless_node(NodeKind.PROCESSING_INSTRUCTION, name="pi", value="data", target="pi")
    assert _matches(".[. instance of node()]", text)
    assert _matches(".[. instance of node()]", comment)
    assert _matches(".[. instance of node()]", pi)
    assert _matches(".[true()]", _parentless_attribute("code"))
    # A parentless namespace node cannot be represented as an elementpath
    # focus; the matcher treats the resulting evaluation error as a non-match.
    assert not _matches(".[true()]", _parentless_namespace())


def test_predicate_effective_boolean_value_branches() -> None:
    document = _document("<r><item>text</item><item/></r>")
    first, _second = _element_children(document.root_element)
    assert not _matches("item[()]", first)
    assert not _matches("item[false()]", first)
    assert _matches("item[true()]", first)
    assert not _matches("item[0]", first)
    assert _matches("item[1]", first)
    assert not _matches("item['']", first)
    assert _matches("item['x']", first)
    assert not _matches("item[number('NaN')]", first)
    assert not _matches("item[(1, 2)]", first)
    assert not _matches("item[xs:QName('x')]", first)
    assert not _matches("item[position() = 2]", first)

    assert _numeric_equals_position(float("nan"), 1) is False
    assert _numeric_equals_position(2, 1) is False
    assert _effective_boolean_value(()) is False
    with pytest.raises(TypeError):
        _effective_boolean_value((XdmAtomic.integer(1), XdmAtomic.integer(2)))
    assert _effective_boolean_value((XdmAtomic.string("x"),)) is True
    assert _effective_boolean_value((XdmAtomic.boolean(True),)) is True
    assert _effective_boolean_value((XdmAtomic.boolean(False),)) is False
    assert _is_descendant_or_self(first, document) is True
    assert _is_descendant_or_self(document, first) is False
    assert _is_descendant_or_self(first, _document("<other/>").root_element) is False


def test_more_default_priorities_and_candidate_filters() -> None:
    context = _context()
    assert default_priority("/a", context) == Decimal("0.5")
    assert default_priority("//a", context) == Decimal("0.5")
    assert default_priority("$v", _context(variables=("v",))) == Decimal("0.5")
    assert default_priority("(a)[1]", context) == Decimal("0.5")
    assert default_priority("Q{urn:x}a", context) == Decimal("0")
    assert default_priority("Q{urn:x}*", context) == Decimal("-0.25")
    assert default_priority("Q{}*", context) == Decimal("-0.25")
    assert default_priority("child::document-node()", context) == Decimal("-0.5")
    assert default_priority("descendant::node()", context) == Decimal("-0.5")

    document = _document("<r><a/></r>")
    a = _element_children(document.root_element)[0]
    rule = _rule("a")
    index = PatternIndex([rule])
    assert index.candidates(None, None, XdmAtomic.integer(1)) == ()
    # Wrong kind, name, and namespace shapes are rejected cheaply.
    assert PatternIndex([_rule("@code")]).candidates(None, None, a) == ()
    assert PatternIndex([_rule("b")]).candidates(None, None, a) == ()
    assert PatternIndex([_rule("other:*")]).candidates(None, None, a) == ()
