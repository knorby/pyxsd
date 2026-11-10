"""Template-rule selection, modes, built-ins, attribute sets, and origins.

Every stylesheet in this module is written inline or in a temporary directory,
so the tests do not depend on the pinned XSLT corpus.  They drive the
package-aware rule dispatcher through public processor entry points only.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic, XdmFunction
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xpath.environment import ExpressionEnvironment, Focus
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.instructions import (
    ComponentReference,
    Instruction,
    SequenceConstructor,
    TextNode,
)
from pyxsd.xslt.compiler.packages import Component, PackageResolver
from pyxsd.xslt.compiler.patterns import compile_pattern
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.processor import _ABSENT, _Invocation, _prepare_invocation
from pyxsd.xslt.runtime.templates import (
    PatternIndex,
    RuleDispatcher,
    RuleMatch,
    TemplateRule,
)

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"
APP_NS = "urn:app"


def stylesheet(body: str, *, attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" xmlns:app="{APP_NS}" version="3.0"{attributes}>'
        f"{body}</xsl:stylesheet>"
    ).encode()


def package(name: str, body: str, *, version: str = "1.0", attributes: str = "") -> str:
    return (
        f'<xsl:package name="{name}" package-version="{version}" '
        f'xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" xmlns:my="{MY_NS}" '
        f'xmlns:app="{APP_NS}" version="3.0"{attributes}>{body}</xsl:package>'
    )


def compile_bytes(source: bytes, **kwargs: Any):
    return XsltProcessor(**kwargs).compile(io.BytesIO(source))


def transform_path(path: Path, input_source: bytes, *, delivery: str = "document", **options: Any):
    return (
        XsltProcessor().compile(path).transform(source=input_source, delivery=delivery, **options)
    )


def transform(
    source: bytes,
    input_source: bytes | None = None,
    *,
    delivery: str = "document",
    **options: Any,
):
    compiled = compile_bytes(source)
    if input_source is None:
        return compiled.transform(delivery=delivery, **options)
    return compiled.transform(source=input_source, delivery=delivery, **options)


def element_children(node: Any) -> tuple[Any, ...]:
    return tuple(child for child in node.children if child.kind is NodeKind.ELEMENT)


def text_of(node: Any) -> str:
    return node.string_value


def error_code(call: Callable[[], object]) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


SELECTION_IMPORT = stylesheet(
    """
    <xsl:template match="item">
      <xsl:text>import,</xsl:text>
      <xsl:apply-imports/>
    </xsl:template>
    """
)


def _write(tmp_path: Path, name: str, text: bytes | str) -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    return path


def test_component_selection_and_scope(tmp_path: Path) -> None:
    imported = _write(tmp_path, "base.xsl", SELECTION_IMPORT)
    selection = stylesheet(
        f"""
        <xsl:import href="{imported.name}"/>
        <xsl:template match="item" priority="1">
          <xsl:text>next,</xsl:text>
          <xsl:apply-imports/>
        </xsl:template>
        <xsl:template match="item" priority="2">
          <xsl:text>override,</xsl:text>
          <xsl:next-match/>
        </xsl:template>
        <xsl:template match="/">
          <xsl:apply-templates select="/*/item"/>
        </xsl:template>
        """
    )
    selection_path = _write(tmp_path, "selection.xsl", selection)
    result = (
        XsltProcessor()
        .compile(selection_path)
        .transform(
            source=b"<doc><item>builtin</item></doc>",
            delivery="document",
        )
    )
    assert result.principal_tree.string_value == "override,next,import,builtin"

    tunnel = stylesheet(
        """
        <xsl:template name="outer">
          <xsl:param name="t" tunnel="yes" select="'outer'"/>
          <xsl:call-template name="inner">
            <xsl:with-param name="t" select="concat($t, '-inner')" tunnel="yes"/>
          </xsl:call-template>
        </xsl:template>
        <xsl:template name="inner">
          <xsl:param name="t" tunnel="yes" select="'missing'"/>
          <xsl:value-of select="$t"/>
        </xsl:template>
        <xsl:template name="xsl:initial-template">
          <xsl:call-template name="outer"/>
        </xsl:template>
        """
    )
    assert transform(tunnel).principal_tree.string_value == "outer-inner"

    function = stylesheet(
        """
        <xsl:function name="my:f" as="xs:integer">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="$value + 1"/>
        </xsl:function>
        <xsl:template name="xsl:initial-template">
          <xsl:sequence select="my:f('41')"/>
        </xsl:template>
        """
    )
    assert transform(function, delivery="raw").principal == (XdmAtomic.integer(42),)


def test_import_precedence_beats_higher_priority(tmp_path: Path) -> None:
    imported = _write(
        tmp_path,
        "base.xsl",
        stylesheet(
            """
            <xsl:template match="item" priority="100"><from-import/></xsl:template>
            """
        ),
    )
    principal = stylesheet(
        f"""
        <xsl:import href="{imported.name}"/>
        <xsl:template match="item" priority="-100"><from-principal/></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    principal_path = _write(tmp_path, "principal.xsl", principal)
    result = transform_path(principal_path, b"<doc><item/></doc>")
    assert element_children(result.principal_tree)[0].local_name == "from-principal"


def test_default_priority_prefers_named_pattern_over_wildcard(tmp_path: Path) -> None:
    imported = _write(
        tmp_path,
        "base.xsl",
        stylesheet('<xsl:template match="*"><wildcard/></xsl:template>'),
    )
    principal = stylesheet(
        f"""
        <xsl:import href="{imported.name}"/>
        <xsl:template match="item"><named/></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    principal_path = _write(tmp_path, "principal.xsl", principal)
    result = transform_path(principal_path, b"<doc><item/></doc>")
    assert element_children(result.principal_tree)[0].local_name == "named"


def test_conflict_resolution_last_declaration_and_failure_mode() -> None:
    last_wins = stylesheet(
        """
        <xsl:template match="item" priority="1"><first/></xsl:template>
        <xsl:template match="item" priority="1"><second/></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    result = transform(last_wins, b"<doc><item/></doc>")
    assert element_children(result.principal_tree)[0].local_name == "second"

    failure = stylesheet(
        """
        <xsl:mode on-multiple-match="fail"/>
        <xsl:template match="item"><first/></xsl:template>
        <xsl:template match="item"><second/></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(failure, b"<doc><item/></doc>")) == "XTDE0540"


def test_next_match_and_apply_imports_require_a_current_rule() -> None:
    next_match = stylesheet(
        """
        <xsl:template name="xsl:initial-template"><xsl:next-match/></xsl:template>
        """
    )
    apply_imports = stylesheet(
        """
        <xsl:template name="xsl:initial-template"><xsl:apply-imports/></xsl:template>
        """
    )
    assert error_code(lambda: transform(next_match)) == "XTDE0560"
    assert error_code(lambda: transform(apply_imports)) == "XTDE0560"


def test_builtin_rules_cover_every_item_kind() -> None:
    source = stylesheet(
        """
        <xsl:template match="/">
          <out>
            <xsl:apply-templates select="/*/@attr"/>
            <xsl:apply-templates select="/*/node()"/>
            <xsl:apply-templates select="/*/namespace::*"/>
            <xsl:apply-templates select="(1, upper-case#1)"/>
          </out>
        </xsl:template>
        """
    )
    result = transform(
        source,
        b'<doc attr="A" xmlns:p="urn:p"><!--comment--><?pi data?><child>text</child></doc>',
    )
    out = element_children(result.principal_tree)[0]
    assert out.string_value == "Atext1"
    assert all(child.kind is NodeKind.TEXT for child in out.children)


def test_on_no_match_builtin_variants() -> None:
    deep_copy = stylesheet(
        """
        <xsl:mode on-no-match="deep-copy"/>
        <xsl:template match="/"><xsl:apply-templates select="/*"/></xsl:template>
        """
    )
    copied = transform(
        deep_copy,
        b'<doc attr="1"><a><b>text</b></a><!--c--><?pi x?></doc>',
    )
    doc = element_children(copied.principal_tree)[0]
    assert doc.name == "doc"
    assert [(attribute.name, attribute.string_value) for attribute in doc.attributes] == [
        ("attr", "1")
    ]
    assert doc.string_value == "text"

    shallow_copy = stylesheet(
        """
        <xsl:mode on-no-match="shallow-copy"/>
        <xsl:template match="b"><B><xsl:value-of select="."/></B></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*"/></xsl:template>
        """
    )
    shallow = transform(shallow_copy, b'<doc attr="1"><a><b>text</b></a></doc>')
    shallow_doc = element_children(shallow.principal_tree)[0]
    assert shallow_doc.attributes[0].string_value == "1"
    a = element_children(shallow_doc)[0]
    assert a.local_name == "a"
    b = element_children(a)[0]
    assert b.local_name == "B"
    assert b.string_value == "text"

    deep_skip = stylesheet(
        """
        <xsl:mode on-no-match="deep-skip"/>
        <xsl:template match="/"><xsl:apply-templates select="/*"/></xsl:template>
        """
    )
    assert transform(deep_skip, b"<doc><a>text</a></doc>").principal == ()

    shallow_skip = stylesheet(
        """
        <xsl:mode on-no-match="shallow-skip"/>
        <xsl:template match="/">
          <xsl:apply-templates select="/*"><xsl:with-param name="p" select="1"/></xsl:apply-templates>
        </xsl:template>
        """
    )
    assert transform(shallow_skip, b"<doc>text<a/></doc>").principal == ()

    failure = stylesheet(
        """
        <xsl:mode on-no-match="fail"/>
        <xsl:template match="/"><xsl:apply-templates select="/*"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(failure, b"<doc/>")) == "XTDE0555"


def test_template_parameters_cross_builtin_rules_and_bind_lexically() -> None:
    source = stylesheet(
        """
        <xsl:template match="/">
          <xsl:apply-templates select="/*">
            <xsl:with-param name="p" select="'parameter'"/>
          </xsl:apply-templates>
        </xsl:template>
        <xsl:template match="doc/child">
          <xsl:param name="p" select="'missing'"/>
          <out><xsl:value-of select="$p"/></out>
        </xsl:template>
        """
    )
    result = transform(source, b"<doc><child/></doc>")
    assert element_children(result.principal_tree)[0].string_value == "parameter"


def test_package_layers_next_match_and_attribute_set_original(tmp_path: Path) -> None:
    library = package(
        "urn:library",
        """
        <xsl:mode name="app:m" visibility="public"/>
        <xsl:attribute-set name="app:attrs" visibility="public">
          <xsl:attribute name="base" select="'base'"/>
        </xsl:attribute-set>
        <xsl:template match="item" mode="app:m"><from-library/></xsl:template>
        """,
    )
    principal = package(
        "urn:principal",
        """
        <xsl:use-package name="urn:library" package-version="1.0">
          <xsl:accept component="mode" names="app:m" visibility="public"/>
          <xsl:override>
            <xsl:template match="item" mode="app:m">
              <from-override/>
              <xsl:next-match/>
            </xsl:template>
            <xsl:attribute-set name="app:attrs" use-attribute-sets="xsl:original">
              <xsl:attribute name="using" select="'using'"/>
            </xsl:attribute-set>
          </xsl:override>
        </xsl:use-package>
        <xsl:mode/>
        <xsl:template match="/">
          <out xsl:use-attribute-sets="app:attrs"/>
          <xsl:apply-templates select="/*/item" mode="app:m"/>
        </xsl:template>
        """,
    )
    library_path = _write(tmp_path, "library.xsl", library)
    principal_path = _write(tmp_path, "principal.xsl", principal)
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library_path)).compile(
        principal_path
    )
    result = compiled.transform(source=b"<doc><item/></doc>", delivery="document")

    out = element_children(result.principal_tree)[0]
    assert [(attribute.name, attribute.string_value) for attribute in out.attributes] == [
        ("base", "base"),
        ("using", "using"),
    ]
    mode_results = [child.local_name for child in element_children(result.principal_tree)[1:]]
    assert mode_results == ["from-override", "from-library"]


def test_attribute_set_expansion_order_and_conflicts() -> None:
    source = stylesheet(
        """
        <xsl:attribute-set name="first">
          <xsl:attribute name="a" select="'first'"/>
          <xsl:attribute name="b" select="'first'"/>
        </xsl:attribute-set>
        <xsl:attribute-set name="second" use-attribute-sets="first">
          <xsl:attribute name="a" select="'second'"/>
        </xsl:attribute-set>
        <xsl:template match="/">
          <out xsl:use-attribute-sets="second" a="literal">
            <xsl:attribute name="b" select="'content'"/>
          </out>
        </xsl:template>
        """
    )
    result = transform(source, b"<doc/>")
    out = element_children(result.principal_tree)[0]
    assert [(attribute.name, attribute.string_value) for attribute in out.attributes] == [
        ("a", "literal"),
        ("b", "content"),
    ]


def test_attribute_set_content_and_select_values() -> None:
    source = stylesheet(
        """
        <xsl:attribute-set name="fixed">
          <xsl:attribute name="one">alpha</xsl:attribute>
          <xsl:attribute name="two" select="1, 2"/>
        </xsl:attribute-set>
        <xsl:template match="/"><out xsl:use-attribute-sets="fixed"/></xsl:template>
        """
    )
    result = transform(source, b"<doc/>")
    out = element_children(result.principal_tree)[0]
    assert [(attribute.name, attribute.string_value) for attribute in out.attributes] == [
        ("one", "alpha"),
        ("two", "1 2"),
    ]


def test_apply_imports_uses_only_lower_import_precedence(tmp_path: Path) -> None:
    base = _write(
        tmp_path,
        "base.xsl",
        stylesheet(
            """
            <xsl:template match="item" priority="10"><base/></xsl:template>
            """
        ),
    )
    principal = stylesheet(
        f"""
        <xsl:import href="{base.name}"/>
        <xsl:template match="item" priority="1">
          <override/>
          <xsl:apply-imports/>
        </xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    principal_path = _write(tmp_path, "principal.xsl", principal)
    result = transform_path(principal_path, b"<doc><item/></doc>")
    assert [child.local_name for child in element_children(result.principal_tree)] == [
        "override",
        "base",
    ]


def _compiled_pattern(source: str):
    return compile_pattern(source, StaticContext())


def _typed_rule(
    source: str,
    *,
    order: int = 0,
    priority: object = None,
    precedence: tuple[int, ...] = (),
    mode: str | None = None,
    layer: int = 0,
) -> TemplateRule:
    return TemplateRule(
        pattern=_compiled_pattern(source),
        mode=mode,
        package_layer=layer,
        precedence=precedence,
        declaration_order=order,
        priority=priority,
    )


def test_rule_dispatcher_contract_and_layer_candidates() -> None:
    document = XdmDocument.parse(b"<doc><item/></doc>")
    item = document.root_element.children[0] if document.root_element else None
    assert item is not None
    environment = ExpressionEnvironment()
    with pytest.raises(ValueError, match="on-multiple-match"):
        RuleDispatcher([], on_multiple_match="magic")
    with pytest.raises(TypeError, match="layer index"):
        RuleDispatcher([("first", PatternIndex([]))])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="PatternIndex"):
        RuleDispatcher([(0, "not-an-index")])  # type: ignore[list-item]

    single_rule = _typed_rule("item", order=0, priority=5)
    single = RuleDispatcher([(0, PatternIndex([single_rule]))])
    priority = single_rule.priority
    assert priority is not None
    last = RuleMatch(single_rule, 0, priority, 0)
    assert single.select(item, None, environment) == last
    assert single.next_match(item, last, None, environment) is None
    assert single.apply_imports(item, last, None, environment) is None
    forged = RuleMatch(_typed_rule("other", order=9), 0, priority, 0)
    with pytest.raises(XsltError) as caught:
        single.next_match(item, forged, None, environment)
    assert caught.value.code == "XTDE0560"
    with pytest.raises(XsltError):
        single.apply_imports(item, forged, None, environment)

    current_rule = _typed_rule("item", order=0, priority=5, precedence=(0, 2))
    same_rule = _typed_rule("item", order=1, priority=1, precedence=(0, 2))
    lower_rule = _typed_rule("item", order=2, priority=100, precedence=(0, 1))
    descendant_rule = _typed_rule("item", order=3, priority=1, precedence=(0, 2, 1))
    layer_rule = _typed_rule("item", order=4, priority=1, layer=1)
    priority = current_rule.priority
    layer_priority = layer_rule.priority
    descendant_priority = descendant_rule.priority
    assert priority is not None and layer_priority is not None
    assert descendant_priority is not None
    current = RuleMatch(current_rule, 0, priority, 0)
    same_priority_lower = RuleMatch(same_rule, 0, priority, 0)
    lower_precedence = RuleMatch(lower_rule, 0, priority, 0)
    descendant = RuleMatch(descendant_rule, 0, descendant_priority, 0)
    lower_layer = RuleMatch(layer_rule, 0, layer_priority, 1)
    dispatcher = RuleDispatcher(
        [
            (
                0,
                PatternIndex(
                    [
                        lower_precedence.rule,
                        descendant.rule,
                        same_priority_lower.rule,
                        current.rule,
                    ]
                ),
            ),
            (1, PatternIndex([lower_layer.rule])),
        ]
    )
    selected = dispatcher.select(item, None, environment)
    assert selected is not None and selected.rule is current.rule
    imported = dispatcher.apply_imports(item, current, None, environment)
    assert imported == descendant
    assert dispatcher.apply_imports(item, descendant, None, environment) == lower_layer
    assert dispatcher.apply_imports(item, lower_precedence, None, environment) == lower_layer


def test_pattern_index_union_alternatives_and_environment_validation() -> None:
    document = XdmDocument.parse(b"<doc><item/></doc>")
    item = document.root_element.children[0] if document.root_element else None
    assert item is not None
    union = _typed_rule("item | item", order=1)
    index = PatternIndex([union])
    property_matches = index.matching(None, 0, item, ExpressionEnvironment())
    assert [match.alternative_index for match in property_matches] == [1, 0]
    with pytest.raises(TypeError, match="environment"):
        index.matching(None, 0, item, "not-an-environment")  # type: ignore[arg-type]
    non_matching = PatternIndex([_typed_rule("other", order=2)])
    assert non_matching.matching(None, 0, item, ExpressionEnvironment()) == ()
    assert non_matching.select(item) is None
    predicate_failure = PatternIndex([_typed_rule("item[@missing]", order=3)])
    assert predicate_failure.matching(None, 0, item, ExpressionEnvironment()) == ()


def _invocation(source: bytes) -> _Invocation:
    compiled = compile_bytes(source)
    options = _prepare_invocation(
        compiled,
        source=_ABSENT,
        parameters=None,
        global_context_item=_ABSENT,
        initial_match_selection=_ABSENT,
        template_parameters=None,
        tunnel_parameters=None,
        initial_template="main",
        initial_mode=None,
        initial_function=None,
        initial_function_arguments=(),
        output_uri=None,
        delivery="raw",
    )
    return _Invocation(compiled, options)


def test_builtin_value_covers_every_on_no_match_variant() -> None:
    invocation = _invocation(
        stylesheet(
            """
            <xsl:mode name="copy" on-no-match="deep-copy"/>
            <xsl:mode name="shallow" on-no-match="shallow-copy"/>
            <xsl:mode name="deep-skip" on-no-match="deep-skip"/>
            <xsl:mode name="shallow-skip" on-no-match="shallow-skip"/>
            <xsl:mode name="fail" on-no-match="fail"/>
            <xsl:mode name="invalid" on-no-match="sometimes"/>
            <xsl:template name="main"><out/></xsl:template>
            """
        )
    )
    document = XdmDocument.parse(b'<doc attr="A"><!--note--><child>text</child></doc>')
    doc = document.root_element
    assert doc is not None
    child = element_children(doc)[0]
    attribute = doc.attributes[0]
    text = child.children[0]

    assert invocation._builtin_value(doc, "text-only-copy", {}, {})[0].string_value == "text"
    assert invocation._builtin_value(attribute, "text-only-copy", {}, {})[0].string_value == "A"
    assert (
        invocation._builtin_value(XdmAtomic.integer(7), "text-only-copy", {}, {})[0].string_value
        == "7"
    )
    assert invocation._builtin_value(text, "text-only-copy", {}, {})[0].string_value == "text"
    comment = next(node for node in document.descendants if node.kind is NodeKind.COMMENT)
    assert invocation._builtin_value(comment, "text-only-copy", {}, {}) == ()
    assert invocation._builtin_value(document, "deep-skip", {}, {}) == ()
    assert invocation._builtin_value(document, "shallow-skip", {}, {}) == ()
    function_item = XdmFunction(lambda arguments, environment, captured: (), arity=0)
    assert invocation._builtin_value(function_item, "shallow-skip", {}, {}) == ()
    assert invocation._builtin_value(doc, "deep-skip", {}, {}) == ()
    assert invocation._builtin_value(child, "shallow-skip", {}, {}) == ()
    assert (
        invocation._builtin_value(XdmAtomic.integer(3), "shallow-skip", {}, {})[0].string_value
        == "3"
    )
    assert invocation._builtin_value(XdmAtomic.integer(3), "shallow", {}, {}) == (
        XdmAtomic.integer(3),
    )
    deep = invocation._builtin_value(doc, "copy", {}, {})[0]
    assert isinstance(deep, XdmNode) and deep.string_value == "text"
    assert invocation._builtin_value(XdmAtomic.integer(2), "copy", {}, {}) == (
        XdmAtomic.integer(2),
    )
    shallow = invocation._builtin_value(doc, "shallow", {}, {})[0]
    assert isinstance(shallow, XdmNode) and shallow.name == "doc"
    shallow_document = invocation._builtin_value(document, "shallow", {}, {})[0]
    assert isinstance(shallow_document, XdmNode)
    assert shallow_document.kind is NodeKind.DOCUMENT
    with pytest.raises(XsltError) as caught:
        invocation._builtin_value(doc, "fail", {}, {})
    assert caught.value.code == "XTDE0555"
    with pytest.raises(XsltError) as caught:
        invocation._builtin_variant("invalid")
    assert caught.value.code == "XTSE0020"


def test_builtin_deep_and_shallow_skip_start_at_the_document_node() -> None:
    deep_skip = stylesheet('<xsl:mode name="skip" on-no-match="deep-skip"/>')
    assert transform(deep_skip, b"<doc><a>text</a></doc>", initial_mode="skip").principal == ()

    shallow_skip = stylesheet('<xsl:mode name="skip" on-no-match="shallow-skip"/>')
    assert transform(shallow_skip, b"<doc>text<a/></doc>", initial_mode="skip").principal == ()

    shallow_copy = stylesheet('<xsl:mode name="copy" on-no-match="shallow-copy"/>')
    copied = transform(shallow_copy, b"<doc attr='1'><a>text</a></doc>", initial_mode="copy")
    assert copied.principal_tree.string_value == "text"


def test_next_match_inside_construction_uses_the_current_rule() -> None:
    matched = stylesheet(
        """
        <xsl:template match="item" priority="2">
          <wrap><xsl:next-match/></wrap>
        </xsl:template>
        <xsl:template match="item" priority="1"><inner/></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    result = transform(matched, b"<doc><item/></doc>")
    wrap = element_children(result.principal_tree)[0]
    assert element_children(wrap)[0].local_name == "inner"

    fallback = stylesheet(
        """
        <xsl:template match="item">
          <wrap><xsl:next-match/></wrap>
        </xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    result = transform(fallback, b"<doc><item>text</item></doc>")
    assert element_children(result.principal_tree)[0].string_value == "text"

    absent = stylesheet(
        """
        <xsl:template name="main"><wrap><xsl:next-match/></wrap></xsl:template>
        """
    )
    assert error_code(lambda: transform(absent, initial_template="main")) == "XTDE0560"


def test_apply_imports_inside_construction_uses_only_imported_rules(tmp_path: Path) -> None:
    imported = _write(
        tmp_path,
        "base.xsl",
        stylesheet('<xsl:template match="item"><base/></xsl:template>'),
    )
    principal = stylesheet(
        f"""
        <xsl:import href="{imported.name}"/>
        <xsl:template match="item">
          <wrap><xsl:apply-imports/></wrap>
        </xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    principal_path = _write(tmp_path, "principal.xsl", principal)
    result = transform_path(principal_path, b"<doc><item/></doc>")
    wrap = element_children(result.principal_tree)[0]
    assert element_children(wrap)[0].local_name == "base"


def test_attribute_instruction_name_errors_and_prefixes() -> None:
    missing = stylesheet(
        """
        <xsl:template match="/"><out><xsl:attribute select="1"/></out></xsl:template>
        """
    )
    with pytest.raises(Exception) as caught:
        compile_bytes(missing)
    assert getattr(caught.value, "code", None) == "XTSE0010"

    mixed = stylesheet(
        """
        <xsl:template match="/">
          <out><xsl:attribute name="a" select="1">content</xsl:attribute></out>
        </xsl:template>
        """
    )
    with pytest.raises(Exception) as caught:
        compile_bytes(mixed)
    assert getattr(caught.value, "code", None) == "XTSE0620"

    empty = stylesheet(
        """
        <xsl:template match="/"><out><xsl:attribute name="{''}" select="1"/></out></xsl:template>
        """
    )
    assert error_code(lambda: transform(empty, b"<doc/>")) == "XTDE0850"

    unbound = stylesheet(
        """
        <xsl:template match="/">
          <out><xsl:attribute name="{'missing:value'}" select="1"/></out>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(unbound, b"<doc/>")) == "XTDE0850"

    prefixed = stylesheet(
        """
        <xsl:template match="/">
          <out><xsl:attribute name="app:value" select="1"/></out>
        </xsl:template>
        """
    )
    result = transform(prefixed, b"<doc/>")
    out = element_children(result.principal_tree)[0]
    assert [(attribute.name, attribute.string_value) for attribute in out.attributes] == [
        (f"{{{APP_NS}}}value", "1")
    ]


def test_attribute_set_ignores_whitespace_text_nodes() -> None:
    from pyxsd.xslt.processor import _compile_attribute_set_parts

    declaration = Instruction(
        name=f"{{{XSLT_NS}}}attribute-set",
        children=(TextNode(" "),),
        namespaces={},
    )
    component = Component(
        kind="attribute-set",
        body=SequenceConstructor(children=declaration.children),
        declaration=declaration,
        name="a",
        declarations=(declaration,),
    )
    module = type("Module", (), {"identity": None})()
    assert _compile_attribute_set_parts(component, StaticContext(), module) == (((), ()),)


def test_attribute_set_rejects_non_attribute_content() -> None:
    text = stylesheet(
        """
        <xsl:attribute-set name="a">text</xsl:attribute-set>
        <xsl:template name="main"><out xsl:use-attribute-sets="a"/></xsl:template>
        """
    )
    with pytest.raises(Exception) as caught:
        compile_bytes(text)
    assert getattr(caught.value, "code", None) == "XTSE0010"

    element = stylesheet(
        """
        <xsl:attribute-set name="a"><out/></xsl:attribute-set>
        <xsl:template name="main"><out xsl:use-attribute-sets="a"/></xsl:template>
        """
    )
    with pytest.raises(Exception) as caught:
        compile_bytes(element)
    assert getattr(caught.value, "code", None) == "XTSE0010"


def test_local_variable_empty_as_and_attribute_set_cycles() -> None:
    empty = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="empty" as="item()*"/>
          <xsl:sequence select="count($empty)"/>
        </xsl:template>
        """
    )
    assert transform(empty, initial_template="main", delivery="raw").principal == (
        XdmAtomic.integer(0),
    )

    compiled = compile_bytes(
        stylesheet(
            """
            <xsl:attribute-set name="a"><xsl:attribute name="one" select="1"/></xsl:attribute-set>
            <xsl:template name="main"><out xsl:use-attribute-sets="a"/></xsl:template>
            """
        )
    )
    options = _prepare_invocation(
        compiled,
        source=_ABSENT,
        parameters=None,
        global_context_item=_ABSENT,
        initial_match_selection=_ABSENT,
        template_parameters=None,
        tunnel_parameters=None,
        initial_template="main",
        initial_mode=None,
        initial_function=None,
        initial_function_arguments=(),
        output_uri=None,
        delivery="document",
    )
    invocation = _Invocation(compiled, options)
    component = compiled.package.attribute_set("a")
    assert component is not None
    invocation.evaluating_attribute_sets.append((component.owner, component.name))
    with pytest.raises(XsltError) as caught:
        invocation._attribute_set_attributes(component, Focus.absent(), None)
    assert caught.value.code == "XTSE0720"
    with pytest.raises(XsltError) as caught:
        invocation._resolve_attribute_set(
            ComponentReference(kind="attribute-set", name="missing"),
            None,
        )
    assert caught.value.code == "XTDE0710"


def test_apply_imports_excludes_sibling_imports(tmp_path: Path) -> None:
    b_style = stylesheet('<xsl:template match="item"><from-b/></xsl:template>')
    a_style = stylesheet(
        """
        <xsl:template match="item">
          <from-a/>
          <xsl:apply-imports/>
        </xsl:template>
        """
    )
    b_path = _write(tmp_path, "b.xsl", b_style)
    a_path = _write(tmp_path, "a.xsl", a_style)
    principal = stylesheet(
        f"""
        <xsl:import href="{b_path.name}"/>
        <xsl:import href="{a_path.name}"/>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    principal_path = _write(tmp_path, "principal.xsl", principal)
    result = transform_path(principal_path, b"<doc><item/></doc>")
    assert [child.local_name for child in element_children(result.principal_tree)] == ["from-a"]


def test_union_next_match_with_explicit_priority_does_not_reenter_the_rule() -> None:
    source = stylesheet(
        """
        <xsl:template match="item|item" priority="5">
          <alpha><xsl:next-match/></alpha>
        </xsl:template>
        <xsl:template match="item" priority="1"><low/></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    result = transform(source, b"<doc><item/></doc>")
    alpha = element_children(result.principal_tree)[0]
    assert alpha.local_name == "alpha"
    assert [child.local_name for child in element_children(alpha)] == ["low"]


def test_union_rule_without_priority_fails_on_multiple_match() -> None:
    source = stylesheet(
        """
        <xsl:mode on-multiple-match="fail"/>
        <xsl:template match="item|item"><alpha/></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(source, b"<doc><item/></doc>")) == "XTDE0540"


def test_shallow_skip_applies_attribute_rules_before_children() -> None:
    source = stylesheet(
        """
        <xsl:mode on-no-match="shallow-skip"/>
        <xsl:template match="@a"><attr><xsl:value-of select="."/></attr></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*"/></xsl:template>
        """
    )
    result = transform(source, b'<doc a="X"><child>t</child></doc>')
    attributes = [
        child for child in element_children(result.principal_tree) if child.local_name == "attr"
    ]
    assert [child.string_value for child in attributes] == ["X"]


def test_builtin_rules_flatten_arrays_and_ignore_function_items() -> None:
    arrays = stylesheet(
        """
        <xsl:template match="/"><xsl:apply-templates select="([1, 2])"/></xsl:template>
        """
    )
    assert transform(arrays, b"<doc/>").principal_tree.string_value == "12"

    shallow_arrays = stylesheet(
        """
        <xsl:mode on-no-match="shallow-skip"/>
        <xsl:template match="/"><xsl:apply-templates select="([1, 2])"/></xsl:template>
        """
    )
    assert transform(shallow_arrays, b"<doc/>").principal_tree.string_value == "12"

    functions = stylesheet(
        """
        <xsl:template match="."><xsl:next-match/></xsl:template>
        <xsl:template match="/">
          <xsl:apply-templates select="upper-case#1"/>
        </xsl:template>
        """
    )
    assert transform(functions, b"<doc/>").principal == ()


def test_named_template_absent_context_clears_the_current_rule() -> None:
    source = stylesheet(
        """
        <xsl:template match="item" priority="2">
          <xsl:call-template name="delegate"/>
        </xsl:template>
        <xsl:template match="item" priority="1"><low/></xsl:template>
        <xsl:template name="delegate">
          <xsl:context-item use="absent"/>
          <xsl:next-match/>
        </xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(source, b"<doc><item/></doc>")) == "XTDE0560"


def test_variable_and_attribute_content_keep_the_current_rule() -> None:
    variable = stylesheet(
        """
        <xsl:template match="item" priority="2">
          <xsl:variable name="v" as="element()*"><xsl:next-match/></xsl:variable>
          <xsl:sequence select="$v"/>
        </xsl:template>
        <xsl:template match="item" priority="1"><low/></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    result = transform(variable, b"<doc><item/></doc>")
    assert [child.local_name for child in element_children(result.principal_tree)] == ["low"]

    attribute = stylesheet(
        """
        <xsl:template match="item" priority="2">
          <out><xsl:attribute name="a"><xsl:next-match/></xsl:attribute></out>
        </xsl:template>
        <xsl:template match="item" priority="1"><xsl:text>next</xsl:text></xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    attribute_result = transform(attribute, b"<doc><item/></doc>")
    out = element_children(attribute_result.principal_tree)[0]
    assert [(item.name, item.string_value) for item in out.attributes] == [("a", "next")]


def test_with_param_content_keeps_the_current_rule() -> None:
    source = stylesheet(
        """
        <xsl:template match="item" priority="2">
          <xsl:call-template name="show">
            <xsl:with-param name="p"><xsl:next-match/></xsl:with-param>
          </xsl:call-template>
        </xsl:template>
        <xsl:template match="item" priority="1"><xsl:text>next</xsl:text></xsl:template>
        <xsl:template name="show">
          <xsl:param name="p"/>
          <out><xsl:value-of select="$p"/></out>
        </xsl:template>
        <xsl:template match="/"><xsl:apply-templates select="/*/item"/></xsl:template>
        """
    )
    result = transform(source, b"<doc><item/></doc>")
    assert element_children(result.principal_tree)[0].string_value == "next"


def test_private_library_mode_and_library_default_mode(tmp_path: Path) -> None:
    library = package(
        "urn:library",
        """
        <xsl:mode name="my:hidden"/>
        <xsl:mode name="my:default"/>
        <xsl:template name="my:private" visibility="public">
          <out><xsl:apply-templates select="/*/item" mode="my:hidden"/></out>
        </xsl:template>
        <xsl:template name="my:defaulted" visibility="public">
          <out><xsl:apply-templates select="/*/item"/></out>
        </xsl:template>
        <xsl:template name="my:explicit-default" visibility="public">
          <out><xsl:apply-templates select="/*/item" mode="#default"/></out>
        </xsl:template>
        <xsl:template match="item" mode="my:hidden"><hidden/></xsl:template>
        <xsl:template match="item" mode="my:default"><defaulted/></xsl:template>
        """,
        attributes=' default-mode="my:default"',
    )
    principal = package(
        "urn:principal",
        """
        <xsl:use-package name="urn:library" package-version="1.0">
          <xsl:accept component="template" names="my:private my:defaulted my:explicit-default" visibility="public"/>
        </xsl:use-package>
        <xsl:template name="xsl:initial-template" visibility="public">
          <xsl:call-template name="my:private"/>
          <xsl:call-template name="my:defaulted"/>
          <xsl:call-template name="my:explicit-default"/>
        </xsl:template>
        """,
    )
    library_path = _write(tmp_path, "library.xsl", library)
    principal_path = _write(tmp_path, "principal.xsl", principal)
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library_path)).compile(
        principal_path
    )
    result = compiled.transform(
        source=b"<doc><item/></doc>",
        initial_template="xsl:initial-template",
        delivery="document",
    )
    assert [child.local_name for child in element_children(result.principal_tree)] == [
        "out",
        "out",
        "out",
    ]
    assert [
        [child.local_name for child in element_children(out)]
        for out in element_children(result.principal_tree)
    ] == [["hidden"], ["defaulted"], ["defaulted"]]


def test_unnamed_mode_selector_and_environment_global_lookup() -> None:
    unnamed = stylesheet(
        """
        <xsl:template match="item"><unnamed/></xsl:template>
        <xsl:template match="/">
          <xsl:apply-templates select="/*/item" mode="#unnamed"/>
        </xsl:template>
        """
    )
    result = transform(unnamed, b"<doc><item/></doc>")
    assert element_children(result.principal_tree)[0].local_name == "unnamed"

    invocation = _invocation(
        stylesheet(
            """
            <xsl:variable name="g" select="1"/>
            <xsl:template name="main"><out/></xsl:template>
            """
        )
    )
    assert invocation._environment({}, None).variables == {}
    component = invocation.stylesheet._package_variables[None][0]
    invocation._global_value(component)
    environment = invocation._environment({}, None)
    assert environment.variables["g"] == (XdmAtomic.integer(1),)
