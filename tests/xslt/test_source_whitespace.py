"""Source whitespace stripping driven by stylesheet declarations.

``xsl:strip-space`` and ``xsl:preserve-space`` act on source trees after they
are captured and before XPath, patterns, keys, accumulators, or merging read
them.  The policy is package-local, honours import precedence, expands
NameTests against the declaring module's namespace context, and lets
``xml:space`` preserve whitespace without touching constructed trees.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.static import SpaceRule

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"


def stylesheet(body: str, *, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'version="3.0"{attributes}>{body}</xsl:stylesheet>'
    ).encode()


STRIP_ALL_STYLESHEET = stylesheet(
    '<xsl:strip-space elements="*"/>'
    '<xsl:template match="/">'
    '<out><xsl:value-of select="count(//text())"/></out>'
    "</xsl:template>"
)


def transform(
    source: bytes,
    input_source: bytes | None = None,
    **options: Any,
):
    compiled = XsltProcessor(**options).compile(io.BytesIO(source))
    if input_source is None:
        return compiled.transform(delivery="document")
    return compiled.transform(source=input_source, delivery="document")


def text_nodes(root: XdmNode | XdmDocument) -> tuple[XdmNode, ...]:
    return tuple(node for node in root.descendants if node.kind is NodeKind.TEXT)


def text_values(root: XdmNode | XdmDocument) -> list[str]:
    return [node.string_value for node in text_nodes(root)]


def _write(path: Path, body: str) -> None:
    path.write_bytes(stylesheet(body))


class MappingPolicy:
    """An explicit in-memory document policy for grounded source resources."""

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self.mapping = dict(mapping)
        self.reads: list[tuple[object, str | None, str]] = []

    def read(self, source: Any, *, base: str | None, purpose: str) -> ResourceData:
        self.reads.append((source, base, purpose))
        if not isinstance(source, str):
            content = source.read()
            if isinstance(content, str):
                content = content.encode("utf-8")
            return ResourceData(getattr(source, "name", "<memory>"), content)
        content = self.mapping.get(source)
        if content is None:
            raise ResourcePolicyError(f"denied {source!r}")
        return ResourceData(source, content)

    def parse(self, data: ResourceData, context: Any) -> Any:  # pragma: no cover
        raise AssertionError("the grounded resolver parses admitted bytes itself")


def test_strip_space_applies_to_source_text():
    result = transform(STRIP_ALL_STYLESHEET, b"<doc> <a/> <b> x </b> </doc>")
    assert result.principal_tree.string_value == "1"


def test_source_space_rules_and_xml_space(tmp_path: Path) -> None:
    source = b"<doc><keep> <a/> </keep><drop> <a/> </drop></doc>"

    # No declaration preserves the parser's whitespace-only text nodes.
    assert (
        transform(
            stylesheet(
                '<xsl:template match="/"><out><xsl:value-of '
                'select="count(//text())"/></out></xsl:template>'
            ),
            source,
        ).principal_tree.string_value
        == "4"
    )

    # A matching strip-space declaration removes them.
    strip = stylesheet(
        '<xsl:strip-space elements="keep drop"/>'
        '<xsl:template match="/"><out><xsl:value-of '
        'select="count(//text())"/></out></xsl:template>'
    )
    assert transform(strip, source).principal_tree.string_value == "0"

    # A matching preserve-space declaration wins over the strip declaration for
    # that element; the preserve wildcard does not override the exact strip
    # match for the other element.
    override = stylesheet(
        '<xsl:strip-space elements="keep drop"/>'
        '<xsl:preserve-space elements="* keep"/>'
        '<xsl:template match="/"><out><xsl:value-of '
        'select="count(//text())"/></out></xsl:template>'
    )
    assert transform(override, source).principal_tree.string_value == "2"

    # Prefixed, EQName, and wildcard NameTests use the declaring module's
    # namespace context; only the no-namespace item is preserved.
    expanded_source = (
        b'<doc><t:item xmlns:t="urn:t"> <x/> </t:item><item> <x/> </item>'
        b'<block xmlns="urn:q"> <x/> </block><wild> <x/> </wild>'
        b'<p:other xmlns:p="urn:p"> <x/> </p:other></doc>'
    )
    expanded = stylesheet(
        '<xsl:strip-space elements="t:item Q{urn:q}block Q{urn:t}* *:wild p:*"/>'
        '<xsl:template match="/"><out><xsl:value-of '
        'select="count(//text())"/></out></xsl:template>',
        attributes=' xmlns:t="urn:t" xmlns:p="urn:p"',
    )
    assert transform(expanded, expanded_source).principal_tree.string_value == "2"

    # xml:space="preserve" applies to the element and its descendants until a
    # closer xml:space="default" restores declaration-driven stripping.  An
    # unrecognized xml:space value leaves the inherited state in effect.
    xml_space_source = (
        b'<doc><a xml:space="preserve"> <b/> </a>'
        b'<c xml:space="preserve"> <d xml:space="default"> <e/> </d> </c>'
        b'<h xml:space="preserve"> <f xml:space="bogus"> <g/> </f> </h></doc>'
    )
    assert transform(STRIP_ALL_STYLESHEET, xml_space_source).principal_tree.string_value == "8"

    # Import precedence ranks declarations before their NameTest priority.
    _write(tmp_path / "base.xsl", '<xsl:strip-space elements="doc"/>')
    _write(
        tmp_path / "main.xsl",
        '<xsl:import href="base.xsl"/>'
        '<xsl:preserve-space elements="*"/>'
        '<xsl:template match="/"><out><xsl:value-of '
        'select="count(//text())"/></out></xsl:template>',
    )
    assert (
        XsltProcessor()
        .compile(tmp_path / "main.xsl")
        .transform(source=b"<doc> <a/> <b> x </b> </doc>", delivery="document")
        .principal_tree.string_value
        == "4"
    )


def test_unprefixed_name_test_uses_xpath_default_namespace() -> None:
    body = (
        '<xsl:strip-space elements="item"/>'
        '<xsl:template match="/"><out><xsl:value-of '
        'select="count(//text())"/></out></xsl:template>'
    )
    result = transform(
        stylesheet(body, attributes=' xpath-default-namespace="urn:t"'),
        b'<doc><t:item xmlns:t="urn:t"> <x/> </t:item></doc>',
    )
    assert result.principal_tree.string_value == "0"


def test_space_declaration_without_elements_is_ignored() -> None:
    body = (
        "<xsl:strip-space/>"
        '<xsl:template match="/"><out><xsl:value-of '
        'select="count(//text())"/></out></xsl:template>'
    )
    assert transform(stylesheet(body), b"<doc> <a/> </doc>").principal_tree.string_value == "2"


def test_unusable_and_wildcard_name_tests() -> None:
    from pyxsd.xslt.runtime.whitespace import apply_source_space_rules

    source = XdmDocument.parse(b"<doc> \t\r\n <keep> </keep></doc>")
    unusable = SpaceRule(
        elements=("Q{}", "missing:item"),
        namespaces={},
        preserve=False,
        module=None,
        base_uri=None,
    )
    assert apply_source_space_rules(source, rules=(unusable,)) is source

    wildcard = SpaceRule(
        elements=("*:*", "Q{}*"),
        namespaces={},
        preserve=False,
        module=None,
        base_uri=None,
    )
    filtered = apply_source_space_rules(source, rules=(wildcard,))
    assert filtered is not source
    assert filtered.string_value == ""
    assert source.string_value == " \t\n  "

    with_pi = XdmDocument.parse(b"<?probe keep?><doc> </doc>")
    filtered_pi = apply_source_space_rules(with_pi, rules=(wildcard,))
    assert filtered_pi.children[0].kind is NodeKind.PROCESSING_INSTRUCTION
    assert filtered_pi.string_value == ""


def test_import_precedence_selects_the_best_declaration(tmp_path: Path) -> None:
    _write(tmp_path / "base.xsl", '<xsl:strip-space elements="*"/>')
    _write(
        tmp_path / "main.xsl",
        '<xsl:import href="base.xsl"/>'
        '<xsl:preserve-space elements="doc"/>'
        '<xsl:template match="/"><out><xsl:value-of '
        'select="count(//text())"/></out></xsl:template>',
    )
    assert (
        XsltProcessor()
        .compile(tmp_path / "main.xsl")
        .transform(source=b"<doc> <a/> <b> x </b> </doc>", delivery="document")
        .principal_tree.string_value
        == "4"
    )

    _write(tmp_path / "preserve.xsl", '<xsl:preserve-space elements="*"/>')
    _write(
        tmp_path / "strip.xsl",
        '<xsl:import href="preserve.xsl"/>'
        '<xsl:strip-space elements="doc"/>'
        '<xsl:template match="/"><out><xsl:value-of '
        'select="count(//text())"/></out></xsl:template>',
    )
    assert (
        XsltProcessor()
        .compile(tmp_path / "strip.xsl")
        .transform(source=b"<doc> <a/> <b> x </b> </doc>", delivery="document")
        .principal_tree.string_value
        == "1"
    )

    # Import precedence decides before NameTest priority: a higher-precedence
    # wildcard beats a lower-precedence exact name.
    _write(tmp_path / "exact.xsl", '<xsl:strip-space elements="doc"/>')
    _write(
        tmp_path / "wildcard.xsl",
        '<xsl:import href="exact.xsl"/>'
        '<xsl:preserve-space elements="*"/>'
        '<xsl:template match="/"><out><xsl:value-of '
        'select="count(//text())"/></out></xsl:template>',
    )
    assert (
        XsltProcessor()
        .compile(tmp_path / "wildcard.xsl")
        .transform(source=b"<doc> <a/> <b> x </b> </doc>", delivery="document")
        .principal_tree.string_value
        == "4"
    )


def test_source_document_applies_the_package_space_policy() -> None:
    policy = MappingPolicy({"urn:source": b"<doc> <a/> <b> x </b> </doc>"})
    body = (
        '<xsl:strip-space elements="*"/>'
        '<xsl:template match="/"><out><xsl:source-document href="urn:source">'
        '<xsl:value-of select="count(//text())"/>'
        "</xsl:source-document></out></xsl:template>"
    )
    result = transform(stylesheet(body), b"<root/>", resource_policy=policy)
    assert result.principal_tree.string_value == "1"


def test_library_package_rules_apply_to_its_own_source_document(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.packages import PackageResolver

    library = tmp_path / "library.xsl"
    library.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:lib="urn:lib" name="urn:lib"
              package-version="1.0" version="3.0">
              <xsl:strip-space elements="*"/>
              <xsl:template name="lib:count" visibility="public">
                <xsl:source-document href="urn:source">
                  <xsl:value-of select="count(//text())"/>
                </xsl:source-document>
              </xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:lib="urn:lib" name="urn:principal" version="3.0">
              <xsl:use-package name="urn:lib" package-version="1.0"/>
              <xsl:preserve-space elements="*"/>
              <xsl:template name="xsl:initial-template" visibility="public">
                <out><xsl:call-template name="lib:count"/></out>
              </xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    policy = MappingPolicy({"urn:source": b"<doc> <a/> <b> x </b> </doc>"})
    compiled = XsltProcessor(
        resolver=PackageResolver.from_sources(library),
        resource_policy=policy,
    ).compile(io.BytesIO(principal.read_bytes()))

    assert compiled.transform(delivery="document").principal_tree.string_value == "1"


def test_merge_source_applies_the_package_space_policy() -> None:
    policy = MappingPolicy(
        {"urn:merge": b'<doc><item k="1"> <x/> </item><item k="2"> y </item></doc>'}
    )
    body = (
        '<xsl:strip-space elements="*"/>'
        '<xsl:template match="/"><out><xsl:merge>'
        '<xsl:merge-source for-each-source="\'urn:merge\'" select="//item">'
        '<xsl:merge-key select="@k"/>'
        "</xsl:merge-source>"
        "<xsl:merge-action>"
        '<xsl:value-of select="string-length(string(.))"/>'
        "</xsl:merge-action>"
        "</xsl:merge></out></xsl:template>"
    )
    result = transform(stylesheet(body), b"<root/>", resource_policy=policy)
    assert result.principal_tree.string_value == "03"


def test_caller_owned_document_is_not_mutated() -> None:
    source = XdmDocument.parse(b"<doc> <a/> <b> x </b> </doc>")
    before = text_values(source)
    assert before == [" ", " ", " x ", " "]

    result = (
        XsltProcessor()
        .compile(io.BytesIO(STRIP_ALL_STYLESHEET))
        .transform(source=source, delivery="document", validation="preserve")
    )

    assert result.principal_tree.string_value == "1"
    assert text_values(source) == before
    assert source.root_element.local_name == "doc"


def test_apply_source_space_rules_preserves_identity_when_nothing_is_removed() -> None:
    from pyxsd.xslt.runtime.whitespace import apply_source_space_rules

    source = XdmDocument.parse(b"<doc><keep>  </keep></doc>")
    assert apply_source_space_rules(source, rules=()) is source

    unmatched = SpaceRule(
        elements=("other",),
        namespaces={},
        preserve=False,
        module=None,
        base_uri=None,
    )
    assert apply_source_space_rules(source, rules=(unmatched,)) is source

    strip = SpaceRule(
        elements=("*",),
        namespaces={},
        preserve=False,
        module=None,
        base_uri=None,
    )
    filtered = apply_source_space_rules(source, rules=(strip,))
    assert filtered is not source
    assert filtered.string_value == ""
    assert source.string_value == "  "

    with pytest.raises(TypeError):
        apply_source_space_rules("not a document", rules=(strip,))


def test_grounded_resources_filter_caller_documents_by_package() -> None:
    from pyxsd.xslt.runtime.resources import DocumentResources
    from pyxsd.xslt.runtime.whitespace import apply_source_space_rules

    caller = XdmDocument.parse(b"<doc> <a/> </doc>")
    before = text_values(caller)
    strip = SpaceRule(
        elements=("*",),
        namespaces={},
        preserve=False,
        module=None,
        base_uri=None,
    )
    filtered = apply_source_space_rules(caller, rules=(strip,))
    assert filtered is not caller
    assert text_values(caller) == before

    resources = DocumentResources(
        {"urn:caller": caller},
        space_rules={None: (strip,), "other-package": ()},
    )
    stripped = resources.resolve("urn:caller", base=None)
    assert stripped is not caller
    assert stripped.string_value == ""
    assert resources.resolve("urn:caller", base=None) is stripped
    assert resources.resolve("urn:caller", base=None, package="other-package") is caller
    assert text_values(caller) == before

    with pytest.raises(TypeError):
        DocumentResources({}, space_rules=("not", "a", "mapping"))

    policy = MappingPolicy({"urn:caller": b"<doc> <a/> </doc>"})
    registry = {"urn:caller": caller}
    parsed = DocumentResources(registry, policy=policy, space_rules={None: (strip,)})
    assert parsed.resolve("urn:caller", base=None).string_value == ""
    assert registry["urn:caller"] is caller
    assert text_values(caller) == before
