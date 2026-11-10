"""Dedicated XSLT value-template and sequence-type grammar compilers.

AVTs, TVTs, and sequence types are not XPath expressions.  Each has its own
grammar, brace/occurrence rules, QName context, and version constraints, while
embedded XPath expressions compile through the Task 11 expression engine.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from elementpath.xpath3 import XPath31Parser

from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.expressions import (
    CompiledSequenceType,
    CompiledValueTemplate,
    ItemType,
    TemplatePart,
    compile_avt,
    compile_sequence_type,
    compile_tvt,
    compile_xpath,
)
from pyxsd.xslt.compiler.modules import ModuleLoader
from pyxsd.xslt.compiler.static import EffectiveModule, StaticPreprocessor
from pyxsd.xslt.errors import XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
DOC_NS = "urn:example:doc"
OTHER_NS = "urn:example:other"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _module(tmp_path: Path, xml: str, *, name: str = "module.xsl") -> EffectiveModule:
    path = _write(tmp_path, name, xml)
    return StaticPreprocessor().apply(ModuleLoader().load(path))


def _stylesheet(body: str = "", *, attributes: str = "", doc_namespace: str = DOC_NS) -> str:
    return (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:doc="{doc_namespace}"{attributes}>{body}</xsl:stylesheet>'
    )


def _context(
    tmp_path: Path,
    body: str = "",
    *,
    attributes: str = "",
    doc_namespace: str = DOC_NS,
) -> StaticContext:
    return StaticContext.for_module(
        _module(
            tmp_path,
            _stylesheet(body, attributes=attributes, doc_namespace=doc_namespace),
        )
    )


def _code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


def _schema(tmp_path: Path) -> SchemaPool:
    path = tmp_path / "schema.xsd"
    path.write_text(
        f'<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{DOC_NS}" '
        f'xmlns:doc="{DOC_NS}" elementFormDefault="qualified">'
        '<xs:element name="item" type="xs:string"/>'
        '<xs:attribute name="code" type="xs:int"/>'
        '<xs:simpleType name="Payload"><xs:restriction base="xs:string"/></xs:simpleType>'
        '<xs:complexType name="Container"><xs:sequence/></xs:complexType>'
        "</xs:schema>",
        encoding="utf-8",
    )
    return SchemaPool.from_imports([path])


def test_context_and_template_compilers(tmp_path: Path) -> None:
    context = _context(tmp_path)
    avt = compile_avt("{@code}-{{literal}}", context)
    assert avt.parts[0].kind == "expression"
    assert avt.parts[1].kind == "text"
    assert avt.parts[1].text == "-{literal}"
    assert compile_tvt("Value: {$price}", context.with_expand_text(True)).parts
    assert (
        str(compile_sequence_type("map(xs:string, array(item()*))", context))
        == "map(xs:string, array(item()*))"
    )


# ---------------------------------------------------------------------------
# Attribute and text value templates


def test_avt_parts_escaped_braces_and_qname_context(tmp_path: Path) -> None:
    context = _context(tmp_path)
    template = compile_avt(
        "start {{brace}} {doc:root/doc:item/@code} {{}} {'}'} end",
        context,
    )

    assert [part.kind for part in template.parts] == [
        "text",
        "expression",
        "text",
        "expression",
        "text",
    ]
    assert template.parts[0].text == "start {brace} "
    assert template.parts[1].text == "doc:root/doc:item/@code"
    assert template.parts[2].text == " {} "
    assert template.parts[3].text == "'}'"
    assert template.parts[4].text == " end"
    assert template.parts[1].expression is not None
    assert template.parts[1].expression.static_context.namespaces["doc"] == DOC_NS
    assert template.expressions == (
        template.parts[1].expression,
        template.parts[3].expression,
    )
    assert template.expand_text is False
    assert template.text_value_template is False
    assert str(template) == template.source

    document = XdmDocument.parse(b'<root xmlns="urn:example:doc"><item code="42"/></root>')
    value = XPathEngine(context.xpath_version).evaluate(
        template.parts[1].expression,
        ExpressionEnvironment(),
        focus=document,
    )
    assert value[0].string_value == "42"


def test_avt_nested_braces_quotes_and_comments(tmp_path: Path) -> None:
    context = _context(tmp_path)
    for source, expression in (
        ("{map{'a': map{'b': 1}}}", "map{'a': map{'b': 1}}"),
        ("{'}'}", "'}'"),
        ('{"}"}', '"}"'),
        ("{ (: } :) 1 }", " (: } :) 1 "),
        ("{1 + (: { :) 2}", "1 + (: { :) 2"),
    ):
        template = compile_avt(source, context)
        assert len(template.parts) == 1
        assert template.parts[0].kind == "expression"
        assert template.parts[0].text == expression

    assert compile_avt("{}", context).parts == ()
    assert compile_avt("a{}b", context).parts == (TemplatePart(kind="text", text="ab"),)
    assert compile_avt("", context).parts == ()


def test_avt_errors_are_exact_static_errors(tmp_path: Path) -> None:
    context = _context(tmp_path)
    for source, code in (
        ("{unclosed", "XTSE0350"),
        ("{1 + 2", "XTSE0350"),
        ("before } after", "XTSE0370"),
        ("{1 +}", "XPST0003"),
        ("{unknown:item}", "XPST0081"),
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_avt(source, context)
        assert _code(caught.value) == code
        assert caught.value.uri == context.uri
        assert caught.value.location is not None


def test_tvt_expand_text_semantics(tmp_path: Path) -> None:
    context = _context(tmp_path)
    expanded = compile_tvt("Value: {$price}", context.with_expand_text(True))
    assert [part.kind for part in expanded.parts] == ["text", "expression"]
    assert expanded.parts[0].text == "Value: "
    assert expanded.parts[1].text == "$price"
    assert expanded.expand_text is True
    assert expanded.text_value_template is True

    literal = compile_tvt("Value: {$price} {{2+2}}", context)
    assert literal.parts == (TemplatePart(kind="text", text="Value: {$price} {{2+2}}"),)

    assert compile_tvt("{{2+2}}", context.with_expand_text(True)).parts[0].text == "{2+2}"
    assert compile_tvt("{{2+2}}", context, expand_text=False).parts[0].text == "{{2+2}}"
    assert compile_tvt("", context, expand_text=True).parts == ()

    effective = compile_tvt("x", context, expand_text=True).with_expand_text(False)
    assert effective.expand_text is False
    with pytest.raises(XsltStaticError) as caught:
        compile_tvt("{$price", context.with_expand_text(True))
    assert _code(caught.value) == "XTSE0350"


# ---------------------------------------------------------------------------
# Sequence types: item and node tests


def test_sequence_type_basic_items_and_occurrences(tmp_path: Path) -> None:
    context = _context(tmp_path)
    cases = {
        "item()": "item()",
        "item()*": "item()*",
        "node()": "node()",
        "node()+": "node()+",
        "text()?": "text()?",
        "comment()": "comment()",
        "namespace-node()": "namespace-node()",
        "processing-instruction()": "processing-instruction()",
        "processing-instruction(foo)": "processing-instruction(foo)",
        "processing-instruction('foo')": "processing-instruction('foo')",
        "empty-sequence()": "empty-sequence()",
        "xs:integer?": "xs:integer?",
        "xs:string+": "xs:string+",
    }
    for source, expected in cases.items():
        compiled = compile_sequence_type(source, context)
        assert isinstance(compiled, CompiledSequenceType)
        assert str(compiled) == expected
        assert compiled.source == source

    integer = compile_sequence_type("xs:integer?", context)
    assert integer.occurrence == "?"
    assert integer.item_type is not None
    assert integer.item_type.kind == "atomic"
    assert integer.item_type.name == f"{{{XSD_NS}}}integer"
    assert integer.item_type.lexical == "xs:integer"

    assert compile_sequence_type("empty-sequence()", context).item_type is None
    assert compile_sequence_type("item()*", context).item_type is not None
    assert compile_sequence_type("item()*", context).item_type.kind == "item"

    with pytest.raises(FrozenInstanceError):
        integer.occurrence = "*"  # type: ignore[misc]

    for source in ("", "   ", "empty-sequence()?", "item()??", "xs:int xs:string", "("):
        with pytest.raises(XsltStaticError) as caught:
            compile_sequence_type(source, context)
        assert _code(caught.value) == "XPST0003"
        assert caught.value.uri == context.uri


def test_sequence_type_element_and_attribute_tests(tmp_path: Path) -> None:
    context = _context(tmp_path, attributes=' xpath-default-namespace="urn:elements"')
    cases = {
        "element()": "element()",
        "element(*)": "element(*)",
        "element(doc:item)": "element(doc:item)",
        "element(item)": "element(item)",
        "element(*, xs:untyped)": "element(*, xs:untyped)",
        "element(doc:item, xs:string?)": "element(doc:item, xs:string?)",
        "element(doc:item, xs:anyType?)": "element(doc:item, xs:anyType?)",
        "element(doc:item, xs:untyped)?": "element(doc:item, xs:untyped)?",
        "attribute()": "attribute()",
        "attribute(*)": "attribute(*)",
        "attribute(doc:code)": "attribute(doc:code)",
        "attribute(code)": "attribute(code)",
        "attribute(doc:code, xs:int)": "attribute(doc:code, xs:int)",
    }
    for source, expected in cases.items():
        assert str(compile_sequence_type(source, context)) == expected

    element = compile_sequence_type("element(doc:item, xs:string?)", context).item_type
    assert element is not None
    assert element.kind == "element"
    assert element.name == f"{{{DOC_NS}}}item"
    assert element.type_name == f"{{{XSD_NS}}}string"
    assert element.nillable is True

    defaulted = compile_sequence_type("element(item)", context).item_type
    assert defaulted is not None
    assert defaulted.name == "{urn:elements}item"
    defaulted_attribute = compile_sequence_type("attribute(code)", context).item_type
    assert defaulted_attribute is not None
    assert defaulted_attribute.name == "code"

    for source in (
        "attribute(doc:code, xs:anyType)",
        "element(doc:item,)",
        "attribute(doc:code, xs:int?)",
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_sequence_type(source, context)
        assert _code(caught.value) in {"XPST0003", "XPST0051"}


def test_sequence_type_document_and_schema_tests(tmp_path: Path) -> None:
    context = _context(tmp_path).with_schema(_schema(tmp_path))
    cases = {
        "document-node()": "document-node()",
        "document-node(element(doc:item))": "document-node(element(doc:item))",
        "document-node(element(doc:item, xs:string))": (
            "document-node(element(doc:item, xs:string))"
        ),
        "document-node(schema-element(doc:item))": ("document-node(schema-element(doc:item))"),
        "schema-element(doc:item)": "schema-element(doc:item)",
        "schema-attribute(doc:code)": "schema-attribute(doc:code)",
    }
    for source, expected in cases.items():
        assert str(compile_sequence_type(source, context)) == expected

    document_test = compile_sequence_type(
        "document-node(schema-element(doc:item))", context
    ).item_type
    assert document_test is not None
    assert document_test.kind == "document-node"
    assert document_test.child is not None
    assert document_test.child.kind == "schema-element"
    assert document_test.child.name == f"{{{DOC_NS}}}item"

    schema_element = compile_sequence_type("schema-element(doc:item)", context).item_type
    assert schema_element is not None
    assert schema_element.name == f"{{{DOC_NS}}}item"

    with pytest.raises(XsltStaticError) as caught:
        compile_sequence_type("schema-element(doc:missing)", context)
    assert _code(caught.value) == "XPST0008"
    with pytest.raises(XsltStaticError) as caught:
        compile_sequence_type("schema-attribute(doc:missing)", context)
    assert _code(caught.value) == "XPST0008"
    with pytest.raises(XsltStaticError) as caught:
        compile_sequence_type("schema-element(doc:item)", _context(tmp_path))
    assert _code(caught.value) == "XPST0008"

    for source in (
        "document-node(text())",
        "document-node(document-node())",
        "schema-element(*)",
        "schema-attribute(doc:code, xs:int)",
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_sequence_type(source, context)
        assert _code(caught.value) in {"XPST0003", "XPST0008"}


def test_sequence_type_function_map_and_array_tests(tmp_path: Path) -> None:
    context = _context(tmp_path)
    cases = {
        "function(*)": "function(*)",
        "function(*) ?": "function(*)?",
        "function(xs:string, item()*) as xs:boolean": (
            "function(xs:string, item()*) as xs:boolean"
        ),
        "function() as empty-sequence()": "function() as empty-sequence()",
        "map(*)": "map(*)",
        "map(xs:string, item()*)": "map(xs:string, item()*)",
        "map(xs:string, array(item()*))?": "map(xs:string, array(item()*))?",
        "array(*)": "array(*)",
        "array(item()*)": "array(item()*)",
        "array(xs:int+)": "array(xs:int+)",
    }
    for source, expected in cases.items():
        assert str(compile_sequence_type(source, context)) == expected

    function = compile_sequence_type(
        "function(xs:string, item()*) as xs:boolean", context
    ).item_type
    assert function is not None
    assert function.kind == "function"
    assert tuple(str(parameter) for parameter in function.parameters) == (
        "xs:string",
        "item()*",
    )
    assert function.return_type is not None
    assert str(function.return_type) == "xs:boolean"

    mapping = compile_sequence_type("map(xs:string, array(item()*))", context).item_type
    assert mapping is not None
    assert mapping.kind == "map"
    assert mapping.key_type is not None
    assert mapping.key_type.name == f"{{{XSD_NS}}}string"
    assert mapping.value_type is not None
    assert str(mapping.value_type) == "array(item()*)"

    for source in (
        "function()",
        "function(*) as item()",
        "map()",
        "map(item(), xs:string)",
        "map(xs:anyType, xs:string)",
        "array()",
        "array(*, xs:string)",
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_sequence_type(source, context)
        assert _code(caught.value) in {"XPST0003", "XPST0051"}


def test_sequence_type_version_gating(tmp_path: Path) -> None:
    legacy = _context(tmp_path).with_version("2.0")
    assert legacy.xpath_version == "2.0"
    assert str(compile_sequence_type("item()*", legacy)) == "item()*"
    assert str(compile_sequence_type("element(doc:item, xs:string)", legacy)) == (
        "element(doc:item, xs:string)"
    )
    for source in (
        "map(*)",
        "map(xs:string, item()*)",
        "array(*)",
        "array(item()*)",
        "function(*)",
        "function(xs:string) as xs:string",
        "namespace-node()",
        "(item())",
        "Q{urn:example}item",
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_sequence_type(source, legacy)
        assert _code(caught.value) == "XPST0003"

    assert str(compile_sequence_type("function(*)", legacy.with_version("3.0"))) == ("function(*)")
    assert str(compile_sequence_type("map(*)", legacy.with_version("3.0"))) == "map(*)"


def test_sequence_type_qname_and_type_errors(tmp_path: Path) -> None:
    context = _context(tmp_path)
    for source in ("unknown:item", "element(unknown:item)", "element(doc:item, unknown:t)"):
        with pytest.raises(XsltStaticError) as caught:
            compile_sequence_type(source, context)
        assert _code(caught.value) == "XPST0081"

    for source in ("xs:missing", "element(doc:item, xs:missing)", "map(xs:missing, item())"):
        with pytest.raises(XsltStaticError) as caught:
            compile_sequence_type(source, context)
        assert _code(caught.value) == "XPST0051"

    custom = _context(tmp_path).with_schema(_schema(tmp_path))
    assert str(compile_sequence_type("doc:Payload", custom)) == "doc:Payload"
    with pytest.raises(XsltStaticError) as caught:
        compile_sequence_type("doc:Payload", context)
    assert _code(caught.value) == "XPST0051"
    with pytest.raises(XsltStaticError) as caught:
        compile_sequence_type("xs:missing", custom)
    assert _code(caught.value) == "XPST0051"


def test_sequence_type_helpers_and_values() -> None:
    item = CompiledSequenceType(item_type=None)
    assert item.item_type is None
    assert str(item) == "empty-sequence()"
    assert item.occurrence == ""
    with pytest.raises(ValueError):
        CompiledSequenceType(occurrence="+")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CompiledSequenceType(None, occurrence="*")

    text = TemplatePart(kind="text", text="literal")
    assert text.expression is None
    compiled = compile_xpath("1 + 1", StaticContext())
    with pytest.raises(ValueError):
        TemplatePart(kind="expression", text="1 + 1")
    with pytest.raises(ValueError):
        TemplatePart(kind="text", text="literal", expression=compiled)
    with pytest.raises(ValueError):
        TemplatePart(kind="unknown", text="")  # type: ignore[arg-type]
    expression = TemplatePart(kind="expression", text="1 + 1", expression=compiled)
    assert expression.expression is compiled
    with pytest.raises(TypeError):
        CompiledValueTemplate(source="x", parts=("x",))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CompiledValueTemplate(source="x", parts=(), expand_text="yes")  # type: ignore[arg-type]
    with pytest.raises(FrozenInstanceError):
        expression.text = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# XPath compilation and parser isolation


def test_compile_xpath_uses_context_version_and_wraps_errors(tmp_path: Path) -> None:
    context = _context(tmp_path)
    compiled = compile_xpath("1 + 1", context)
    assert isinstance(compiled, CompiledExpression)
    assert compiled.source == "1 + 1"
    assert compiled.version == "3.1"
    assert compiled.static_context.default_namespace is None
    result = XPathEngine("3.1").evaluate(compiled, ExpressionEnvironment())
    assert tuple(value.value for value in result) == (2,)

    legacy = context.with_version("2.0")
    assert compile_xpath("1 + 1", legacy).version == "2.0"
    with pytest.raises(XsltStaticError) as caught:
        compile_xpath("map{1: 2}", legacy)
    assert _code(caught.value) == "XPST0003"

    for source, code in (
        ("1 +", "XPST0003"),
        ("unknown:item", "XPST0081"),
        ("", "XPST0003"),
        ("   ", "XPST0003"),
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_xpath(source, context)
        assert _code(caught.value) == code
        assert caught.value.uri == context.uri
        assert caught.value.location is not None

    location = context.location
    overridden = context.with_uri("urn:other")
    with pytest.raises(XsltStaticError) as caught:
        compile_xpath("1 +", overridden, location=location)
    assert caught.value.uri == "urn:other"
    assert caught.value.location == location

    with pytest.raises(TypeError):
        compile_xpath("1", "context")  # type: ignore[arg-type]


def test_context_isolated_namespaces_and_symbols(tmp_path: Path) -> None:
    first = _context(tmp_path)
    second = _context(tmp_path, doc_namespace=OTHER_NS)

    first_avt = compile_avt("{doc:item}", first)
    second_avt = compile_avt("{doc:item}", second)
    assert first_avt.parts[0].expression is not None
    assert second_avt.parts[0].expression is not None
    assert first_avt.parts[0].expression.static_context.namespaces["doc"] == DOC_NS
    assert second_avt.parts[0].expression.static_context.namespaces["doc"] == OTHER_NS
    assert str(compile_sequence_type("element(doc:item)", first)) == "element(doc:item)"
    assert str(compile_sequence_type("element(doc:item)", second)) == "element(doc:item)"
    assert "namespaces" not in XPath31Parser.__dict__

    declared = _context(
        tmp_path,
        '<xsl:variable name="price" select="1" as="xs:integer"/>'
        '<xsl:function name="doc:plus" as="xs:integer">'
        '<xsl:param name="value" as="xs:integer"/>'
        '<xsl:sequence select="$value + 1"/>'
        "</xsl:function>",
    )
    template = compile_avt("{$price + doc:plus(1)}", declared)
    expression = template.parts[0].expression
    assert expression is not None
    assert expression.static_context.functions[f"{{{DOC_NS}}}plus"] == 1
    assert expression.static_context.variable_types == {"price": "xs:integer"}


# ---------------------------------------------------------------------------
# Grammar, type-validation, and construction edges


def test_sequence_type_tokenizer_edges(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assert str(compile_sequence_type("item() (: top :)", context)) == "item()"
    assert str(compile_sequence_type("item() (: a (: b :) c :)", context)) == "item()"
    assert str(compile_sequence_type("(xs:integer)", context)) == "xs:integer"
    assert str(compile_sequence_type("((item()))*", context)) == "item()*"
    assert str(compile_sequence_type("element(Q{}item)", context)) == "element(Q{}item)"

    for source in (
        "item() (: unclosed",
        "item()#",
        "processing-instruction('foo)",
        "Q{urn:x",
        "Q{urn:x}",
        "processing-instruction('fo''o')",
        "processing-instruction(a:b)",
        "processing-instruction('a b')",
        "(empty-sequence())",
        "map((item()), xs:string)",
        "element(,)",
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_sequence_type(source, context)
        assert _code(caught.value) == "XPST0003"

    xsd_default = _context(tmp_path, attributes=f' xpath-default-namespace="{XSD_NS}"')
    assert str(compile_sequence_type("integer", xsd_default)) == "integer"


def test_sequence_type_schema_special_types(tmp_path: Path) -> None:
    context = _context(tmp_path).with_schema(_schema(tmp_path))
    for source in (
        "element(doc:item, xs:untyped)",
        "element(doc:item, xs:untypedAtomic)",
        "element(doc:item, doc:Container)",
        "element(doc:item, xs:anySimpleType)",
        "element(doc:item, xs:anySimpleType)?",
        "attribute(doc:code, xs:untypedAtomic)",
        "attribute(doc:code, xs:anySimpleType)",
        "xs:untypedAtomic",
    ):
        assert str(compile_sequence_type(source, context)) == source

    for source in (
        "xs:untyped",
        "attribute(doc:code, xs:untyped)",
        "doc:Container",
        "map(doc:Container, xs:string)",
        "attribute(doc:code, doc:Container)",
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_sequence_type(source, context)
        assert _code(caught.value) == "XPST0051"


def test_compiled_object_validation_edges() -> None:
    with pytest.raises(ValueError):
        ItemType("other")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ItemType("atomic")
    with pytest.raises(ValueError):
        ItemType("schema-element")
    with pytest.raises(ValueError):
        ItemType("document-node", wildcard=True)
    with pytest.raises(ValueError):
        ItemType("function")
    with pytest.raises(ValueError):
        ItemType("map")
    with pytest.raises(ValueError):
        ItemType("array")
    with pytest.raises(ValueError):
        CompiledSequenceType(occurrence="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CompiledSequenceType(item_type="item")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CompiledSequenceType(source=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TemplatePart(kind="text", text=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CompiledValueTemplate(source=1, parts=())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CompiledValueTemplate(source="x", parts=("x",))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CompiledValueTemplate(source="x", parts=(), text_value_template="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CompiledValueTemplate(source="x", parts=()).with_expand_text("yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compile_sequence_type("item()", "context")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compile_sequence_type(1, StaticContext())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compile_xpath(1, StaticContext())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compile_avt(1, StaticContext())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compile_tvt(1, StaticContext())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        compile_tvt("x", StaticContext(), expand_text="yes")  # type: ignore[arg-type]
    assert compile_tvt("", StaticContext()).parts == ()


def test_avt_nested_comments_and_doubled_quotes(tmp_path: Path) -> None:
    context = _context(tmp_path)
    nested = compile_avt("{1 + (: a (: b :) c :) 2}", context)
    assert nested.parts[0].text == "1 + (: a (: b :) c :) 2"
    quoted = compile_avt("{'it''s'}", context)
    assert quoted.parts[0].text == "'it''s'"
    value = XPathEngine(context.xpath_version).evaluate(
        quoted.parts[0].expression, ExpressionEnvironment()
    )
    assert value[0].value == "it's"


def test_compile_xpath_wraps_code_less_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(self: object, source: str, static_context: object = None) -> None:
        del self, source, static_context
        raise ValueError("[err:XPST0003] synthetic parser failure")

    monkeypatch.setattr("pyxsd.xslt.compiler.expressions.XPathEngine.compile", fail)
    with pytest.raises(XsltStaticError) as caught:
        compile_xpath("1", StaticContext())
    assert _code(caught.value) == "XPST0003"
    assert isinstance(caught.value.cause, ValueError)
