"""Behavioral tests for the mandatory constructed-node instruction family.

``xsl:comment``, ``xsl:processing-instruction``, and ``xsl:namespace`` compile
and execute outside the processor: this module drives real compilations and
checks the constructed nodes, their attachment, and every assigned static or
dynamic error code.
"""

from __future__ import annotations

import ast
import inspect
import io
from typing import Any

import pytest
from tests.xslt.support import stylesheet

import pyxsd.xslt.processor as processor_module
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.constructors import (
    CONSTRUCTOR_INSTRUCTION_COMPILERS,
    compile_comment,
    compile_namespace,
    compile_processing_instruction,
)
from pyxsd.xslt.compiler.instructions import instruction_name
from pyxsd.xslt.compiler.plans import (
    _CommentPlan,
    _NamespacePlan,
    _ProcessingInstructionPlan,
)
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.runtime.constructors import (
    CONSTRUCTOR_PLAN_EXECUTORS,
    execute_comment,
    execute_namespace,
    execute_processing_instruction,
)

XML_NS = "http://www.w3.org/XML/1998/namespace"
XMLNS_NS = "http://www.w3.org/2000/xmlns/"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
VALUE_NS = "urn:example:value"


def _compile(body: str, *, version: str = "3.0", attributes: str = "", **kwargs: Any) -> Any:
    return XsltProcessor(**kwargs).compile(
        io.BytesIO(stylesheet(body, version=version, attributes=attributes, declare_xs=False))
    )


def _transform(body: str, *, version: str = "3.0", attributes: str = "", **kwargs: Any) -> Any:
    return _compile(body, version=version, attributes=attributes, **kwargs).transform(
        initial_template="main", delivery="document"
    )


def _error_code(body: str, *, version: str = "3.0", **kwargs: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        _compile(body, version=version, **kwargs).transform(
            initial_template="main", delivery="document"
        )
    return caught.value.code


def _transform_source(
    body: str,
    source: XdmDocument | bytes,
    *,
    version: str = "3.0",
    attributes: str = "",
    **kwargs: Any,
) -> Any:
    return _compile(body, version=version, attributes=attributes, **kwargs).transform(
        source=source,
        initial_template="main",
        delivery="document",
    )


def _comments(node: XdmNode) -> tuple[XdmNode, ...]:
    return tuple(child for child in node.children if child.kind is NodeKind.COMMENT)


def _pis(node: XdmNode) -> tuple[XdmNode, ...]:
    return tuple(child for child in node.children if child.kind is NodeKind.PROCESSING_INSTRUCTION)


# ---------------------------------------------------------------------------
# xsl:comment


def test_comment_value_comes_from_content_select_and_string_conversion() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:comment>content</xsl:comment>
            <xsl:comment select="1 + 1"/>
            <xsl:comment select="(1, 'two')"/>
            <xsl:comment select="()"/>
          </out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert [comment.string_value for comment in _comments(out)] == [
        "content",
        "2",
        "1 two",
        "",
    ]
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?><out><!--content--><!--2--><!--1 two--><!----></out>'
    )


def test_comment_text_nodes_merge_without_a_separator() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:comment>
              <xsl:value-of select="'a'"/>
              <xsl:value-of select="'b'"/>
            </xsl:comment>
          </out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert [comment.string_value for comment in _comments(out)] == ["ab"]


def test_comment_content_is_sanitized_per_the_spec() -> None:
    # XSLT 3.0 11.8 requires a space after -- and after a trailing -.  The
    # specification assigns no dynamic error code to comment content.
    result = _transform(
        """
        <xsl:template name="main">
          <out><xsl:comment>a--b-</xsl:comment></out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert [comment.string_value for comment in _comments(out)] == ["a- -b- "]


# ---------------------------------------------------------------------------
# xsl:processing-instruction


def test_processing_instruction_uses_literal_avt_and_namespaced_names() -> None:
    result = _transform(
        """
        <xsl:template xmlns:xslt="http://www.w3.org/1999/XSL/Transform" name="main">
          <out>
            <xsl:processing-instruction name="pi">data</xsl:processing-instruction>
            <xsl:processing-instruction name="{concat('tar', 'get')}" select="'from select'"/>
            <xslt:processing-instruction name="literal">namespaced</xslt:processing-instruction>
          </out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert [(pi.target, pi.string_value) for pi in _pis(out)] == [
        ("pi", "data"),
        ("target", "from select"),
        ("literal", "namespaced"),
    ]


def test_processing_instruction_strips_leading_spaces_and_sanitizes_terminator() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out><xsl:processing-instruction name="pi">  value?&gt;</xsl:processing-instruction></out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert [pi.string_value for pi in _pis(out)] == ["value? >"]


def test_processing_instruction_select_and_empty_comment_keep_their_values() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:processing-instruction name="pi" select="()"/>
            <xsl:comment select="()"/>
          </out>
        </xsl:template>
        """
    )
    out = result.principal_tree.root_element
    assert [pi.string_value for pi in _pis(out)] == [""]
    assert [comment.string_value for comment in _comments(out)] == [""]
    assert result.serialize() == ('<?xml version="1.0" encoding="UTF-8"?><out><?pi?><!----></out>')


def test_processing_instruction_atomic_values_keep_the_space_separator() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out><xsl:processing-instruction name="pi" select="(1, 2)"/></out>
        </xsl:template>
        """
    )
    out = result.principal_tree.root_element
    assert _pis(out)[0].string_value == "1 2"


def test_processing_instruction_target_from_avt_is_validated() -> None:
    assert (
        _error_code(
            """
            <xsl:template name="main">
              <xsl:processing-instruction name="{concat('x', 'ml')}">x</xsl:processing-instruction>
            </xsl:template>
            """
        )
        == "XTDE0890"
    )


# ---------------------------------------------------------------------------
# xsl:namespace


def test_namespace_binds_prefixes_and_the_default_namespace() -> None:
    result = _transform(
        """
        <xsl:template xmlns:x="urn:x" exclude-result-prefixes="x" name="main">
          <xsl:element name="x:out">
            <xsl:namespace name="p" select="'urn:p'"/>
            <xsl:namespace name="">urn:default</xsl:namespace>
            <xsl:text>value</xsl:text>
          </xsl:element>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    prefix_node = out.namespace_node("p")
    default_node = out.namespace_node("")
    assert prefix_node is not None
    assert prefix_node.namespace_binding == "urn:p"
    assert default_node is not None
    assert default_node.namespace_binding == "urn:default"
    serialized = result.serialize()
    assert 'xmlns:p="urn:p"' in serialized
    assert 'xmlns="urn:default"' in serialized


def test_namespace_node_precedes_comment_and_element_content() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:namespace name="p" select="'urn:p'"/>
            <xsl:comment>before</xsl:comment>
            <child/>
          </out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert [child.kind for child in out.children] == [
        NodeKind.COMMENT,
        NodeKind.ELEMENT,
    ]
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?><out xmlns:p="urn:p"><!--before--><child/></out>'
    )


def test_namespace_after_other_content_uses_the_attachment_error() -> None:
    assert (
        _error_code(
            """
            <xsl:template name="main">
              <out>
                <xsl:comment>first</xsl:comment>
                <xsl:namespace name="p" select="'urn:p'"/>
              </out>
            </xsl:template>
            """
        )
        == "XTDE0410"
    )


def test_namespace_expands_an_avt_name_and_an_atomic_value_sequence() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:namespace name="{concat('n', 's')}">urn:<xsl:value-of select="'p'"/></xsl:namespace>
            <xsl:text>x</xsl:text>
          </out>
        </xsl:template>
        """
    )
    out = result.principal_tree.root_element
    node = out.namespace_node("ns")
    assert node is not None
    assert node.namespace_binding == "urn:p"


def test_namespace_with_empty_prefix_requires_a_namespaced_parent() -> None:
    assert (
        _error_code(
            """
            <xsl:template name="main">
              <out><xsl:namespace name="" select="'urn:default'"/></out>
            </xsl:template>
            """
        )
        == "XTDE0440"
    )


# ---------------------------------------------------------------------------
# Document order, nesting, and sequence constructors


def test_constructed_nodes_keep_document_order_around_elements() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:comment>before</xsl:comment>
            <xsl:processing-instruction name="pi">middle</xsl:processing-instruction>
            <child/>
            <xsl:comment>after</xsl:comment>
          </out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert [child.kind for child in out.children] == [
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
        NodeKind.ELEMENT,
        NodeKind.COMMENT,
    ]
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<out><!--before--><?pi middle?><child/><!--after--></out>"
    )


def test_constructors_work_inside_a_sequence_constructor() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <xsl:sequence>
            <xsl:comment select="'from sequence'"/>
          </xsl:sequence>
        </xsl:template>
        """
    )

    tree = result.principal_tree
    assert [child.kind for child in tree.children] == [NodeKind.COMMENT]
    assert tree.children[0].string_value == "from sequence"


def test_constructors_nest_inside_xsl_element_and_literal_result_elements() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <xsl:element name="outer">
            <xsl:comment select="'outer comment'"/>
            <literal>
              <xsl:processing-instruction name="inner">nested</xsl:processing-instruction>
            </literal>
          </xsl:element>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert out.children[0].kind is NodeKind.COMMENT
    literal = out.children[1]
    assert literal.kind is NodeKind.ELEMENT
    assert [pi.target for pi in _pis(literal)] == ["inner"]


def test_literal_result_element_keeps_its_lexical_prefix() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out xmlns="http://abc.uri/">
            <p:item xmlns:p="http://abc.uri/"/>
          </out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    item = out.children[0]
    assert out.prefix == ""
    assert out.namespace_uri == "http://abc.uri/"
    assert item.prefix == "p"
    assert item.namespace_uri == "http://abc.uri/"
    assert dict(item.in_scope_namespaces)["p"] == "http://abc.uri/"
    assert ("p", "http://abc.uri/") in item.namespace_declarations
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<out xmlns="http://abc.uri/"><p:item xmlns:p="http://abc.uri/"/></out>'
    )


def test_xsl_element_uses_the_default_prefix_for_an_unprefixed_name() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out xmlns="urn:outer">
            <xsl:element name="item" namespace="urn:item"/>
          </out>
        </xsl:template>
        """
    )

    item = result.principal_tree.root_element.children[0]
    assert item.prefix == ""
    assert item.namespace_uri == "urn:item"
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<out xmlns="urn:outer"><item xmlns="urn:item"/></out>'
    )


def test_xsl_element_rebinds_the_requested_lexical_prefix() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out xmlns:p="urn:outer">
            <xsl:element name="p:item" namespace="urn:item"/>
          </out>
        </xsl:template>
        """
    )

    item = result.principal_tree.root_element.children[0]
    assert item.prefix == "p"
    assert item.namespace_uri == "urn:item"
    assert dict(item.in_scope_namespaces)["p"] == "urn:item"
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<out xmlns:p="urn:outer"><p:item xmlns:p="urn:item"/></out>'
    )


def test_xsl_element_does_not_copy_stylesheet_namespaces() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:element name="item" xmlns:unused="urn:unused"/>
          </out>
        </xsl:template>
        """
    )

    item = result.principal_tree.root_element.children[0]
    assert "unused" not in item.in_scope_namespaces
    assert "xsl" not in item.in_scope_namespaces
    assert result.serialize() == ('<?xml version="1.0" encoding="UTF-8"?><out><item/></out>')


def test_literal_result_element_keeps_unprefixed_standard_attribute_names() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out version="1.0" default-collation="urn:c"><xsl:element name="in"/></out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert [attribute.name for attribute in out.attributes] == [
        "version",
        "default-collation",
    ]
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<out version="1.0" default-collation="urn:c"><in/></out>'
    )


def test_literal_result_element_drops_xslt_standard_attributes() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out xsl:version="1.0" xsl:expand-text="no"/>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert out.attributes == ()
    assert result.serialize() == '<?xml version="1.0" encoding="UTF-8"?><out/>'


def test_literal_result_prefix_is_visible_to_name() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <xsl:variable name="v">
            <p:item xmlns:p="urn:a" xmlns="urn:a"/>
          </xsl:variable>
          <out><xsl:value-of select="name($v/*)"/></out>
        </xsl:template>
        """
    )

    assert result.principal_tree.root_element.string_value == "p:item"


def test_literal_nested_default_rebinding_and_undeclaration() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <a xmlns="urn:1"><b xmlns="urn:2"><c xmlns=""/></b></a>
        </xsl:template>
        """
    )

    a = result.principal_tree.root_element
    b = a.children[0]
    c = b.children[0]
    assert [(node.prefix, node.namespace_uri) for node in (a, b, c)] == [
        ("", "urn:1"),
        ("", "urn:2"),
        ("", None),
    ]
    assert a.namespace_declarations == (("", "urn:1"),)
    assert b.namespace_declarations == (("", "urn:2"),)
    assert c.namespace_declarations == ()
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<a xmlns="urn:1"><b xmlns="urn:2"><c xmlns=""/></b></a>'
    )


# ---------------------------------------------------------------------------
# Namespace copying, exclusions, and aliasing


def _declarations(node: XdmNode) -> dict[str, str]:
    return {
        node.prefix: node.namespace_binding for node in node.namespace_nodes if node.prefix != "xml"
    }


def test_literal_local_exclude_all_applies_to_the_bearing_subtree() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out xmlns:p="urn:p" xmlns:q="urn:q" xsl:exclude-result-prefixes="#all">
            <child xmlns:r="urn:r"/>
          </out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    child = out.children[0]
    assert _declarations(out) == {}
    assert _declarations(child) == {"r": "urn:r"}
    assert "p" not in child.in_scope_namespaces
    assert "q" not in child.in_scope_namespaces
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?><out><child xmlns:r="urn:r"/></out>'
    )


def test_literal_local_exclusion_of_default_and_a_shared_uri() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out xmlns="urn:d" xmlns:a="urn:a" xmlns:a2="urn:a"
               xsl:exclude-result-prefixes="#default a"/>
        </xsl:template>
        """,
        attributes=' xmlns="urn:root" exclude-result-prefixes="#default"',
    )

    out = result.principal_tree.root_element
    assert out.namespace_uri == "urn:d"
    # The excluded URI is still required by the element name, so only that
    # one namespace node is reintroduced; every other excluded binding stays
    # out of the copied set.
    assert _declarations(out) == {"": "urn:d"}
    assert out.in_scope_namespaces[""] == "urn:d"
    assert "a" not in out.in_scope_namespaces
    assert "a2" not in out.in_scope_namespaces
    assert result.serialize() == ('<?xml version="1.0" encoding="UTF-8"?><out xmlns="urn:d"/>')


def test_literal_excluded_uri_required_by_an_attribute_name_is_fixed_up() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out a:value="1"/>
        </xsl:template>
        """,
        attributes=' xmlns:a="urn:a" exclude-result-prefixes="a"',
    )

    out = result.principal_tree.root_element
    attribute = out.attributes[0]
    assert attribute.name == "{urn:a}value"
    assert attribute.prefix == "a"
    # Exclusion removed the copied node; namespace fixup reintroduces exactly
    # the declaration the attribute name needs.
    assert _declarations(out) == {"a": "urn:a"}
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?><out xmlns:a="urn:a" a:value="1"/>'
    )


def test_literal_xml_namespace_attribute_uses_the_xml_prefix() -> None:
    result = _transform(
        """
        <xsl:template name="main"><out xml:lang="en"/></xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert out.attributes[0].prefix == "xml"
    assert result.serialize() == ('<?xml version="1.0" encoding="UTF-8"?><out xml:lang="en"/>')


def test_namespace_alias_does_not_rewrite_attribute_set_attributes() -> None:
    result = _transform(
        """
        <xsl:attribute-set name="s">
          <xsl:attribute name="old:from-set">set</xsl:attribute>
        </xsl:attribute-set>
        <xsl:namespace-alias stylesheet-prefix="old" result-prefix="new"/>
        <xsl:template name="main">
          <out xsl:use-attribute-sets="s" old:from-literal="lit"/>
        </xsl:template>
        """,
        attributes=' xmlns:old="urn:old" xmlns:new="urn:new"',
    )

    out = result.principal_tree.root_element
    attributes = {attribute.name: attribute for attribute in out.attributes}
    assert set(attributes) == {"{urn:old}from-set", "{urn:new}from-literal"}
    assert attributes["{urn:old}from-set"].prefix == "old"
    assert attributes["{urn:new}from-literal"].prefix == "new"
    assert _declarations(out) == {"old": "urn:old", "new": "urn:new"}


def test_literal_own_extension_prefixes_are_excluded_namespaces() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out xmlns:ext="urn:ext" xmlns:keep="urn:keep"
               xsl:extension-element-prefixes="ext"/>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    assert _declarations(out) == {"keep": "urn:keep"}
    assert "ext" not in out.in_scope_namespaces
    assert "keep" in out.in_scope_namespaces


def test_ancestor_extension_prefixes_are_excluded_for_the_subtree() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <outer xmlns:ext="urn:ext" xsl:extension-element-prefixes="ext">
            <inner/>
          </outer>
        </xsl:template>
        """
    )

    outer = result.principal_tree.root_element
    inner = outer.children[0]
    assert _declarations(outer) == {}
    assert _declarations(inner) == {}
    assert "ext" not in outer.in_scope_namespaces
    assert "ext" not in inner.in_scope_namespaces


def test_module_root_extension_prefixes_still_exclude_literal_namespaces() -> None:
    result = _transform(
        '<xsl:template name="main"><out xmlns:keep="urn:keep"/></xsl:template>',
        attributes=' xmlns:ext="urn:ext" extension-element-prefixes="ext"',
    )

    out = result.principal_tree.root_element
    assert _declarations(out) == {"keep": "urn:keep"}
    assert "ext" not in out.in_scope_namespaces


def test_namespace_alias_rewrites_element_and_attribute_names() -> None:
    result = _transform(
        """
        <xsl:namespace-alias stylesheet-prefix="axsl" result-prefix="xsl"/>
        <xsl:template name="main">
          <axsl:template axsl:match="/"/>
        </xsl:template>
        """,
        attributes=' xmlns:axsl="urn:alias"',
    )

    out = result.principal_tree.root_element
    assert out.name == "{http://www.w3.org/1999/XSL/Transform}template"
    assert out.prefix == "xsl"
    assert out.attributes[0].name == "{http://www.w3.org/1999/XSL/Transform}match"
    assert out.attributes[0].prefix == "xsl"
    assert "urn:alias" not in out.in_scope_namespaces.values()
    assert _declarations(out) == {"xsl": "http://www.w3.org/1999/XSL/Transform"}
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<xsl:template xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xsl:match="/"/>'
    )


def test_namespace_alias_to_the_null_default_namespace() -> None:
    result = _transform(
        """
        <xsl:namespace-alias stylesheet-prefix="a" result-prefix="#default"/>
        <xsl:template name="main">
          <a:foo a:bar="baz"/>
        </xsl:template>
        """,
        attributes=' xmlns:a="http://a/com/"',
    )

    out = result.principal_tree.root_element
    assert out.name == "foo"
    assert out.namespace_uri is None
    assert out.prefix == ""
    assert out.attributes[0].name == "bar"
    assert out.attributes[0].prefix == ""
    assert _declarations(out) == {}
    assert result.serialize() == ('<?xml version="1.0" encoding="UTF-8"?><foo bar="baz"/>')


def test_namespace_alias_result_prefix_rebinds_per_literal() -> None:
    result = _transform(
        """
        <xsl:namespace-alias stylesheet-prefix="f1" result-prefix="bar"
            xmlns:f1="urn:f1" xmlns:bar="urn:bar1"/>
        <xsl:namespace-alias stylesheet-prefix="f2" result-prefix="bar"
            xmlns:f2="urn:f2" xmlns:bar="urn:bar2"/>
        <xsl:template name="main">
          <out><one xmlns="urn:f1"/><two xmlns="urn:f2"/></out>
        </xsl:template>
        """
    )

    out = result.principal_tree.root_element
    one, two = out.children
    assert one.prefix == "bar"
    assert one.namespace_uri == "urn:bar1"
    assert two.prefix == "bar"
    assert two.namespace_uri == "urn:bar2"
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<out><bar:one xmlns:bar="urn:bar1"/><bar:two xmlns:bar="urn:bar2"/></out>'
    )


def test_namespace_alias_copies_a_target_uri_even_when_excluded() -> None:
    result = _transform(
        """
        <xsl:namespace-alias stylesheet-prefix="old" result-prefix="new"/>
        <xsl:template name="main">
          <old:item/>
        </xsl:template>
        """,
        attributes=' xmlns:old="urn:old" xmlns:new="urn:new" exclude-result-prefixes="new"',
    )

    out = result.principal_tree.root_element
    assert out.name == "{urn:new}item"
    assert out.prefix == "new"
    assert _declarations(out) == {"new": "urn:new"}
    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?><new:item xmlns:new="urn:new"/>'
    )


# ---------------------------------------------------------------------------
# Copy construction (c.4): namespace nodes, prefixes, declarations, identity


def _namespace_prefixes(node: XdmNode) -> list[str]:
    return [namespace.prefix for namespace in node.namespace_nodes]


def test_copy_of_deep_retains_namespaces_prefixes_and_new_identity() -> None:
    source = XdmDocument.parse(
        b'<a:root xmlns:a="urn:a" xmlns:u="urn:unused">'
        b'<b:item xmlns:b="urn:b" b:attr="1"><child/></b:item>'
        b"</a:root>"
    )
    result = _transform_source(
        '<xsl:template name="main"><out><xsl:copy-of select="/a:root"/></out></xsl:template>',
        source,
        attributes=' xmlns:a="urn:a"',
    )

    out = result.principal_tree.root_element
    copied = out.children[0]
    source_root = source.root_element
    assert copied is not source_root
    assert copied.name == "{urn:a}root"
    assert copied.prefix == "a"
    assert copied.namespace_uri == "urn:a"
    assert copied.namespace_declarations == (("a", "urn:a"), ("u", "urn:unused"))
    assert set(_namespace_prefixes(copied)) >= {"a", "u", "xml"}
    assert len(_namespace_prefixes(copied)) == len(set(_namespace_prefixes(copied)))
    assert all(namespace.parent is copied for namespace in copied.namespace_nodes)
    assert all(namespace not in source_root.namespace_nodes for namespace in copied.namespace_nodes)
    assert copied.in_scope_namespaces["u"] == "urn:unused"

    item = copied.children[0]
    assert item.name == "{urn:b}item"
    assert item.prefix == "b"
    assert item.namespace_declarations == (("b", "urn:b"),)
    assert item.attributes[0].name == "{urn:b}attr"
    assert item.attributes[0].prefix == "b"
    assert item.children[0].name == "child"


def test_shallow_copy_retains_name_attributes_and_namespaces_only() -> None:
    source = XdmDocument.parse(
        b'<a:root xmlns:a="urn:a"><b:item xmlns:b="urn:b" b:attr="1"><child/></b:item></a:root>'
    )
    result = _transform_source(
        '<xsl:template name="main">'
        '<out><xsl:for-each select="/a:root/b:item"><xsl:copy/></xsl:for-each></out>'
        "</xsl:template>",
        source,
        attributes=' xmlns:a="urn:a" xmlns:b="urn:b"',
    )

    copied = result.principal_tree.root_element.children[0]
    assert copied.name == "{urn:b}item"
    assert copied.prefix == "b"
    assert copied.children == ()
    assert copied.attributes[0].name == "{urn:b}attr"
    assert copied.attributes[0].prefix == "b"
    assert set(_namespace_prefixes(copied)) >= {"b", "xml"}
    assert all(namespace.parent is copied for namespace in copied.namespace_nodes)


def test_copy_without_namespaces_drops_unused_bindings_but_keeps_name_ones() -> None:
    source = (
        b'<src xmlns:unused="urn:unused" xmlns:p="urn:p" p:a="1"><child xmlns:q="urn:q"/></src>'
    )
    result = _transform_source(
        '<xsl:template name="main">'
        '<out><xsl:copy-of select="/*" copy-namespaces="no"/></out>'
        "</xsl:template>",
        source,
    )

    copied = result.principal_tree.root_element.children[0]
    assert copied.prefix == ""
    assert copied.attributes[0].name == "{urn:p}a"
    assert copied.attributes[0].prefix == "p"
    assert set(_namespace_prefixes(copied)) == {"p", "xml"}
    child = copied.children[0]
    assert child.in_scope_namespaces.keys() >= {"xml"}
    assert "q" not in child.in_scope_namespaces
    assert "unused" not in copied.in_scope_namespaces


@pytest.mark.parametrize(
    ("inherit", "expected_plain"),
    [("yes", {"xml", "z"}), ("no", {"xml"})],
)
def test_copy_inherit_namespaces_controls_propagation_to_constructed_children(
    inherit: str,
    expected_plain: set[str],
) -> None:
    source = b'<root xmlns:z="urn:z"><item/></root>'
    result = _transform_source(
        '<xsl:template name="main">'
        '<xsl:variable name="v">'
        f'<xsl:copy select="/*/*" inherit-namespaces="{inherit}">'
        '<xsl:element name="plain"/>'
        "</xsl:copy>"
        "</xsl:variable>"
        '<out><xsl:sequence select="$v"/></out>'
        "</xsl:template>",
        source,
    )

    item = result.principal_tree.root_element.children[0]
    plain = item.children[0]
    assert set(_namespace_prefixes(item)) >= {"z", "xml"}
    assert set(_namespace_prefixes(plain)) >= expected_plain
    if inherit == "no":
        assert "z" not in plain.in_scope_namespaces


def test_inherited_default_namespace_does_not_cross_a_no_namespace_element() -> None:
    source = b'<root><item><q:c xmlns:q="urn:q"/></item></root>'
    result = _transform_source(
        '<xsl:template name="main">'
        '<o xmlns="urn:d"><xsl:copy-of select="/*/*"/></o>'
        "</xsl:template>",
        source,
    )

    out = result.principal_tree.root_element
    item = out.children[0]
    child = item.children[0]
    assert out.in_scope_namespaces[""] == "urn:d"
    assert item.namespace_uri is None
    assert item.in_scope_namespaces.get("") is None
    assert child.namespace_uri == "urn:q"
    assert child.in_scope_namespaces.get("") is None
    assert child.namespace_node("") is None


_QNAME_SCHEMA = f"""
<xs:schema xmlns:xs="{XSD_NS}" xmlns:v="{VALUE_NS}"
    targetNamespace="{VALUE_NS}" elementFormDefault="qualified">
  <xs:complexType name="Holder">
    <xs:attribute name="ref" type="xs:QName"/>
  </xs:complexType>
  <xs:element name="holder" type="v:Holder"/>
</xs:schema>
"""


_QNAME_SOURCE = f'<v:holder xmlns:v="{VALUE_NS}" xmlns:q="urn:value" ref="q:thing"/>'.encode()


def _qname_body(select: str) -> str:
    return (
        f"<xsl:import-schema>{_QNAME_SCHEMA}</xsl:import-schema>"
        '<xsl:template name="main">'
        '<out xmlns:q="urn:other" xsl:validation="preserve">'
        f'<xsl:copy-of select="{select}" validation="preserve"/>'
        "</out>"
        "</xsl:template>"
    )


def test_copy_preserves_qname_typed_content_and_its_prefix_binding() -> None:
    result = _transform_source(
        _qname_body("/*"),
        _QNAME_SOURCE,
        attributes=f' xmlns:v="{VALUE_NS}"',
        validation="strict",
    )

    copied = result.principal_tree.root_element.children[0]
    reference = copied.attributes[0]
    assert reference.string_value == "q:thing"
    assert reference.typed_value[0].type_name.endswith("}QName")
    assert copied.in_scope_namespaces["q"] == "urn:value"
    assert result.principal_tree.root_element.in_scope_namespaces["q"] == "urn:other"
    assert 'xmlns:q="urn:value"' in result.serialize()


def test_copying_a_parentless_namespace_sensitive_attribute_is_xtte0950() -> None:
    with pytest.raises(XsltError) as caught:
        _transform_source(
            _qname_body("/*/@ref"),
            _QNAME_SOURCE,
            attributes=f' xmlns:v="{VALUE_NS}"',
            validation="strict",
        )
    assert caught.value.code == "XTTE0950"


def test_raw_delivery_returns_a_constructed_leaf_node() -> None:
    principal = (
        _compile('<xsl:template name="main"><xsl:comment select="\'c\'"/></xsl:template>')
        .transform(initial_template="main", delivery="raw")
        .principal
    )
    assert len(principal) == 1
    assert isinstance(principal[0], XdmNode)
    assert principal[0].kind is NodeKind.COMMENT
    assert principal[0].string_value == "c"


# ---------------------------------------------------------------------------
# Static errors


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("<xsl:comment select=\"'x'\">content</xsl:comment>", "XTSE0940"),
        (
            "<xsl:comment select=\"'x'\"><xsl:fallback/></xsl:comment>",
            "XTSE0940",
        ),
        (
            '<xsl:processing-instruction name="pi" select="\'x\'">content'
            "</xsl:processing-instruction>",
            "XTSE0880",
        ),
        (
            '<xsl:namespace name="p" select="\'urn:p\'">content</xsl:namespace>',
            "XTSE0910",
        ),
        ('<xsl:namespace name="p"/>', "XTSE0910"),
        (
            '<xsl:namespace name="p"><xsl:fallback/></xsl:namespace>',
            "XTSE0910",
        ),
        ("<xsl:processing-instruction/>", "XTSE0010"),
        ("<xsl:namespace/>", "XTSE0010"),
    ],
)
def test_static_constructor_errors(body: str, code: str) -> None:
    assert _error_code(f'<xsl:template name="main">{body}</xsl:template>') == code


def test_namespace_accepts_fallback_content_with_select() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out>
            <xsl:namespace name="p" select="'urn:p'"><xsl:fallback/></xsl:namespace>
            <xsl:text>value</xsl:text>
          </out>
        </xsl:template>
        """
    )

    assert result.serialize() == (
        '<?xml version="1.0" encoding="UTF-8"?><out xmlns:p="urn:p">value</out>'
    )


def test_comment_allows_whitespace_only_content_with_select() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out><xsl:comment select="'value'" xml:space="preserve">   </xsl:comment></out>
        </xsl:template>
        """
    )

    assert result.serialize() == ('<?xml version="1.0" encoding="UTF-8"?><out><!--value--></out>')


def test_comment_select_rejects_an_invalid_expression() -> None:
    assert (
        _error_code('<xsl:template name="main"><xsl:comment select="+"/></xsl:template>')
        == "XPST0003"
    )


# ---------------------------------------------------------------------------
# Dynamic errors


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            '<xsl:processing-instruction name="42">x</xsl:processing-instruction>',
            "XTDE0890",
        ),
        (
            '<xsl:processing-instruction name="xml">x</xsl:processing-instruction>',
            "XTDE0890",
        ),
        (
            '<xsl:processing-instruction name="XmL">x</xsl:processing-instruction>',
            "XTDE0890",
        ),
        ('<xsl:namespace name="xmlns" select="\'urn:p\'"/>', "XTDE0920"),
        ('<xsl:namespace name="23" select="\'urn:p\'"/>', "XTDE0920"),
        (f'<xsl:namespace name="p" select="\'{XMLNS_NS}\'"/>', "XTDE0905"),
        ('<xsl:namespace name="p" select="\'\'"/>', "XTDE0930"),
        ('<xsl:namespace name="xml" select="\'urn:x\'"/>', "XTDE0925"),
        (f'<xsl:namespace name="other" select="\'{XML_NS}\'"/>', "XTDE0925"),
    ],
)
def test_dynamic_constructor_errors(body: str, code: str) -> None:
    assert _error_code(f'<xsl:template name="main"><out>{body}</out></xsl:template>') == code


def test_the_xml_prefix_binds_the_xml_namespace() -> None:
    result = _transform(
        f"""
        <xsl:template name="main">
          <out>
            <xsl:namespace name="xml" select="'{XML_NS}'"/>
            <xsl:text>x</xsl:text>
          </out>
        </xsl:template>
        """
    )
    out = result.principal_tree.root_element
    assert out.namespace_node("xml") is not None


def test_xsd_10_profiles_reject_an_invalid_anyuri() -> None:
    assert (
        _error_code(
            '<xsl:template name="main"><out><xsl:namespace name="p">'
            "####</xsl:namespace></out></xsl:template>",
            version="2.0",
            language="2.0",
            xpath="2.0",
            xsd_version="1.0",
        )
        == "XTDE0905"
    )


def test_xsd_10_profiles_accept_a_valid_anyuri() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out><xsl:namespace name="p">urn:p</xsl:namespace></out>
        </xsl:template>
        """,
        version="2.0",
        language="2.0",
        xpath="2.0",
        xsd_version="1.0",
    )
    assert result.serialize().endswith('<out xmlns:p="urn:p"/>')


def test_xsd_11_profiles_accept_every_string_as_anyuri() -> None:
    result = _transform(
        """
        <xsl:template name="main">
          <out><xsl:namespace name="p">####</xsl:namespace></out>
        </xsl:template>
        """
    )
    out = result.principal_tree.root_element
    node = out.namespace_node("p")
    assert node is not None
    assert node.namespace_binding == "####"


# ---------------------------------------------------------------------------
# Structural regression


def test_constructors_live_outside_the_processor() -> None:
    assert compile_comment.__module__ == "pyxsd.xslt.compiler.constructors"
    assert compile_processing_instruction.__module__ == "pyxsd.xslt.compiler.constructors"
    assert compile_namespace.__module__ == "pyxsd.xslt.compiler.constructors"
    assert execute_comment.__module__ == "pyxsd.xslt.runtime.constructors"
    assert execute_processing_instruction.__module__ == "pyxsd.xslt.runtime.constructors"
    assert execute_namespace.__module__ == "pyxsd.xslt.runtime.constructors"
    assert not hasattr(processor_module, "_compile_comment")
    assert not hasattr(processor_module, "_compile_processing_instruction")
    assert not hasattr(processor_module, "_compile_namespace")
    source = inspect.getsource(processor_module)
    assert "_compile_comment" not in source
    assert "_compile_processing_instruction" not in source
    assert "_compile_namespace" not in source
    assert "_CommentPlan" not in source
    assert "_ProcessingInstructionPlan" not in source
    assert "_NamespacePlan" not in source
    functions = {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert not functions & {
        "_compile_comment",
        "_compile_processing_instruction",
        "_compile_namespace",
    }


def test_constructor_families_are_registered_declaratively() -> None:
    assert dict(CONSTRUCTOR_INSTRUCTION_COMPILERS) == {
        instruction_name("comment"): compile_comment,
        instruction_name("processing-instruction"): compile_processing_instruction,
        instruction_name("namespace"): compile_namespace,
    }
    assert dict(CONSTRUCTOR_PLAN_EXECUTORS) == {
        _CommentPlan: execute_comment,
        _ProcessingInstructionPlan: execute_processing_instruction,
        _NamespacePlan: execute_namespace,
    }
    compiled = _compile('<xsl:template name="main"><xsl:comment>x</xsl:comment></xsl:template>')
    assert set(CONSTRUCTOR_PLAN_EXECUTORS) <= set(compiled.plan_executors)
    for name, compiler in CONSTRUCTOR_INSTRUCTION_COMPILERS.items():
        assert processor_module._INSTRUCTION_COMPILERS[name] is compiler
