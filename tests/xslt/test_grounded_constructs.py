"""Grounded ``xsl:fork`` and ``xsl:source-document`` execution.

``xsl:fork`` concatenates independent branch results in stylesheet order.
``xsl:source-document`` reads one already-authorized XML resource and evaluates
its content with the grounded document node as the context item.  There is no
streaming cursor, no bounded-memory claim, and no implicit filesystem or
network retrieval: a resource is available only when the invocation already
holds it or an explicit policy admits it.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.runtime.instructions import construct_grounded_document
from pyxsd.xslt.runtime.resources import DocumentResources, resolve_document_uri

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"


def stylesheet(body: str, *, version: str = "3.0") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'version="{version}">{body}</xsl:stylesheet>'
    ).encode()


def compiled(source: Any, **options: Any):
    return XsltProcessor(**options).compile(
        io.BytesIO(source) if isinstance(source, bytes) else source
    )


def transform(
    source: bytes,
    input_source: bytes | None = None,
    *,
    delivery: str = "document",
    **options: Any,
):
    stylesheet_ = compiled(source)
    if input_source is None:
        return stylesheet_.transform(delivery=delivery, **options)
    return stylesheet_.transform(source=input_source, delivery=delivery, **options)


def error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def element_children(node: Any) -> tuple[XdmNode, ...]:
    return tuple(child for child in node.children if child.kind is NodeKind.ELEMENT)


class MappingPolicy:
    """An explicit in-memory resource policy for grounded documents."""

    def __init__(self, mapping: dict[str, bytes], *, failure: BaseException | None = None) -> None:
        self.mapping = dict(mapping)
        self.failure = failure
        self.reads: list[tuple[object, str | None, str]] = []

    def read(self, source: Any, *, base: str | None, purpose: str) -> ResourceData:
        self.reads.append((source, base, purpose))
        if self.failure is not None:
            raise self.failure
        if not isinstance(source, str):
            content = source.read()
            if isinstance(content, str):
                content = content.encode("utf-8")
            return ResourceData(getattr(source, "name", "<memory>"), content)
        try:
            content = self.mapping[source]
        except KeyError:
            raise ResourcePolicyError(f"denied {source!r}") from None
        return ResourceData(source, content)

    def parse(self, data: ResourceData, context: Any) -> Any:  # pragma: no cover
        raise AssertionError("the grounded resolver parses admitted bytes itself")


# ---------------------------------------------------------------------------
# xsl:fork


def test_fork_concatenates_branches_in_stylesheet_order() -> None:
    body = """
        <xsl:template name="main">
          <xsl:fork>
            <xsl:sequence select="'a'"/>
            <xsl:sequence select="'b'"/>
            <xsl:sequence select="'c'"/>
          </xsl:fork>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal == (
        XdmAtomic.string("a"),
        XdmAtomic.string("b"),
        XdmAtomic.string("c"),
    )
    assert result.principal_tree.string_value == "a b c"


def test_fork_branches_are_independent_and_keep_outer_variables() -> None:
    body = """
        <xsl:template name="main">
          <xsl:variable name="v" select="'outer'"/>
          <out>
            <xsl:fork>
              <xsl:sequence>
                <xsl:variable name="v" select="'left'"/>
                <xsl:value-of select="$v"/>
              </xsl:sequence>
              <xsl:sequence>
                <xsl:variable name="v" select="'right'"/>
                <xsl:value-of select="$v"/>
              </xsl:sequence>
            </xsl:fork>
            <xsl:value-of select="$v"/>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal_tree.string_value == "leftrightouter"


def test_fork_branch_error_propagates_and_discards_the_fork_result() -> None:
    body = """
        <xsl:template name="main">
          <xsl:fork>
            <xsl:sequence select="'before'"/>
            <xsl:sequence select="current()"/>
          </xsl:fork>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTDE1360"


def test_fork_single_for_each_group_branch_is_allowed() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:fork>
              <xsl:for-each-group select="doc/n" group-by="@k">
                <g key="{current-grouping-key()}">
                  <xsl:value-of select="current-group()" separator=","/>
                </g>
              </xsl:for-each-group>
            </xsl:fork>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="a">1</n><n k="a">2</n><n k="b">3</n></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [(group.attributes[0].string_value, group.string_value) for group in groups] == [
        ("a", "1,2"),
        ("b", "3"),
    ]


def test_fork_content_model_errors() -> None:
    cases = (
        (
            "<xsl:template name='main'><xsl:fork>"
            "<xsl:for-each-group select='(1)' group-by='.'><g/></xsl:for-each-group>"
            "<xsl:for-each-group select='(1)' group-by='.'><g/></xsl:for-each-group>"
            "</xsl:fork></xsl:template>"
        ),
        (
            "<xsl:template name='main'><xsl:fork>"
            "<xsl:for-each-group select='(1)' group-by='.'><g/></xsl:for-each-group>"
            "<xsl:sequence select='1'/>"
            "</xsl:fork></xsl:template>"
        ),
        "<xsl:template name='main'><xsl:fork><xsl:if test='true()'><g/></xsl:if></xsl:fork></xsl:template>",
        "<xsl:template name='main'><xsl:fork><g/></xsl:fork></xsl:template>",
        "<xsl:template name='main'><xsl:fork>text</xsl:fork></xsl:template>",
    )
    for body in cases:
        assert error_code(lambda body=body: compiled(stylesheet(body))) == "XTSE0010", body


def test_fork_ignores_fallback_and_allows_empty_or_nested_content() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:fork/>
            <xsl:fork xml:space="preserve">
              <xsl:fallback><xsl:sequence select="'fallback'"/></xsl:fallback>
              <xsl:sequence select="'one'"/>
            </xsl:fork>
            <xsl:fork>
              <xsl:sequence>
                <xsl:fork>
                  <xsl:sequence select="'nested'"/>
                </xsl:fork>
              </xsl:sequence>
            </xsl:fork>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal_tree.string_value == "one nested"


def test_fork_ignores_preserved_whitespace_text_nodes() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0" xml:space="preserve">'
        '<xsl:template name="main"><xsl:fork> '
        "<xsl:sequence select=\"'one'\"/> "
        "</xsl:fork></xsl:template></xsl:stylesheet>"
    ).encode()
    result = transform(source, initial_template="main")
    assert result.principal == (XdmAtomic.string("one"),)


def test_fork_inside_a_merge_action_sees_current_merge_state_in_every_branch() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="doc/a">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-source select="doc/b">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <xsl:fork>
                  <xsl:sequence>
                    <left key="{current-merge-key()}" count="{count(current-merge-group())}"/>
                  </xsl:sequence>
                  <xsl:sequence>
                    <right key="{current-merge-key()}" count="{count(current-merge-group())}"/>
                  </xsl:sequence>
                </xsl:fork>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><a k="1"/><b k="1"/></doc>',
        initial_template="main",
    )
    branches = element_children(result.principal_tree.root_element)
    assert [
        (
            branch.local_name,
            branch.attributes[0].string_value,
            branch.attributes[1].string_value,
        )
        for branch in branches
    ] == [
        ("left", "1", "2"),
        ("right", "1", "2"),
    ]


# ---------------------------------------------------------------------------
# xsl:source-document


def test_source_document_evaluates_content_with_the_document_as_context() -> None:
    body = """
        <xsl:template name="main">
          <xsl:source-document href="urn:doc">
            <out pos="{position()}" size="{last()}">
              <xsl:value-of select="/doc/n"/>
            </out>
          </xsl:source-document>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc><n>grounded</n></doc>"})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    out = result.principal_tree.root_element
    assert out.attributes[0].string_value == "1"
    assert out.attributes[1].string_value == "1"
    assert out.string_value == "grounded"


def test_source_document_exposes_document_uri_and_base_uri() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:source-document href="urn:doc">
              <document-uri><xsl:value-of select="document-uri(/)"/></document-uri>
              <base-uri><xsl:value-of select="base-uri(/)"/></base-uri>
            </xsl:source-document>
          </out>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc/>"})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    document_uri, base_uri = element_children(result.principal_tree.root_element)
    assert document_uri.string_value == "urn:doc"
    assert base_uri.string_value == "urn:doc"


def test_source_document_streamable_uses_ordinary_grounded_semantics() -> None:
    body = """
        <xsl:template name="main">
          <xsl:source-document href="urn:doc" streamable="yes">
            <out size="{last()}"><xsl:sequence select="/doc/n"/></out>
          </xsl:source-document>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc><n>grounded</n></doc>"})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    out = result.principal_tree.root_element
    assert out.attributes[0].string_value == "1"
    assert out.string_value == "grounded"
    assert result.principal_tree.string_value == "grounded"


def test_source_document_href_is_an_attribute_value_template() -> None:
    body = """
        <xsl:template name="main">
          <xsl:variable name="name" select="'doc'"/>
          <xsl:source-document href="{concat('urn:', $name)}">
            <xsl:value-of select="/doc/n"/>
          </xsl:source-document>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc><n>template</n></doc>"})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    assert result.principal_tree.string_value == "template"


def test_source_document_resolves_relative_href_against_the_stylesheet_base(
    tmp_path: Path,
) -> None:
    document_path = tmp_path / "doc.xml"
    document_path.write_text("<doc><n>relative</n></doc>", encoding="utf-8")
    stylesheet_path = tmp_path / "style.xsl"
    stylesheet_path.write_text(
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:template name="main">'
        '<xsl:source-document href="doc.xml"><xsl:value-of select="/doc/n"/></xsl:source-document>'
        "</xsl:template></xsl:stylesheet>",
        encoding="utf-8",
    )
    policy = MappingPolicy(
        {
            str(stylesheet_path): stylesheet_path.read_bytes(),
            document_path.as_uri(): document_path.read_bytes(),
        }
    )
    result = compiled(stylesheet_path, resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    assert result.principal_tree.string_value == "relative"
    assert (document_path.as_uri(), None, "instance") in policy.reads


def test_source_document_caches_the_document_for_the_invocation() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <first><xsl:source-document href="urn:doc">
              <xsl:value-of select="generate-id(/)"/>
            </xsl:source-document></first>
            <second><xsl:source-document href="urn:doc">
              <xsl:value-of select="generate-id(/)"/>
            </xsl:source-document></second>
            <document><xsl:value-of select="count(document('urn:doc')/doc/n)"/></document>
          </out>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc><n>1</n><n>2</n></doc>"})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    first, second, document = element_children(result.principal_tree.root_element)
    assert first.string_value == second.string_value
    assert document.string_value == "2"
    assert [read for read in policy.reads if read[0] == "urn:doc"] == [
        ("urn:doc", None, "instance")
    ]


def test_nested_source_documents_restore_the_outer_context() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:source-document href="urn:outer">
              <before><xsl:value-of select="/doc/n"/></before>
              <xsl:source-document href="urn:inner">
                <inner><xsl:value-of select="/doc/n"/></inner>
              </xsl:source-document>
              <after><xsl:value-of select="/doc/n"/></after>
            </xsl:source-document>
          </out>
        </xsl:template>
    """
    policy = MappingPolicy(
        {
            "urn:outer": b"<doc><n>outer</n></doc>",
            "urn:inner": b"<doc><n>inner</n></doc>",
        }
    )
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    before, inner, after = element_children(result.principal_tree.root_element)
    assert [before.string_value, inner.string_value, after.string_value] == [
        "outer",
        "inner",
        "outer",
    ]


def test_source_document_validation_options() -> None:
    for validation in ("strict", "lax", "preserve", "strip"):
        body = f"""
            <xsl:template name="main">
              <xsl:source-document href="urn:doc" validation="{validation}">
                <out><xsl:value-of select="/doc/n"/></out>
              </xsl:source-document>
            </xsl:template>
        """
        policy = MappingPolicy({"urn:doc": b"<doc><n>value</n></doc>"})
        result = compiled(stylesheet(body), resource_policy=policy).transform(
            delivery="document", initial_template="main"
        )
        assert result.principal_tree.string_value == "value"

    typed = """
        <xsl:template name="main">
          <xsl:source-document href="urn:doc" type="xs:anyType">
            <out><xsl:value-of select="/doc/n"/></out>
          </xsl:source-document>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc><n>typed</n></doc>"})
    result = compiled(stylesheet(typed), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    assert result.principal_tree.string_value == "typed"

    invalid = """
        <xsl:template name="main">
          <xsl:source-document href="urn:doc" validation="bogus"/>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(invalid))) == "XTSE0020"

    both = """
        <xsl:template name="main">
          <xsl:source-document href="urn:doc" validation="strict" type="xs:anyType"/>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(both))) == "XTSE1505"


def test_source_document_accepts_use_accumulators_syntax() -> None:
    body = """
        <xsl:accumulator name="acc" as="xs:integer" initial-value="0" streamable="yes">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template name="main">
          <xsl:source-document href="urn:doc" use-accumulators="acc">
            <xsl:value-of select="/doc/n"/>
          </xsl:source-document>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc><n>accumulated</n></doc>"})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    assert result.principal_tree.string_value == "accumulated"


def test_source_document_requires_href() -> None:
    body = """
        <xsl:template name="main">
          <xsl:source-document><xsl:sequence select="1"/></xsl:source-document>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(body))) == "XTSE0010"


def test_source_document_rejects_an_invalid_streamable_value() -> None:
    body = """
        <xsl:template name="main">
          <xsl:source-document href="urn:doc" streamable="bogus"/>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(body))) == "XTSE0020"


def test_source_document_resource_denial_is_explicit() -> None:
    body = """
        <xsl:template name="main">
          <xsl:source-document href="urn:denied"/>
        </xsl:template>
    """
    policy = MappingPolicy({})
    sheet = compiled(stylesheet(body), resource_policy=policy)
    with pytest.raises(ResourcePolicyError, match="denied"):
        sheet.transform(delivery="document", initial_template="main")


def test_source_document_does_not_read_the_filesystem_or_network_implicitly(
    tmp_path: Path,
) -> None:
    document_path = tmp_path / "doc.xml"
    document_path.write_text("<doc><n>secret</n></doc>", encoding="utf-8")
    body = f"""
        <xsl:template name="main">
          <xsl:source-document href="{document_path.as_uri()}"/>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "FODC0002"


def test_source_document_unavailable_without_a_policy_is_fodc0002() -> None:
    body = """
        <xsl:template name="main">
          <xsl:source-document href="urn:missing"/>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "FODC0002"


def test_source_document_malformed_resource_is_fodc0002_with_cause() -> None:
    body = """
        <xsl:template name="main">
          <xsl:source-document href="urn:bad"/>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:bad": b"<doc>"})
    sheet = compiled(stylesheet(body), resource_policy=policy)
    with pytest.raises(XsltError) as caught:
        sheet.transform(delivery="document", initial_template="main")
    assert caught.value.code == "FODC0002"
    assert caught.value.cause is not None


def test_source_document_relative_uri_without_base_is_xtde1162() -> None:
    body = """
        <xsl:template name="main">
          <xsl:source-document href="doc.xml"/>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTDE1162"


def test_source_document_fragment_identifier_is_xtde1160() -> None:
    body = """
        <xsl:template name="main">
          <xsl:source-document href="urn:doc#fragment"/>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTDE1160"


def test_source_document_with_empty_content_still_reads_the_document() -> None:
    body = """
        <xsl:template name="main">
          <xsl:source-document href="urn:doc"/>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc/>"})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    assert result.principal == ()
    assert ("urn:doc", None, "instance") in policy.reads


# ---------------------------------------------------------------------------
# Draft syntax and feature honesty


def test_draft_xsl_stream_is_rejected_even_with_fallback() -> None:
    for version in ("3.0", "4.0"):
        body = """
            <xsl:template name="main">
              <xsl:stream href="urn:doc">
                <xsl:fallback><xsl:sequence select="'fallback'"/></xsl:fallback>
              </xsl:stream>
            </xsl:template>
        """
        assert (
            error_code(
                lambda body=body, version=version: compiled(stylesheet(body, version=version))
            )
            == "XTSE0010"
        )


def test_streaming_feature_is_not_claimed() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <merge><xsl:value-of select="element-available('xsl:merge')"/></merge>
            <fork><xsl:value-of select="element-available('xsl:fork')"/></fork>
            <source><xsl:value-of select="element-available('xsl:source-document')"/></source>
            <stream><xsl:value-of select="element-available('xsl:stream')"/></stream>
            <streaming><xsl:value-of select="system-property('xsl:supports-streaming')"/></streaming>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    values = [child.string_value for child in element_children(result.principal_tree.root_element)]
    assert values == ["true", "true", "true", "false", "no"]


# ---------------------------------------------------------------------------
# Grounded resource contracts


def test_resolve_document_uri_contract() -> None:
    assert resolve_document_uri("urn:doc", None) == "urn:doc"
    assert (
        resolve_document_uri("doc.xml", "https://example.test/base/")
        == "https://example.test/base/doc.xml"
    )
    with pytest.raises(XsltError) as caught:
        resolve_document_uri("doc.xml", None)
    assert caught.value.code == "XTDE1162"
    with pytest.raises(TypeError, match="string"):
        resolve_document_uri(1, None)  # type: ignore[arg-type]


def test_document_resources_cache_and_explicit_policy_reads() -> None:
    policy = MappingPolicy({"urn:doc": b"<doc/>"})
    documents: dict[str, XdmDocument] = {}
    resources = DocumentResources(documents, policy=policy)
    first = resources.resolve("urn:doc", base=None)
    second = resources.resolve("urn:doc", base=None)
    assert first is second
    assert documents["urn:doc"] is first
    assert policy.reads == [("urn:doc", None, "instance")]


def test_document_resources_rejects_fragments_and_missing_policies() -> None:
    resources = DocumentResources({})
    with pytest.raises(XsltError) as caught:
        resources.resolve("urn:doc#fragment", base=None)
    assert caught.value.code == "XTDE1160"
    with pytest.raises(XsltError) as caught:
        resources.resolve("urn:doc", base=None)
    assert caught.value.code == "FODC0002"


def test_document_resources_preserves_policy_denial_and_maps_oserror() -> None:
    denied = MappingPolicy({})
    resources = DocumentResources({}, policy=denied)
    with pytest.raises(ResourcePolicyError, match="denied"):
        resources.resolve("urn:denied", base=None)

    unavailable = MappingPolicy({}, failure=OSError("gone"))
    resources = DocumentResources({}, policy=unavailable)
    with pytest.raises(XsltError) as caught:
        resources.resolve("urn:gone", base=None)
    assert caught.value.code == "FODC0002"
    assert isinstance(caught.value.cause, OSError)


def test_document_resources_constructor_contract() -> None:
    with pytest.raises(TypeError, match="mutable document mapping"):
        DocumentResources([])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="read"):
        DocumentResources({}, policy=object())


def test_document_resources_rejects_a_non_resource_data_result() -> None:
    class BadPolicy:
        def read(self, source: str, *, base: str | None, purpose: str) -> Any:
            return b"<doc/>"

        def parse(self, data: Any, context: Any) -> Any:  # pragma: no cover
            raise AssertionError

    resources = DocumentResources({}, policy=BadPolicy())
    with pytest.raises(TypeError, match="ResourceData"):
        resources.resolve("urn:doc", base=None)


def test_construct_grounded_document_contract() -> None:
    schema_pool = object()
    for content in (b"<doc/>", bytearray(b"<doc/>"), memoryview(b"<doc/>")):
        document = construct_grounded_document(
            content,
            uri="urn:doc",
            source_name="memory",
            schema_pool=schema_pool,
        )
        assert document.document_uri == "urn:doc"
        assert document.base_uri == "urn:doc"
        assert document.source_name == "memory"
        assert document.schema_pool is schema_pool
    with pytest.raises(TypeError, match="bytes"):
        construct_grounded_document("<doc/>", uri="urn:doc")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="source name"):
        construct_grounded_document(b"<doc/>", uri="urn:doc", source_name=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="URI"):
        construct_grounded_document(b"<doc/>", uri="")
