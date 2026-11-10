"""Public processor invocation, compilation, and delivery behavior.

The processor compiles ordinary, simplified, and package stylesheets through
the normative module/package front end.  Each transformation creates fresh
invocation state; the compiled artifact and processor never change.
"""

from __future__ import annotations

import io
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from pyxsd.document import Document
from pyxsd.schema import Schema
from pyxsd.xdm import XdmAtomic, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xslt import (
    CompiledStylesheet,
    ResultShapeError,
    XsltProcessor,
)
from pyxsd.xslt.compiler.packages import PackageResolver
from pyxsd.xslt.errors import XsltError, XsltStaticError
from pyxsd.xslt.processor import XsltInvocationError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"


def xsl(
    body: str,
    *,
    version: str = "3.0",
    output: str = "",
    attributes: str = "",
) -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="{version}" {attributes}>\n'
        f"{output}\n{body}\n</xsl:stylesheet>"
    ).encode()


def compile_bytes(
    source: bytes,
    *,
    processor: XsltProcessor | None = None,
    static_parameters: dict[str, Any] | None = None,
    schema: Any = None,
) -> CompiledStylesheet:
    return (processor or XsltProcessor()).compile(
        io.BytesIO(source),
        static_parameters=static_parameters,
        schema=schema,
    )


def _element_children(node: XdmNode) -> tuple[XdmNode, ...]:
    return tuple(child for child in node.children if child.kind is NodeKind.ELEMENT)


def _text(node: XdmNode) -> str:
    return "".join(
        child.string_value for child in node.children if child.kind is NodeKind.TEXT
    ).strip()


def _xml(node: XdmNode) -> str:
    from xml.etree import ElementTree as ET

    def build(source: XdmNode) -> Any:
        element = ET.Element(source.name or source.local_name or "node")
        for attribute in source.attributes:
            element.set(attribute.name, attribute.value)
        for child in source.children:
            if child.kind is NodeKind.TEXT:
                if len(element):
                    element[-1].tail = (element[-1].tail or "") + child.string_value
                else:
                    element.text = (element.text or "") + child.string_value
            elif child.kind is NodeKind.ELEMENT:
                element.append(build(child))
            elif child.kind is NodeKind.COMMENT:
                element.append(ET.Comment(child.string_value))
            elif child.kind is NodeKind.PROCESSING_INSTRUCTION:
                element.append(ET.ProcessingInstruction(child.target or "", child.string_value))
        return element

    if len(node.children) == 1 and node.children[0].kind is NodeKind.ELEMENT:
        return ET.tostring(build(node.children[0]), encoding="unicode")
    return "".join(child.string_value for child in node.children)


def test_processor_defaults_and_immutable_configuration() -> None:
    processor = XsltProcessor()

    assert processor.language == "3.0"
    assert processor.xpath == "3.1"
    assert processor.schema_aware is True
    assert processor.default_collation is None
    assert processor.static_options == {}
    with pytest.raises(FrozenInstanceError):
        processor.language = "2.0"  # type: ignore[misc]

    with pytest.raises(ValueError, match="unsupported XSLT language"):
        XsltProcessor(language="2.0")
    with pytest.raises(ValueError, match=r"requires XPath 3\.1"):
        XsltProcessor(xpath="3.0")
    with pytest.raises(TypeError, match="schema_aware"):
        XsltProcessor(schema_aware="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="default_collation"):
        XsltProcessor(default_collation=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="static options"):
        XsltProcessor(static_options=[])  # type: ignore[arg-type]


def test_processor_rejects_conflicting_xml_policy_configuration() -> None:
    from pyxsd.resource_policy import ResourcePolicyError
    from pyxsd.xml_policy import XmlParserPolicy

    with pytest.raises(ResourcePolicyError, match="mutually exclusive"):
        XsltProcessor(resource_policy=object(), xml_policy=XmlParserPolicy.default())  # type: ignore[arg-type]


def test_compile_reports_static_expression_and_pattern_errors() -> None:
    bad_expression = xsl('<xsl:template name="main"><xsl:value-of select="1 +"/></xsl:template>')
    with pytest.raises(XsltStaticError) as error:
        compile_bytes(bad_expression)
    assert error.value.code == "XPST0003"

    bad_pattern = xsl('<xsl:template match="map(*)"><literal/></xsl:template>')
    with pytest.raises(XsltStaticError) as error:
        compile_bytes(bad_pattern)
    assert error.value.code == "XTSE0340"


def test_source_less_initial_template_and_raw_delivery() -> None:
    compiled = compile_bytes(
        xsl('<xsl:template name="xsl:initial-template"><xsl:sequence select="1,2"/></xsl:template>')
    )

    result = compiled.transform(delivery="raw")

    assert result.principal == (XdmAtomic.integer(1), XdmAtomic.integer(2))
    with pytest.raises(ResultShapeError, match="not a document tree"):
        _ = result.principal_tree

    document = compiled.transform(delivery="document").principal_tree
    assert isinstance(document, XdmDocument)
    assert document.children[0].string_value == "1 2"


def test_named_initial_template_default_and_reuse() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:param name="value" select="\'default\'"/>'
            '<xsl:template name="xsl:initial-template"><xsl:value-of select="$value"/></xsl:template>'
        )
    )

    first = compiled.transform(parameters={"value": "one"}, delivery="document")
    second = compiled.transform(parameters={"value": "two"}, delivery="document")

    assert _text(first.principal_tree) == "one"
    assert _text(second.principal_tree) == "two"
    assert first is not second


def test_empty_source_differs_from_absent_source() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="xsl:initial-template"><literal/></xsl:template>'
            '<xsl:template match="/"><matched/></xsl:template>'
        )
    )

    absent = compiled.transform(delivery="document")
    empty = compiled.transform(source=(), delivery="document")

    assert _element_children(absent.principal_tree)[0].local_name == "literal"
    assert empty.principal == ()
    assert empty.principal_tree.children == ()


def test_source_global_context_and_initial_selection_conflicts() -> None:
    compiled = compile_bytes(
        xsl('<xsl:template name="xsl:initial-template"><literal/></xsl:template>')
    )

    with pytest.raises(XsltInvocationError, match="source and global_context_item"):
        compiled.transform(source=b"<doc/>", global_context_item=XdmAtomic.integer(1))
    with pytest.raises(XsltInvocationError, match="source and initial_match_selection"):
        compiled.transform(source=b"<doc/>", initial_match_selection=XdmAtomic.integer(1))
    with pytest.raises(TypeError, match="None is not an XDM value"):
        compiled.transform(source=None)
    with pytest.raises(XsltInvocationError, match="global_context_item"):
        compiled.transform(global_context_item=(XdmAtomic.integer(1), XdmAtomic.integer(2)))
    with pytest.raises(TypeError, match="None is not an XDM value"):
        compiled.transform(global_context_item=None)


def test_global_context_and_initial_selection_are_independent() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:param name="context" select="string(.)"/>'
            '<xsl:template name="main"><xsl:sequence select="string($context)"/></xsl:template>'
            '<xsl:template match="doc"><xsl:sequence select="string($context)"/></xsl:template>'
        )
    )

    template_result = compiled.transform(
        initial_template="main",
        global_context_item=XdmAtomic.integer(7),
        delivery="raw",
    )
    assert template_result.principal == (XdmAtomic.string("7"),)

    selection_result = compiled.transform(
        global_context_item=XdmAtomic.integer(8),
        initial_match_selection=(XdmDocument.parse(b"<doc/>"),),
        delivery="document",
    )
    assert _text(selection_result.principal_tree) == "8"


def test_initial_function_arguments_and_raw_sequence() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:function name="my:f" as="item()*" visibility="public">'
            '<xsl:param name="a"/><xsl:param name="b"/>'
            '<xsl:sequence select="$a,$b"/>'
            "</xsl:function>"
        )
    )

    result = compiled.transform(
        initial_function="my:f",
        initial_function_arguments=(1, "two"),
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.integer(1), XdmAtomic.string("two"))


def test_initial_function_arguments_require_an_initial_function() -> None:
    compiled = compile_bytes(
        xsl('<xsl:template name="xsl:initial-template"><literal/></xsl:template>')
    )

    with pytest.raises(ValueError, match="initial_function_arguments"):
        compiled.transform(initial_function_arguments=(1,))


def test_missing_entry_points_raise_exact_codes() -> None:
    compiled = compile_bytes(
        xsl('<xsl:template name="xsl:initial-template"><literal/></xsl:template>')
    )

    with pytest.raises(XsltError) as error:
        compiled.transform(initial_template="missing")
    assert error.value.code == "XTDE0040"

    with pytest.raises(XsltError) as error:
        compiled.transform(initial_function="my:missing", initial_function_arguments=())
    assert error.value.code == "XTDE0041"


def test_entry_point_conflicts_are_rejected() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="xsl:initial-template"><literal/></xsl:template>'
            '<xsl:template match="doc" name="t" mode="m"><literal/></xsl:template>'
            '<xsl:function name="my:f" visibility="public"><xsl:sequence select="1"/></xsl:function>'
        )
    )

    with pytest.raises(XsltInvocationError, match="initial_template and initial_function"):
        compiled.transform(initial_template="t", initial_function="my:f")
    with pytest.raises(XsltInvocationError, match="initial_template and initial_mode"):
        compiled.transform(initial_template="t", initial_mode="m")
    with pytest.raises(XsltInvocationError, match="initial_function and initial_mode"):
        compiled.transform(initial_function="my:f", initial_mode="m")
    with pytest.raises(XsltInvocationError, match="template_parameters"):
        compiled.transform(
            initial_function="my:f",
            template_parameters={"x": 1},
        )
    with pytest.raises(XsltError) as error:
        compiled.transform(initial_function="my:f", initial_function_arguments=(1, 2))
    assert error.value.code == "XTDE0041"


def test_initial_mode_dispatch_and_validation() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template match="doc" mode="m"><matched/></xsl:template>'
            '<xsl:template match="doc"><default-match/></xsl:template>'
        )
    )

    result = compiled.transform(source=b"<doc/>", initial_mode="m", delivery="document")
    assert _element_children(result.principal_tree)[0].local_name == "matched"

    with pytest.raises(XsltError) as error:
        compiled.transform(source=b"<doc/>", initial_mode="missing")
    assert error.value.code == "XTDE0045"

    with pytest.raises(XsltInvocationError) as error:
        compiled.transform(initial_mode="m")
    assert error.value.code == "XTDE0044"


def test_default_mode_is_honoured_for_source_invocation() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template match="doc"><default-match/></xsl:template>'
            '<xsl:template match="doc" mode="chosen"><chosen/></xsl:template>',
            attributes='default-mode="chosen"',
        )
    )

    result = compiled.transform(source=b"<doc/>", delivery="document")

    assert _element_children(result.principal_tree)[0].local_name == "chosen"


def test_simplified_stylesheet_compiles_and_transforms() -> None:
    simplified = (
        f'<html xmlns:xsl="{XSLT_NS}" xsl:version="3.0"><body>Hello</body></html>'
    ).encode()

    compiled = compile_bytes(simplified)
    result = compiled.transform(source=b"<doc/>", delivery="document")

    html = _element_children(result.principal_tree)[0]
    assert html.local_name == "html"
    body = _element_children(html)[0]
    assert body.local_name == "body"
    assert _text(body) == "Hello"


def test_builtin_template_rules_traverse_documents() -> None:
    compiled = compile_bytes(xsl('<xsl:template match="other"><unused/></xsl:template>'))

    result = compiled.transform(source=b"<doc>text<child>more</child></doc>", delivery="document")

    assert _text(result.principal_tree) == "textmore"


def test_template_and_tunnel_parameters() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:param name="normal" required="yes"/>'
            '<xsl:param name="my:tunnel" tunnel="yes"/>'
            '<out><xsl:value-of select="$normal"/><xsl:text>|</xsl:text>'
            '<xsl:value-of select="$my:tunnel"/></out>'
            "</xsl:template>"
        )
    )

    result = compiled.transform(
        initial_template="main",
        template_parameters={"normal": "n"},
        tunnel_parameters={"my:tunnel": "t"},
        delivery="document",
    )

    assert _element_children(result.principal_tree)[0].string_value == "n|t"

    with pytest.raises(XsltError) as error:
        compiled.transform(initial_template="main")
    assert error.value.code == "XTDE0700"


def test_global_variables_and_literal_result_construction() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:param name="base" select="12"/>'
            '<xsl:variable name="twice" select="$base * 2"/>'
            '<xsl:variable name="items" as="element()*">'
            "<item>one</item><item>two</item>"
            "</xsl:variable>"
            '<xsl:template name="main">'
            '<out><xsl:value-of select="$twice"/></out>'
            '<xsl:sequence select="$items"/>'
            "</xsl:template>"
        )
    )

    result = compiled.transform(initial_template="main", delivery="document")
    children = _element_children(result.principal_tree)

    assert children[0].string_value == "24"
    assert [child.local_name for child in children[1:]] == ["item", "item"]


def test_global_parameter_supplied_required_and_ignored() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:param name="required" required="yes"/>'
            '<xsl:template name="main"><xsl:sequence select="$required"/></xsl:template>'
        )
    )

    assert compiled.transform(
        initial_template="main",
        parameters={"required": "supplied", "unknown": "ignored"},
        delivery="raw",
    ).principal == (XdmAtomic.string("supplied"),)

    with pytest.raises(XsltError) as error:
        compiled.transform(initial_template="main")
    assert error.value.code == "XTDE0050"


def test_static_global_parameter_uses_preprocessed_value() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:param name="value" static="yes" select="\'default\'"/>'
            '<xsl:template name="main"><xsl:sequence select="$value"/></xsl:template>'
        ),
        static_parameters={"value": "static-value"},
    )

    assert compiled.transform(initial_template="main", delivery="raw").principal == (
        XdmAtomic.string("static-value"),
    )


def test_output_build_tree_defaults_and_overrides() -> None:
    text = compile_bytes(
        xsl(
            '<xsl:template name="main"><xsl:value-of select="\'text\'"/></xsl:template>',
            output='<xsl:output method="text"/>',
        )
    )
    assert _text(text.transform(initial_template="main").principal_tree) == "text"

    json_output = compile_bytes(
        xsl(
            '<xsl:template name="main"><xsl:sequence select="map{\'k\':1}"/></xsl:template>',
            output='<xsl:output method="json"/>',
        )
    )
    raw = json_output.transform(initial_template="main")
    assert isinstance(raw.principal[0], XdmMap)
    with pytest.raises(ResultShapeError, match="not a document tree"):
        _ = raw.principal_tree

    no_tree = compile_bytes(
        xsl(
            '<xsl:template name="main"><xsl:sequence select="1,2"/></xsl:template>',
            output='<xsl:output method="xml" build-tree="no"/>',
        )
    )
    assert no_tree.transform(initial_template="main").principal == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
    )
    assert (
        no_tree.transform(initial_template="main", delivery="document")
        .principal_tree.children[0]
        .string_value
        == "1 2"
    )

    forced = compile_bytes(
        xsl(
            '<xsl:template name="main"><xsl:sequence select="1"/></xsl:template>',
            output='<xsl:output method="json" build-tree="yes"/>',
        )
    )
    assert forced.transform(initial_template="main").principal_tree.children[0].string_value == "1"


def test_document_delivery_never_serializes_or_reparses() -> None:
    compiled = compile_bytes(
        xsl('<xsl:template name="main"><xsl:value-of select="\'&lt;child/&gt;\'"/></xsl:template>')
    )

    document = compiled.transform(initial_template="main").principal_tree

    assert document.children[0].kind is NodeKind.TEXT
    assert document.children[0].string_value == "<child/>"
    assert _element_children(document) == ()


def test_result_entry_views_and_output_uri() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main"><xsl:sequence select="1"/></xsl:template>',
            output='<xsl:output method="text"/>',
        )
    )

    result = compiled.transform(initial_template="main", output_uri="http://example.com/out.txt")

    assert result.principal_entry.output_uri == "http://example.com/out.txt"
    assert result.principal_entry.serialization["method"] == "text"
    assert result.principal_entry.serialized is None
    assert result.principal_tree.document_uri == "http://example.com/out.txt"
    assert result.principal_tree.base_uri == "http://example.com/out.txt"


def test_secondary_results_are_in_memory_and_never_written(tmp_path: Path) -> None:
    output = tmp_path / "secondary.xml"
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            f'<xsl:result-document href="{output.as_uri()}" method="adaptive">'
            "<xsl:sequence select=\"map{'k':1}\"/>"
            "</xsl:result-document>"
            '<xsl:sequence select="1,2"/>'
            "</xsl:template>"
        )
    )

    result = compiled.transform(initial_template="main", delivery="raw")

    assert result.principal == (XdmAtomic.integer(1), XdmAtomic.integer(2))
    entry = result.secondary[output.as_uri()]
    assert isinstance(entry.raw[0], XdmMap)
    assert entry.tree is None
    assert not output.exists()


def test_secondary_document_delivery_and_named_format() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:result-document href="secondary.xml" format="f">'
            "<secondary/>"
            "</xsl:result-document>"
            "<principal/>"
            "</xsl:template>",
            output='<xsl:output name="f" method="html" indent="yes"/>',
        )
    )

    result = compiled.transform(
        initial_template="main",
        output_uri="http://example.com/out.xml",
    )

    entry = result.secondary["http://example.com/secondary.xml"]
    assert entry.tree is not None
    assert _element_children(entry.tree)[0].local_name == "secondary"
    assert entry.serialization["method"] == "html"
    assert entry.serialization["indent"] == "yes"


def test_secondary_uri_errors_and_principal_collision() -> None:
    duplicate = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:result-document href="http://example.com/dup"><one/></xsl:result-document>'
            '<xsl:result-document href="http://example.com/dup"><two/></xsl:result-document>'
            "</xsl:template>"
        )
    )
    with pytest.raises(XsltError) as error:
        duplicate.transform(initial_template="main")
    assert error.value.code == "XTDE1490"

    relative = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:result-document href="relative.xml"><one/></xsl:result-document>'
            "</xsl:template>"
        )
    )
    with pytest.raises(XsltError, match="absolute URI"):
        relative.transform(initial_template="main")

    collision = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:result-document href="http://example.com/out.xml"><one/></xsl:result-document>'
            "</xsl:template>"
        )
    )
    with pytest.raises(XsltError) as error:
        collision.transform(
            initial_template="main",
            output_uri="http://example.com/out.xml",
        )
    assert error.value.code == "XTDE1490"


def test_result_document_is_rejected_in_temporary_output_state() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:function name="my:f" visibility="public">'
            '<xsl:result-document href="http://example.com/x"><one/></xsl:result-document>'
            "</xsl:function>"
        )
    )

    with pytest.raises(XsltError) as error:
        compiled.transform(initial_function="my:f", delivery="raw")
    assert error.value.code == "XTDE1480"


def test_unsupported_instruction_is_explicit() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:for-each select="1 to 2"><xsl:sequence select="."/></xsl:for-each>'
            "</xsl:template>"
        )
    )

    with pytest.raises(XsltError, match="not supported by the Task 16 execution slice"):
        compiled.transform(initial_template="main", delivery="raw")


def test_invalid_delivery_and_output_uri_are_rejected() -> None:
    compiled = compile_bytes(xsl('<xsl:template name="main"><literal/></xsl:template>'))

    with pytest.raises(XsltInvocationError, match="delivery"):
        compiled.transform(initial_template="main", delivery="filesystem")
    with pytest.raises(XsltInvocationError, match="output_uri"):
        compiled.transform(initial_template="main", output_uri="relative.xml")
    with pytest.raises(XsltInvocationError, match="output_uri"):
        compiled.transform(initial_template="main", output_uri="")


def test_parameter_values_reject_none_and_bad_mappings() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:param name="p" select="1"/>'
            '<xsl:template name="main"><xsl:value-of select="$p"/></xsl:template>'
        )
    )

    with pytest.raises(TypeError, match="None is not an XDM value"):
        compiled.transform(initial_template="main", parameters={"p": None})
    with pytest.raises(TypeError, match="mapping"):
        compiled.transform(initial_template="main", parameters=[("p", 1)])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="mapping"):
        compiled.transform(initial_template="main", template_parameters=[("p", 1)])  # type: ignore[arg-type]


def test_compile_accepts_schema_pool_and_rejects_schema_without_awareness(
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "doc.xsd"
    schema_path.write_text(
        f'<xs:schema xmlns:xs="{XSD_NS}"><xs:element name="doc" type="xs:string"/></xs:schema>',
        encoding="utf-8",
    )
    schema = Schema.compile(schema_path)
    pool = SchemaPool.from_schema(schema)

    compiled = compile_bytes(
        xsl('<xsl:template name="main"><literal/></xsl:template>'),
        schema=pool,
    )
    assert compiled.schema_pool is pool

    with pytest.raises(ValueError, match="schema_aware"):
        compile_bytes(
            xsl('<xsl:template name="main"><literal/></xsl:template>'),
            processor=XsltProcessor(schema_aware=False),
            schema=pool,
        )


def test_source_accepts_paths_streams_documents_and_nodes(tmp_path: Path) -> None:
    compiled = compile_bytes(
        xsl('<xsl:template match="/"><seen><xsl:value-of select="name(/*)"/></seen></xsl:template>')
    )
    path = tmp_path / "source.xml"
    path.write_text("<doc/>", encoding="utf-8")

    for source in (
        path,
        b"<doc/>",
        io.BytesIO(b"<doc/>"),
        XdmDocument.parse(b"<doc/>"),
    ):
        result = compiled.transform(source=source, delivery="document")
        assert _text(_element_children(result.principal_tree)[0]) == "doc"

    schema_path = tmp_path / "doc.xsd"
    schema_path.write_text(
        f'<xs:schema xmlns:xs="{XSD_NS}"><xs:element name="doc" type="xs:string"/></xs:schema>',
        encoding="utf-8",
    )
    document = Schema.compile(schema_path).parse(b"<doc/>", capture_xdm=True)
    assert isinstance(document, Document)
    assert (
        _text(
            _element_children(
                compiled.transform(source=document, delivery="document").principal_tree
            )[0]
        )
        == "doc"
    )

    node = XdmDocument.parse(b"<doc/>").root_element
    node_compiled = compile_bytes(
        xsl(
            '<xsl:template match="doc"><seen><xsl:value-of select="name(.)"/></seen></xsl:template>'
        )
    )
    assert (
        _text(
            _element_children(
                node_compiled.transform(source=node, delivery="document").principal_tree
            )[0]
        )
        == "doc"
    )


def test_compiled_stylesheet_is_reusable_from_threads() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:param name="value" select="0"/>'
            '<xsl:template name="main"><xsl:sequence select="$value"/></xsl:template>'
        )
    )
    barrier = threading.Barrier(4)
    results: list[int] = []
    errors: list[BaseException] = []

    def run(value: int) -> None:
        try:
            barrier.wait()
            result = compiled.transform(
                initial_template="main",
                parameters={"value": value},
                delivery="raw",
            )
            results.append(result.principal[0].value)  # type: ignore[union-attr]
        except BaseException as exc:
            barrier.abort()
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(results) == [0, 1, 2, 3]


def test_compile_package_with_resolver(tmp_path: Path) -> None:
    library = tmp_path / "library.xsl"
    library.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:library="urn:library"
              name="urn:library" package-version="1.0" version="3.0">
              <xsl:template name="library:helper" visibility="public"><helper/></xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:library="urn:library"
              name="urn:principal" version="3.0">
              <xsl:use-package name="urn:library" package-version="1.0"/>
              <xsl:template name="xsl:initial-template" visibility="public"><principal/></xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    resolver = PackageResolver.from_sources(library)

    compiled = XsltProcessor(resolver=resolver).compile(principal)
    result = compiled.transform(delivery="document")

    assert compiled.package.used_packages[0].name == "urn:library"
    assert _element_children(result.principal_tree)[0].local_name == "principal"

    registered_principal = PackageResolver.from_sources(library, principal)
    reused = XsltProcessor(resolver=registered_principal).compile(principal)
    assert reused.package.name == "urn:principal"


def test_processor_option_and_compile_type_errors() -> None:
    with pytest.raises(TypeError, match="static option names"):
        XsltProcessor(static_options={1: "x"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="static_parameters"):
        compile_bytes(
            xsl('<xsl:template name="main"><literal/></xsl:template>'), static_parameters=[]
        )  # type: ignore[arg-type]


def test_compile_accepts_an_effective_module() -> None:
    from pyxsd.xslt.compiler.modules import ModuleLoader
    from pyxsd.xslt.compiler.static import StaticPreprocessor

    effective = StaticPreprocessor().apply(
        ModuleLoader().load(io.BytesIO(xsl('<xsl:template name="main"><literal/></xsl:template>')))
    )
    compiled = XsltProcessor().compile(effective)

    result = compiled.transform(initial_template="main", delivery="raw")
    assert len(result.principal) == 1
    assert result.principal[0].local_name == "literal"


def test_compiled_accessors_and_mode_rule_counts() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template match="doc" mode="m"><literal/></xsl:template>'
            '<xsl:template match="doc"><other/></xsl:template>'
            '<xsl:function name="my:f" visibility="public"><xsl:sequence select="1"/></xsl:function>'
        )
    )

    assert compiled.language == "3.0"
    assert compiled.xpath == "3.1"
    assert compiled.name is None
    assert compiled.version == "1"
    assert compiled.mode_names == ("m", None)
    assert compiled.mode_rule_count() == 1
    assert compiled.mode_rule_count("#unnamed") == 1
    assert compiled.mode_rule_count("m") == 1
    assert compiled.function_available("my:f", 0)
    assert not compiled.function_available("my:f", 1)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            '<xsl:template match="doc" select="1"><literal/></xsl:template>',
            "XTSE0010",
        ),
        (
            '<xsl:variable name="v" select="1"><content/></xsl:variable>'
            '<xsl:template match="doc"><literal/></xsl:template>',
            "XTSE0620",
        ),
        (
            '<xsl:template match="doc"><xsl:value-of select="1">text</xsl:value-of></xsl:template>',
            "XTSE0870",
        ),
        (
            '<xsl:template match="doc"><xsl:sequence select="1">text</xsl:sequence></xsl:template>',
            "XTSE0010",
        ),
        (
            '<xsl:template match="doc"><xsl:param select="1"/></xsl:template>',
            "XTSE0010",
        ),
        (
            '<xsl:template match="doc"><xsl:param name="p" select="1"><content/></xsl:param></xsl:template>',
            "XTSE0620",
        ),
        (
            '<xsl:template match="doc"><xsl:param name="p" required="yes" select="1"/></xsl:template>',
            "XTSE0010",
        ),
        (
            '<xsl:template match="doc"><xsl:text expand-text="maybe">x</xsl:text></xsl:template>',
            "XTSE0020",
        ),
    ],
)
def test_static_compile_rejects_invalid_constructs(body: str, code: str) -> None:
    with pytest.raises(XsltStaticError) as error:
        compile_bytes(xsl(body))

    assert error.value.code == code


def test_literal_result_avt_and_excluded_namespaces() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<out xmlns:unused="urn:unused" a="{1 + 1}" b="plain">text</out>'
            "</xsl:template>",
            attributes='exclude-result-prefixes="unused"',
        )
    )

    element = _element_children(compiled.transform(initial_template="main").principal_tree)[0]

    assert element.attributes[0].string_value == "2"
    assert element.attributes[1].string_value == "plain"
    assert all(node.prefix != "unused" for node in element.namespace_nodes)


def test_literal_result_exclude_all_and_default_namespaces() -> None:
    all_compiled = compile_bytes(
        xsl(
            '<xsl:template name="main"><out xmlns:unused="urn:unused">text</out></xsl:template>',
            attributes='exclude-result-prefixes="#all"',
        )
    )
    all_element = _element_children(all_compiled.transform(initial_template="main").principal_tree)[
        0
    ]
    assert all(node.prefix != "unused" for node in all_element.namespace_nodes)

    default_compiled = compile_bytes(
        xsl(
            '<xsl:template name="main"><out xmlns="urn:default">text</out></xsl:template>',
            attributes='exclude-result-prefixes="#default"',
        )
    )
    default_element = _element_children(
        default_compiled.transform(initial_template="main").principal_tree
    )[0]
    assert default_element.name == "{urn:default}out"
    assert any(
        node.prefix == "" and node.string_value == "urn:default"
        for node in default_element.namespace_nodes
    )
    assert default_element.in_scope_namespaces[""] == "urn:default"


def test_literal_text_uses_effective_expand_text() -> None:
    compiled = compile_bytes(
        xsl('<xsl:template name="main" expand-text="yes">{1 + 1}</xsl:template>')
    )

    assert _text(compiled.transform(initial_template="main").principal_tree) == "2"


def test_text_value_templates_and_nested_sequences() -> None:
    text = compile_bytes(
        xsl(
            '<xsl:template name="main"><xsl:text expand-text="yes">{1 + 1}</xsl:text></xsl:template>'
        )
    )
    assert _text(text.transform(initial_template="main").principal_tree) == "2"

    no_expand = compile_bytes(
        xsl(
            '<xsl:template name="main"><xsl:text expand-text="no">{1 + 1}</xsl:text></xsl:template>'
        )
    )
    assert _text(no_expand.transform(initial_template="main").principal_tree) == "{1 + 1}"

    nested = compile_bytes(
        xsl(
            '<xsl:template name="main"><xsl:sequence>'
            '<xsl:sequence select="1"/></xsl:sequence></xsl:template>'
        )
    )
    assert nested.transform(initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(1),
    )


def test_unsupported_instruction_with_test_attribute_is_compiled() -> None:
    compiled = compile_bytes(
        xsl('<xsl:template name="main"><xsl:if test="true()"><literal/></xsl:if></xsl:template>')
    )

    with pytest.raises(XsltError, match="not supported"):
        compiled.transform(initial_template="main")


def test_parameter_defaults_and_context_item() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:param name="a"><a/></xsl:param>'
            '<xsl:param name="b" as="element()*"><b/></xsl:param>'
            '<xsl:param name="c" as="item()*"/>'
            '<xsl:param name="d"/>'
            '<xsl:param name="e" select="1"/>'
            '<xsl:param name="context" select="string(.)"/>'
            '<xsl:sequence select="name($a/*), name($b), count($c), string-length($d), $e, $context"/>'
            "</xsl:template>"
        )
    )

    result = compiled.transform(
        initial_template="main",
        global_context_item=XdmAtomic.integer(9),
        delivery="raw",
    )

    assert result.principal == (
        XdmAtomic.string("a"),
        XdmAtomic.string("b"),
        XdmAtomic.integer(0),
        XdmAtomic.integer(0),
        XdmAtomic.integer(1),
        XdmAtomic.string("9"),
    )


def test_parameter_after_body_instruction_is_rejected_at_runtime() -> None:
    compiled = compile_bytes(
        xsl('<xsl:template name="main"><literal/><xsl:param name="p"/></xsl:template>')
    )

    with pytest.raises(XsltError) as error:
        compiled.transform(initial_template="main")
    assert error.value.code == "XTDE0000"


def test_circular_and_cached_global_variables() -> None:
    circular = compile_bytes(
        xsl(
            '<xsl:variable name="a" select="$b"/>'
            '<xsl:variable name="b" select="$a"/>'
            '<xsl:template name="main"><literal/></xsl:template>'
        )
    )
    with pytest.raises(XsltError) as error:
        circular.transform(initial_template="main")
    assert error.value.code == "XTDE0640"

    cached = compile_bytes(
        xsl(
            '<xsl:variable name="a" select="$b"/>'
            '<xsl:variable name="b" select="2"/>'
            '<xsl:template name="main"><xsl:sequence select="$a,$a"/></xsl:template>'
        )
    )
    assert cached.transform(initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(2),
        XdmAtomic.integer(2),
    )


def test_global_variable_content_forms() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:variable name="doc"><doc/></xsl:variable>'
            '<xsl:variable name="seq" as="element()*"><a/><b/></xsl:variable>'
            '<xsl:variable name="empty" as="item()*"/>'
            '<xsl:variable name="text"/>'
            '<xsl:template name="main">'
            '<xsl:sequence select="count($doc/*), count($seq), count($empty), string-length($text)"/>'
            "</xsl:template>"
        )
    )

    assert compiled.transform(initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
        XdmAtomic.integer(0),
        XdmAtomic.integer(0),
    )


def test_static_parameter_runtime_boolean_values() -> None:
    disabled = compile_bytes(
        xsl(
            '<xsl:param name="value" static="no" select="\'x\'"/>'
            '<xsl:template name="main"><xsl:sequence select="$value"/></xsl:template>'
        )
    )
    assert disabled.transform(initial_template="main", delivery="raw").principal == (
        XdmAtomic.string("x"),
    )


def test_mode_rule_parameters_and_builtin_comment_pi() -> None:
    mode = compile_bytes(
        xsl(
            '<xsl:template match="/" mode="m">'
            '<xsl:param name="p" select="\'default\'"/>'
            '<out><xsl:value-of select="$p"/></out>'
            "</xsl:template>"
        )
    )
    given = mode.transform(
        source=b"<doc/>",
        initial_mode="m",
        template_parameters={"p": "given"},
        delivery="document",
    )
    default = mode.transform(source=b"<doc/>", initial_mode="m", delivery="document")

    assert _text(_element_children(given.principal_tree)[0]) == "given"
    assert _text(_element_children(default.principal_tree)[0]) == "default"

    builtin = compile_bytes(xsl('<xsl:template match="other"><literal/></xsl:template>'))
    result = builtin.transform(
        source=b"<doc><!--comment--><?pi?>text</doc>",
        delivery="document",
    )
    assert _text(result.principal_tree) == "text"


def test_initial_mode_can_default_selection_from_global_context() -> None:
    compiled = compile_bytes(
        xsl('<xsl:template match=".[. instance of xs:integer]" mode="m"><matched/></xsl:template>')
    )

    result = compiled.transform(
        initial_mode="m",
        global_context_item=XdmAtomic.integer(1),
        delivery="document",
    )

    assert _element_children(result.principal_tree)[0].local_name == "matched"


def test_missing_initial_template_and_bad_argument_sequence() -> None:
    compiled = compile_bytes(xsl('<xsl:template match="doc"><literal/></xsl:template>'))

    with pytest.raises(XsltError) as error:
        compiled.transform()
    assert error.value.code == "XTDE0040"

    private_initial = compile_bytes(
        xsl(
            '<xsl:template name="xsl:initial-template" visibility="private"><literal/></xsl:template>'
        )
    )
    with pytest.raises(XsltError) as error:
        private_initial.transform()
    assert error.value.code == "XTDE0040"

    with pytest.raises(TypeError, match="sequence of values"):
        compiled.transform(initial_function="my:f", initial_function_arguments="x")
    with pytest.raises(TypeError, match="parameter names"):
        compiled.transform(parameters={1: "x"})  # type: ignore[dict-item]


def test_xml_string_and_single_node_source_forms() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template match="doc"><seen><xsl:value-of select="name(.)"/></seen></xsl:template>'
        )
    )

    text_source = compiled.transform(source="<doc/>", delivery="document")
    node = XdmDocument.parse(b"<doc/>").root_element
    node_source = compiled.transform(source=(node,), delivery="document")

    assert _text(_element_children(text_source.principal_tree)[0]) == "doc"
    assert _text(_element_children(node_source.principal_tree)[0]) == "doc"


def test_unknown_parameter_names_are_ignored() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:param name="p" select="1"/>'
            '<xsl:template name="main"><xsl:sequence select="$p"/></xsl:template>'
        )
    )

    result = compiled.transform(
        initial_template="main",
        parameters={"{urn:x}p": 2, "missing:q": 3},
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.integer(1),)


def test_result_document_serialization_and_method_local_helpers() -> None:
    from pyxsd.xslt.processor import _has_plan_content, _method_local, _TextPlan

    assert _method_local("Q{urn:x}json") == "json"
    assert _method_local("my:json") == "json"
    assert not _has_plan_content(())
    assert not _has_plan_content((_TextPlan(" "),))
    assert _has_plan_content((_TextPlan("x"),))

    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:result-document href="http://example.com/secondary" indent="yes" '
            'build-tree="no" method="text">secondary</xsl:result-document>'
            "</xsl:template>"
        )
    )
    result = compiled.transform(initial_template="main")
    entry = result.secondary["http://example.com/secondary"]

    assert entry.serialization == {
        "indent": "yes",
        "build-tree": "no",
        "method": "text",
    }
    assert entry.tree is None


def test_result_document_error_arms() -> None:
    invalid_build = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:result-document href="http://example.com/x" build-tree="maybe"><x/></xsl:result-document>'
            "</xsl:template>"
        )
    )
    with pytest.raises(XsltError) as error:
        invalid_build.transform(initial_template="main")
    assert error.value.code == "XTDE0030"

    missing_format = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:result-document href="http://example.com/x" format="missing"><x/></xsl:result-document>'
            "</xsl:template>"
        )
    )
    with pytest.raises(XsltError) as error:
        missing_format.transform(initial_template="main")
    assert error.value.code == "XTDE1460"

    empty_href = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            "<xsl:result-document><x/></xsl:result-document>"
            "</xsl:template>"
        )
    )
    with pytest.raises(XsltError) as error:
        empty_href.transform(initial_template="main", output_uri="http://example.com/out")
    assert error.value.code == "XTDE1490"


def test_value_of_node_and_function_item_atomization() -> None:
    node = compile_bytes(xsl('<xsl:template match="doc"><xsl:value-of select="."/></xsl:template>'))
    assert _text(node.transform(source=b"<doc>value</doc>").principal_tree) == "value"

    function_item = compile_bytes(
        xsl('<xsl:template name="main"><xsl:value-of select="map{\'k\':1}"/></xsl:template>')
    )
    with pytest.raises(XsltError) as error:
        function_item.transform(initial_template="main")
    assert error.value.code == "FOTY0013"


def test_global_context_empty_sequence_is_absent() -> None:
    compiled = compile_bytes(xsl('<xsl:template name="main"><literal/></xsl:template>'))

    result = compiled.transform(global_context_item=(), initial_template="main")

    assert _element_children(result.principal_tree)[0].local_name == "literal"


def test_static_options_str_key_and_schema_forms(tmp_path: Path) -> None:
    from pyxsd.xslt.processor import _lexical_name

    assert XsltProcessor(static_options={"option": 1}).static_options["option"] == 1

    schema_path = tmp_path / "doc.xsd"
    schema_path.write_text(
        f'<xs:schema xmlns:xs="{XSD_NS}"><xs:element name="doc" type="xs:string"/></xs:schema>',
        encoding="utf-8",
    )
    schema = Schema.compile(schema_path)
    compiled = compile_bytes(
        xsl('<xsl:template name="main"><literal/></xsl:template>'),
        schema=schema,
    )
    assert isinstance(compiled.schema_pool, SchemaPool)

    with pytest.raises(TypeError, match="compiled Schema"):
        compile_bytes(
            xsl('<xsl:template name="main"><literal/></xsl:template>'),
            schema=object(),
        )

    assert _lexical_name("{urn:none}x", {}) == "{urn:none}x"
    assert _lexical_name("{urn:p}x", {"p": "urn:p"}) == "p:x"


def test_named_template_select_is_rejected() -> None:
    with pytest.raises(XsltStaticError) as error:
        compile_bytes(xsl('<xsl:template name="named" select="1"><literal/></xsl:template>'))

    assert error.value.code == "XTSE0010"


def test_declaration_expand_text_and_literal_metadata_attributes() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main" expand-text="yes">'
            "<xsl:text>{1 + 1}</xsl:text>"
            '<out version="9" x="1"/>'
            "</xsl:template>"
        )
    )

    document = compiled.transform(initial_template="main").principal_tree
    text = document.children[0]
    element = _element_children(document)[0]

    assert text.string_value == "2"
    assert [attribute.name for attribute in element.attributes] == ["x"]


def test_exclude_all_keeps_namespaces_that_are_used() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main"><p:out xmlns:p="urn:p" p:a="1"/></xsl:template>',
            attributes='exclude-result-prefixes="#all"',
        )
    )

    element = _element_children(compiled.transform(initial_template="main").principal_tree)[0]

    assert element.name == "{urn:p}out"
    assert any(node.prefix == "p" for node in element.namespace_nodes)

    explicitly_excluded = compile_bytes(
        xsl(
            '<xsl:template name="main"><p:out xmlns:p="urn:p" p:a="1"/></xsl:template>',
            attributes='exclude-result-prefixes="p"',
        )
    )
    excluded_element = _element_children(
        explicitly_excluded.transform(initial_template="main").principal_tree
    )[0]
    assert excluded_element.name == "{urn:p}out"
    assert excluded_element.prefix == "p"
    assert excluded_element.attributes[0].prefix == "p"
    assert any(node.prefix == "p" for node in excluded_element.namespace_nodes)


def test_package_override_exposes_hidden_original_and_accepted_symbols(tmp_path: Path) -> None:
    library = tmp_path / "library.xsl"
    library.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:library="urn:library"
              name="urn:library" package-version="1.0" version="3.0">
              <xsl:variable name="shared" select="'value'" visibility="public"/>
              <xsl:function name="library:f" visibility="public">
                <xsl:sequence select="1"/>
              </xsl:function>
              <xsl:template name="library:t" visibility="public"><old/></xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:library="urn:library"
              name="urn:principal" version="3.0">
              <xsl:use-package name="urn:library" package-version="1.0">
                <xsl:override>
                  <xsl:template name="library:t"><new/></xsl:template>
                </xsl:override>
              </xsl:use-package>
              <xsl:template name="xsl:initial-template" visibility="public"><principal/></xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    resolver = PackageResolver.from_sources(library)

    compiled = XsltProcessor(resolver=resolver).compile(principal)
    result = compiled.transform(delivery="document")

    assert _element_children(result.principal_tree)[0].local_name == "principal"
    assert compiled.package.variable("shared") is not None
    assert compiled.package.function("library:f", 0) is not None


def test_builtin_template_rule_handles_atomic_match_selection() -> None:
    compiled = compile_bytes(xsl('<xsl:template match="other"><unused/></xsl:template>'))

    result = compiled.transform(
        initial_match_selection=(XdmAtomic.integer(5),),
        delivery="document",
    )

    assert _text(result.principal_tree) == "5"


def test_unknown_global_variable_reference_is_reported() -> None:
    from pyxsd.xpath.continuation import XPathEvaluationError

    compiled = compile_bytes(
        xsl(
            '<xsl:variable name="v" select="$unknown"/>'
            '<xsl:template name="main"><literal/></xsl:template>'
        )
    )

    with pytest.raises(XPathEvaluationError):
        compiled.transform(initial_template="main")


def test_initial_entry_points_must_be_public_or_final(tmp_path: Path) -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="private-template" visibility="private"><literal/></xsl:template>'
            '<xsl:function name="my:private" visibility="private"><xsl:sequence select="1"/></xsl:function>'
        )
    )

    with pytest.raises(XsltError) as error:
        compiled.transform(initial_template="private-template")
    assert error.value.code == "XTDE0040"
    with pytest.raises(XsltError) as error:
        compiled.transform(initial_function="my:private", initial_function_arguments=())
    assert error.value.code == "XTDE0041"

    with pytest.raises(XsltError) as error:
        compiled.transform()
    assert error.value.code == "XTDE0040"

    private_initial = compile_bytes(
        xsl(
            '<xsl:template name="xsl:initial-template" visibility="private"><literal/></xsl:template>'
        )
    )
    with pytest.raises(XsltError) as error:
        private_initial.transform()
    assert error.value.code == "XTDE0040"

    with pytest.raises(XsltInvocationError, match="initial_match_selection"):
        compiled.transform(
            initial_template="private-template",
            initial_match_selection=(),
        )
    with pytest.raises(XsltInvocationError, match="initial_match_selection"):
        compiled.transform(
            initial_function="my:private",
            initial_function_arguments=(),
            initial_match_selection=(),
        )


def test_explicit_document_delivery_rejects_raw_function_items() -> None:
    compiled = compile_bytes(
        xsl('<xsl:template name="main"><xsl:sequence select="map{\'k\':1}"/></xsl:template>')
    )

    with pytest.raises(XsltError) as error:
        compiled.transform(initial_template="main", delivery="document")
    assert error.value.code == "XTDE0450"


def test_processor_default_collation_is_used() -> None:
    compiled = XsltProcessor(
        default_collation="http://www.w3.org/2005/xpath-functions/collation/codepoint"
    ).compile(
        io.BytesIO(
            xsl(
                '<xsl:template name="main">'
                "<xsl:value-of select=\"compare('a', 'b')\"/>"
                "</xsl:template>"
            )
        )
    )

    assert (
        compiled.transform(initial_template="main", delivery="raw").principal[0].string_value
        == "-1"
    )


def test_package_mode_layers_dispatch_in_order(tmp_path: Path) -> None:
    library = tmp_path / "library.xsl"
    library.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:app="urn:app"
              name="urn:library" package-version="1.0" version="3.0">
              <xsl:mode name="app:m" visibility="public"/>
              <xsl:template match="doc" mode="app:m"><from-library/></xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:app="urn:app"
              name="urn:principal" version="3.0">
              <xsl:use-package name="urn:library" package-version="1.0">
                <xsl:accept component="mode" names="app:m" visibility="public"/>
                <xsl:override>
                  <xsl:template match="other" mode="app:m"><from-override/></xsl:template>
                </xsl:override>
              </xsl:use-package>
              <xsl:template name="xsl:initial-template" visibility="public"><done/></xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(principal)

    from_library = compiled.transform(source=b"<doc/>", initial_mode="app:m", delivery="document")
    from_override = compiled.transform(
        source=b"<other/>", initial_mode="app:m", delivery="document"
    )

    assert compiled.mode_rule_count("app:m") == 2
    assert _element_children(from_library.principal_tree)[0].local_name == "from-library"
    assert _element_children(from_override.principal_tree)[0].local_name == "from-override"


def test_private_library_globals_are_primed_in_package_scope(tmp_path: Path) -> None:
    library = tmp_path / "library.xsl"
    library.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:lib="urn:lib" xmlns:app="urn:app"
              name="urn:lib" package-version="1.0" version="3.0">
              <xsl:variable name="lib:private" select="'private'" visibility="private"/>
              <xsl:variable name="lib:public" select="$lib:private" visibility="public"/>
              <xsl:mode name="app:m" visibility="public"/>
              <xsl:template match="doc" mode="app:m">
                <xsl:value-of select="$lib:private"/>
              </xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:lib="urn:lib" xmlns:app="urn:app"
              name="urn:principal" version="3.0">
              <xsl:use-package name="urn:lib" package-version="1.0">
                <xsl:accept component="mode" names="app:m" visibility="public"/>
                <xsl:accept component="variable" names="lib:public" visibility="public"/>
              </xsl:use-package>
              <xsl:template name="xsl:initial-template" visibility="public">
                <xsl:sequence select="$lib:public"/>
              </xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(principal)

    public = compiled.transform(delivery="raw")
    mode = compiled.transform(source=b"<doc/>", initial_mode="app:m", delivery="document")

    assert public.principal == (XdmAtomic.string("private"),)
    assert _text(mode.principal_tree) == "private"


def test_initial_function_arguments_and_results_are_converted() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:function name="my:integer" visibility="public">'
            '<xsl:param name="x" as="xs:integer"/>'
            '<xsl:sequence select="$x instance of xs:integer"/>'
            "</xsl:function>"
            '<xsl:function name="my:return-integer" as="xs:integer" visibility="public">'
            '<xsl:param name="x"/>'
            '<xsl:sequence select="$x"/>'
            "</xsl:function>"
            '<xsl:function name="my:bad-return" as="xs:integer" visibility="public">'
            '<xsl:sequence select="true()"/>'
            "</xsl:function>"
            '<xsl:function name="my:element-only" visibility="public">'
            '<xsl:param name="x" as="element()"/>'
            '<xsl:sequence select="$x"/>'
            "</xsl:function>"
        )
    )

    assert compiled.transform(
        initial_function="my:integer",
        initial_function_arguments=("5",),
        delivery="raw",
    ).principal == (XdmAtomic.boolean(True),)
    assert compiled.transform(
        initial_function="my:integer",
        initial_function_arguments=(XdmAtomic.untyped_atomic("5"),),
        delivery="raw",
    ).principal == (XdmAtomic.boolean(True),)
    assert compiled.transform(
        initial_function="my:return-integer",
        initial_function_arguments=(5,),
        delivery="raw",
    ).principal == (XdmAtomic.integer(5),)

    with pytest.raises(XsltError) as error:
        compiled.transform(
            initial_function="my:bad-return",
            initial_function_arguments=(),
            delivery="raw",
        )
    assert error.value.code == "XTTE0780"

    with pytest.raises(XsltError) as error:
        compiled.transform(
            initial_function="my:element-only",
            initial_function_arguments=("not-an-element",),
            delivery="raw",
        )
    assert error.value.code == "XTTE0790"


def test_template_and_global_parameter_and_variable_conversion() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:param name="global" as="xs:integer"/>'
            '<xsl:variable name="converted" as="xs:integer" select="\'9\'"/>'
            '<xsl:template name="main">'
            '<xsl:param name="local" as="xs:integer"/>'
            '<xsl:sequence select="$global instance of xs:integer,'
            ' $converted instance of xs:integer, $local instance of xs:integer"/>'
            "</xsl:template>"
        )
    )

    result = compiled.transform(
        initial_template="main",
        parameters={"global": "7"},
        template_parameters={"local": "8"},
        delivery="raw",
    )

    assert result.principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
    )

    with pytest.raises(XsltError) as error:
        compiled.transform(
            initial_template="main",
            parameters={"global": "7"},
            template_parameters={"local": "not-an-integer"},
            delivery="raw",
        )
    assert error.value.code == "XTTE0590"


def test_initial_function_arity_mismatch_uses_xtde0041() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:function name="my:f" visibility="public">'
            '<xsl:param name="x"/>'
            '<xsl:sequence select="$x"/>'
            "</xsl:function>"
        )
    )

    with pytest.raises(XsltError) as error:
        compiled.transform(
            initial_function="my:f",
            initial_function_arguments=(1, 2),
            delivery="raw",
        )
    assert error.value.code == "XTDE0041"

    with pytest.raises(XsltError) as error:
        compiled.transform(
            initial_function="my:missing",
            initial_function_arguments=(),
            delivery="raw",
        )
    assert error.value.code == "XTDE0041"


def test_context_item_declaration_is_consumed_and_enforced() -> None:
    optional = compile_bytes(
        xsl('<xsl:template name="main"><xsl:context-item use="optional"/><out/></xsl:template>')
    )
    assert (
        _element_children(optional.transform(initial_template="main").principal_tree)[0].local_name
        == "out"
    )

    required = compile_bytes(
        xsl('<xsl:template name="main"><xsl:context-item use="required"/><out/></xsl:template>')
    )
    with pytest.raises(XsltError) as error:
        required.transform(initial_template="main")
    assert error.value.code == "XTTE3090"
    assert (
        _element_children(
            required.transform(
                initial_template="main",
                global_context_item=XdmAtomic.integer(1),
            ).principal_tree
        )[0].local_name
        == "out"
    )

    with pytest.raises(XsltStaticError) as error:
        compile_bytes(
            xsl(
                '<xsl:template name="main"><xsl:context-item use="absent" as="item()"/>'
                "<out/></xsl:template>"
            )
        )
    assert error.value.code == "XTSE3088"


def test_temporary_output_state_survives_literal_result_elements() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:function name="my:f" visibility="public">'
            '<out><xsl:result-document href="http://example.com/x"><x/></xsl:result-document></out>'
            "</xsl:function>"
        )
    )

    with pytest.raises(XsltError) as error:
        compiled.transform(initial_function="my:f", delivery="raw")
    assert error.value.code == "XTDE1480"


def test_secondary_results_use_the_unnamed_output_definition() -> None:
    text = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:result-document href="http://example.com/s">secondary</xsl:result-document>'
            "</xsl:template>",
            output='<xsl:output method="text" build-tree="no"/>',
        )
    )
    text_entry = text.transform(initial_template="main").secondary["http://example.com/s"]
    assert text_entry.tree is None
    assert text_entry.serialization["method"] == "text"
    assert text_entry.serialization["build-tree"] == "no"

    json_output = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:result-document href="http://example.com/s">'
            "<xsl:sequence select=\"map{'k':1}\"/>"
            "</xsl:result-document>"
            "</xsl:template>",
            output='<xsl:output method="json"/>',
        )
    )
    json_entry = json_output.transform(initial_template="main").secondary["http://example.com/s"]
    assert json_entry.tree is None
    assert isinstance(json_entry.raw[0], XdmMap)

    overridden = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:result-document href="http://example.com/s" method="xml">'
            "<secondary/>"
            "</xsl:result-document>"
            "</xsl:template>",
            output='<xsl:output method="text"/>',
        )
    )
    overridden_entry = overridden.transform(initial_template="main").secondary[
        "http://example.com/s"
    ]
    assert overridden_entry.tree is not None
    assert overridden_entry.serialization["method"] == "xml"


def test_value_of_content_forms_and_simple_content_separators() -> None:
    content = compile_bytes(
        xsl('<xsl:template name="main"><xsl:value-of>abc</xsl:value-of></xsl:template>')
    )
    assert _text(content.transform(initial_template="main").principal_tree) == "abc"

    empty = compile_bytes(xsl('<xsl:template name="main"><xsl:value-of/></xsl:template>'))
    assert empty.transform(initial_template="main").principal_tree.children == ()

    separated = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:value-of separator="-"><xsl:sequence select="1,2"/></xsl:value-of>'
            "</xsl:template>"
        )
    )
    assert _text(separated.transform(initial_template="main").principal_tree) == "1-2"

    content_default = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:value-of><xsl:sequence select="1,2"/></xsl:value-of>'
            "</xsl:template>"
        )
    )
    assert _text(content_default.transform(initial_template="main").principal_tree) == "12"

    adjacent = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            "<xsl:value-of><xsl:text>a</xsl:text><xsl:text>b</xsl:text></xsl:value-of>"
            "</xsl:template>"
        )
    )
    assert _text(adjacent.transform(initial_template="main").principal_tree) == "ab"


def test_context_item_absent_and_default_forms() -> None:
    absent = compile_bytes(
        xsl('<xsl:template name="main"><xsl:context-item use="absent"/><out/></xsl:template>')
    )
    assert (
        _element_children(
            absent.transform(
                initial_template="main",
                global_context_item=XdmAtomic.integer(1),
            ).principal_tree
        )[0].local_name
        == "out"
    )

    default = compile_bytes(
        xsl('<xsl:template name="main"><xsl:context-item/><out/></xsl:template>')
    )
    assert (
        _element_children(default.transform(initial_template="main").principal_tree)[0].local_name
        == "out"
    )


def test_sequence_type_conversion_and_matching_arms(tmp_path: Path) -> None:
    from pyxsd.xdm import XdmArray
    from pyxsd.xslt.compiler.context import StaticContext
    from pyxsd.xslt.compiler.expressions import compile_sequence_type
    from pyxsd.xslt.processor import _coerce_sequence

    schema_path = tmp_path / "types.xsd"
    schema_path.write_text(
        f'<xs:schema xmlns:xs="{XSD_NS}">'
        '<xs:element name="child" type="xs:string"/>'
        '<xs:attribute name="a" type="xs:string"/>'
        "</xs:schema>",
        encoding="utf-8",
    )
    pool = SchemaPool.from_schema(Schema.compile(schema_path))
    context = StaticContext(namespaces={"xs": XSD_NS}, schema=pool)
    source = XdmDocument.parse(b"<root a='1'>text<!--c--><?pi?><child>text</child></root>")
    root = source.root_element
    text = root.children[0]
    comment = root.children[1]
    instruction = root.children[2]
    child = root.children[3]
    attribute = root.attributes[0]
    namespace = XdmDocument.parse(b"<x xmlns:p='urn:p'/>").root_element.namespace_nodes[0]
    mapping = XdmMap.from_mapping({"k": 1})
    array = XdmArray([1])

    def check(type_source: str, item: Any, expected: Any = None) -> None:
        sequence_type = compile_sequence_type(type_source, context)
        result = _coerce_sequence((item,), sequence_type, code="XTTE0790", label="test")
        assert result == ((item,) if expected is None else (expected,))

    check("node()", root)
    check("text()", text)
    check("comment()", comment)
    check("processing-instruction(pi)", instruction)
    check("attribute()", attribute)
    check("element()", child)
    check("schema-element(child)", child)
    check("schema-attribute(a)", attribute)
    check("document-node()", source)
    check("namespace-node()", namespace)
    check("function(*)", mapping)
    check("map(*)", mapping)
    check("array(*)", array)
    check("item()", XdmAtomic.string("x"))
    check("xs:anyAtomicType", XdmAtomic.string("x"))
    check("xs:string", child, XdmAtomic.string("text"))
    check("xs:string", XdmAtomic.string("x"))

    with pytest.raises(XsltError) as error:
        _coerce_sequence(
            (XdmAtomic.integer(1),),
            compile_sequence_type("empty-sequence()", context),
            code="XTTE0790",
            label="test",
        )
    assert error.value.code == "XTTE0790"

    assert (
        _coerce_sequence(
            (),
            compile_sequence_type("empty-sequence()", context),
            code="XTTE0790",
            label="test",
        )
        == ()
    )

    with pytest.raises(XsltError) as error:
        _coerce_sequence(
            (),
            compile_sequence_type("xs:integer+", context),
            code="XTTE0790",
            label="test",
        )
    assert error.value.code == "XTTE0790"

    with pytest.raises(XsltError) as error:
        _coerce_sequence(
            (mapping,),
            compile_sequence_type("xs:string", context),
            code="XTTE0790",
            label="test",
        )
    assert error.value.code == "FOTY0013"


def test_value_of_flushes_pending_text_before_atomics() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template name="main">'
            '<xsl:value-of separator="-"><xsl:text>a</xsl:text>'
            '<xsl:sequence select="1"/></xsl:value-of>'
            "</xsl:template>"
        )
    )

    assert _text(compiled.transform(initial_template="main").principal_tree) == "a-1"


def test_template_parameter_name_validation_and_unknown_prefixes() -> None:
    from pyxsd.xslt.processor import _expanded_name

    compiled = compile_bytes(xsl('<xsl:template name="main"><out/></xsl:template>'))

    with pytest.raises(TypeError, match="parameter names"):
        compiled.transform(
            initial_template="main",
            template_parameters={1: "x"},  # type: ignore[dict-item]
        )
    result = compiled.transform(
        initial_template="main",
        template_parameters={"missing:p": "ignored"},
        delivery="document",
    )
    assert _element_children(result.principal_tree)[0].local_name == "out"

    assert _expanded_name("{urn:x}p", {}) == "{urn:x}p"
    assert _expanded_name("missing:p", {}) == "missing:p"


def test_literal_namespaces_skip_unused_default_even_when_excluded() -> None:
    from pyxsd.xslt.compiler.instructions import Instruction
    from pyxsd.xslt.processor import _literal_namespaces

    instruction = Instruction(
        name="out",
        namespaces={
            "": "urn:unused",
            "p": "urn:p",
            "xml": "http://www.w3.org/XML/1998/namespace",
            "xsl": XSLT_NS,
        },
    )

    assert _literal_namespaces(instruction, (), ("#default",)) == (("p", "urn:p"),)


def test_mode_rule_context_item_constraints() -> None:
    compiled = compile_bytes(
        xsl(
            '<xsl:template match="doc" mode="m">'
            '<xsl:context-item use="required" as="element()"/>'
            "<matched/>"
            "</xsl:template>"
        )
    )
    result = compiled.transform(source=b"<doc/>", initial_mode="m", delivery="document")
    assert _element_children(result.principal_tree)[0].local_name == "matched"

    with pytest.raises(XsltStaticError) as error:
        compile_bytes(
            xsl(
                '<xsl:template match="doc" mode="m">'
                '<xsl:context-item use="invalid"/>'
                "</xsl:template>"
            )
        )
    assert error.value.code == "XTSE0020"

    with pytest.raises(XsltStaticError) as error:
        compile_bytes(
            xsl(
                '<xsl:template match="doc" mode="m">'
                '<xsl:context-item use="absent" as="item()"/>'
                "</xsl:template>"
            )
        )
    assert error.value.code == "XTSE3088"


def test_mode_rule_result_types_are_converted() -> None:
    good = compile_bytes(
        xsl(
            '<xsl:template match="doc" mode="m" as="xs:integer">'
            "<xsl:sequence select=\"'5'\"/>"
            "</xsl:template>"
        )
    )
    assert good.transform(source=b"<doc/>", initial_mode="m", delivery="raw").principal == (
        XdmAtomic.integer(5),
    )

    bad = compile_bytes(
        xsl(
            '<xsl:template match="doc" mode="m" as="xs:integer">'
            '<xsl:sequence select="true()"/>'
            "</xsl:template>"
        )
    )
    with pytest.raises(XsltError) as error:
        bad.transform(source=b"<doc/>", initial_mode="m", delivery="raw")
    assert error.value.code == "XTTE0505"
