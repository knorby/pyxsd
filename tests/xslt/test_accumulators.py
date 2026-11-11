"""Grounded XSLT accumulators.

The tests exercise package-aware accumulator declarations, start/end phases,
rule priority, ``as`` conversion, applicability through ``use-accumulators``,
source/copied/temporary trees, lazy/eager cache equivalence, invocation
isolation, and the normative static/dynamic error codes.  Every stylesheet is
inline and every accumulator operates on a grounded XDM tree; no event parser
or streaming claim is involved.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pytest

from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xdm import XdmAtomic, XdmSequence
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.packages import PackageResolver
from pyxsd.xslt.errors import XsltDynamicError, XsltError, XsltStaticError
from pyxsd.xslt.results import _NodeFactory
from pyxsd.xslt.runtime.accumulators import (
    AccumulatorDefinition,
    AccumulatorManager,
    AccumulatorRule,
    expand_accumulator_name,
    find_cycle,
    iter_accumulator_calls,
    parse_accumulator_names,
    same_context_accumulator_call,
)

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
APP_NS = "urn:example:app"
DOC_NS = "urn:example:doc"

SOURCE = b"<doc><n>1</n><n>2</n><n>3</n></doc>"
EXPECTED_BEFORE_AFTER_VALUES = ("0", "33", "1", "11", "12", "22", "23", "33")


def stylesheet(body: str, *, version: str = "3.0") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:app="{APP_NS}" xmlns:app2="{APP_NS}" xmlns:doc="{DOC_NS}" '
        f'version="{version}">{body}</xsl:stylesheet>'
    ).encode()


def package(name: str | None, body: str, *, version: str = "3.0") -> str:
    name_attr = "" if name is None else f' name="{name}"'
    return (
        f'<xsl:package{name_attr} version="{version}" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:xs="{XSD_NS}" xmlns:app="{APP_NS}" xmlns:doc="{DOC_NS}">{body}</xsl:package>'
    )


def compiled(source: bytes, **options: Any):
    return XsltProcessor(**options).compile(io.BytesIO(source))


def compile_stylesheet(source: bytes | str):
    if isinstance(source, str):
        source = source.encode()
    return compiled(source)


def transform(
    source: bytes,
    input_source: bytes | XdmDocument | None = None,
    *,
    delivery: str = "document",
    **options: Any,
):
    stylesheet_ = compiled(source)
    if input_source is None:
        return stylesheet_.transform(delivery=delivery, **options)
    return stylesheet_.transform(source=input_source, delivery=delivery, **options)


def values(result: Any) -> tuple[str, ...]:
    return tuple(
        item.lexical if isinstance(item, XdmAtomic) else item.string_value
        for item in result.principal
    )


def error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


class MappingPolicy:
    """An explicit in-memory document policy for grounded source documents."""

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
        target = source
        if target not in self.mapping and base is not None:
            target = urljoin(base, source)
        try:
            content = self.mapping[target]
        except KeyError:
            raise ResourcePolicyError(f"denied {source!r}") from None
        return ResourceData(target, content)

    def parse(self, data: ResourceData, context: Any) -> Any:  # pragma: no cover
        raise AssertionError("the grounded resolver parses admitted bytes itself")


# ---------------------------------------------------------------------------
# Before/after values, phases, and isolation


def test_accumulator_before_after_and_isolation() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:count" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" phase="start" select="$value + 1"/>
          <xsl:accumulator-rule match="n" phase="end" select="$value + 10"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:sequence select="accumulator-before('app:count')"/>
          <xsl:sequence select="accumulator-after('app:count')"/>
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:count')"/>
            <xsl:sequence select="accumulator-after('app:count')"/>
          </xsl:for-each>
        </xsl:template>
    """
    source = stylesheet(body)
    result = transform(source, SOURCE)
    assert values(result) == EXPECTED_BEFORE_AFTER_VALUES
    assert values(transform(source, SOURCE)) == EXPECTED_BEFORE_AFTER_VALUES

    stylesheet_ = compile_stylesheet(source)
    assert values(stylesheet_.transform(source=SOURCE, delivery="document")) == (
        EXPECTED_BEFORE_AFTER_VALUES
    )
    assert values(stylesheet_.transform(source=b"<doc><n/></doc>", delivery="document")) == (
        "0",
        "11",
        "1",
        "11",
    )
    assert values(stylesheet_.transform(source=SOURCE, delivery="document")) == (
        EXPECTED_BEFORE_AFTER_VALUES
    )

    cycle = stylesheet(
        """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n" select="accumulator-before('app:a')"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
        """
    )
    with pytest.raises(XsltStaticError, match="XTDE3400"):
        compile_stylesheet(cycle)


def test_multiple_accumulators_and_both_phases_are_independent() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:count" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" phase="start" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:accumulator name="app:sum" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" phase="end" select="$value + xs:integer(.)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:count')"/>
            <xsl:sequence select="accumulator-before('app:sum')"/>
            <xsl:sequence select="accumulator-after('app:sum')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == (
        "1",
        "0",
        "1",
        "2",
        "1",
        "3",
        "3",
        "3",
        "6",
    )


def test_explicit_priority_and_last_rule_in_document_order() -> None:
    high_priority = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" priority="5" select="$value + 100"/>
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(
        transform(stylesheet('<xsl:mode use-accumulators="#all"/>' + high_priority), SOURCE)
    ) == ("100", "200", "300")

    last_wins = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n[@k = 'x']" select="$value + 1"/>
          <xsl:accumulator-rule match="n" select="$value + 10"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(last_wins), b'<doc><n k="x"/><n/></doc>')) == ("10", "20")

    explicit_wins = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n[@k = 'x']" priority="1" select="$value + 1"/>
          <xsl:accumulator-rule match="n" select="$value + 10"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(explicit_wins), b'<doc><n k="x"/><n/></doc>')) == ("1", "11")


def test_lazy_and_eager_call_orders_agree() -> None:
    template = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" phase="start" select="$value + 1"/>
          <xsl:accumulator-rule match="n" phase="end" select="$value + 10"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            {}
          </xsl:for-each>
        </xsl:template>
    """
    eager = template.format(
        "<xsl:sequence select=\"accumulator-before('app:a')\"/>"
        "<xsl:sequence select=\"accumulator-after('app:a')\"/>"
    )
    lazy = template.format(
        "<xsl:sequence select=\"accumulator-after('app:a')\"/>"
        "<xsl:sequence select=\"accumulator-before('app:a')\"/>"
    )
    assert values(transform(stylesheet(eager), SOURCE)) == ("1", "11", "12", "22", "23", "33")
    assert values(transform(stylesheet(lazy), SOURCE)) == ("11", "1", "22", "12", "33", "23")


# ---------------------------------------------------------------------------
# Declarations, conversion, and package visibility


def test_initial_value_and_rule_results_use_the_declared_type() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="'0'">
          <xsl:accumulator-rule match="n" select="'5'"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:sequence select="accumulator-before('app:a')"/>
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("0", "5", "5", "5", "5")

    default_type = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" initial-value="'text'">
          <xsl:accumulator-rule match="n" select="$value"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(default_type), SOURCE)) == ("text", "text", "text")

    bad_initial = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="'bad'">
          <xsl:accumulator-rule match="n" select="$value"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:sequence select="accumulator-before('app:a')"/>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(bad_initial), SOURCE)) == "XPTY0004"

    bad_rule = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="'bad'"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(bad_rule), SOURCE)) == "XPTY0004"


def test_rule_sequence_constructor_and_local_variables() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n">
            <xsl:variable name="increment" select="1"/>
            <xsl:sequence select="$value + $increment"/>
          </xsl:accumulator-rule>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("1", "2", "3")


def test_current_in_an_accumulator_rule_pattern_is_the_matched_item() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n[current()/@k = 'x']" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), b'<doc><n k="x"/><n/></doc>')) == ("1",)


def test_accumulator_can_be_referenced_from_a_rule_pattern() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:limit" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n[accumulator-before('app:limit') &gt; 0]"
            select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:sequence select="accumulator-after('app:a')"/>
          <xsl:sequence select="accumulator-after('app:limit')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("3", "3")


def test_text_nodes_are_visited_in_document_order() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:chars" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="text()" select="$value + string-length(.)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:sequence select="accumulator-after('app:chars')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), b"<doc>ab<n>c</n></doc>")) == ("3",)


def test_initial_value_uses_the_tree_root_as_its_focus() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="count(*)">
          <xsl:accumulator-rule match="n" select="$value"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:sequence select="accumulator-before('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("1",)


def test_rule_sequence_constructor_text_value() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:string" initial-value="''">
          <xsl:accumulator-rule match="n">x</xsl:accumulator-rule>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("x", "x", "x")


def test_non_literal_accumulator_reference_is_left_to_runtime() -> None:
    body = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + accumulator-before(concat('app:', 'b'))"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    compile_stylesheet(stylesheet(body))


def test_accumulator_rule_phase_finish_alias_is_end() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" phase="finish" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
            <xsl:sequence select="accumulator-after('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("0", "1", "1", "2", "2", "3")


def test_accumulator_visibility_is_package_private() -> None:
    library = package(
        "urn:example:lib",
        """
        <xsl:accumulator name="app:count" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="doc:n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template name="app:lib" visibility="public">
          <xsl:sequence select="accumulator-before('app:count')"/>
        </xsl:template>
        """,
    )
    principal = package(
        None,
        """
        <xsl:use-package name="urn:example:lib">
          <xsl:accept component="template" names="app:lib" visibility="public"/>
        </xsl:use-package>
        <xsl:template name="main" visibility="public">
          <xsl:for-each select="doc:doc/doc:n[1]">
            <xsl:call-template name="app:lib"/>
          </xsl:for-each>
        </xsl:template>
        """,
    )
    stylesheet_ = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(
        io.BytesIO(principal.encode())
    )
    result = stylesheet_.transform(
        source=b'<doc xmlns="urn:example:doc"><n/></doc>',
        initial_template="main",
        delivery="document",
    )
    assert values(result) == ("1",)

    direct = package(
        None,
        """
        <xsl:use-package name="urn:example:lib"/>
        <xsl:template name="main" visibility="public">
          <xsl:for-each select="doc:doc/doc:n[1]">
            <xsl:sequence select="accumulator-before('app:count')"/>
          </xsl:for-each>
        </xsl:template>
        """,
    )
    stylesheet_ = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(
        io.BytesIO(direct.encode())
    )
    assert (
        error_code(
            lambda: stylesheet_.transform(
                source=b'<doc xmlns="urn:example:doc"><n/></doc>',
                initial_template="main",
                delivery="document",
            )
        )
        == "XTDE3340"
    )

    restricted_mode = package(
        None,
        """
        <xsl:use-package name="urn:example:lib"/>
        <xsl:mode use-accumulators="app:count"/>
        <xsl:template name="main" visibility="public"/>
        """,
    )
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(
            io.BytesIO(restricted_mode.encode())
        )
    assert caught.value.code == "XTSE3300"


def test_unreferenced_accumulator_is_not_evaluated() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:accumulator name="app:b" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="error()"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("1", "2", "3")

    failing = body.replace(
        "accumulator-before('app:a')",
        "accumulator-before('app:b')",
    )
    assert error_code(lambda: transform(stylesheet(failing), SOURCE)) == "FOER0000"


# ---------------------------------------------------------------------------
# Tree identity: temporary, source, copied, and document() trees


def test_temporary_tree_has_independent_accumulator_state() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:count" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template name="main">
          <xsl:variable name="t">
            <doc><n/><n/></doc>
          </xsl:variable>
          <xsl:sequence select="accumulator-after('app:count')"/>
          <xsl:for-each select="$t/doc/n">
            <xsl:sequence select="accumulator-before('app:count')"/>
          </xsl:for-each>
          <xsl:for-each select="$t">
            <xsl:sequence select="accumulator-after('app:count')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:count')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE, initial_template="main")) == (
        "3",
        "1",
        "2",
        "2",
        "3",
    )


def test_copied_tree_has_independent_accumulator_state() -> None:
    """A copied tree recomputes its own accumulator values.

    ``xsl:copy-of``/``copy-accumulators`` are not implemented in this slice, so
    this test exercises the XDM clone boundary.  It asserts independent
    recomputation for the new tree, not transplanted accumulator values.
    """
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:count" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:param name="p"/>
        <xsl:template name="main">
          <xsl:for-each select="/doc/n[2]">
            <xsl:sequence select="accumulator-before('app:count')"/>
          </xsl:for-each>
          <xsl:for-each select="$p">
            <xsl:sequence select="accumulator-before('app:count')"/>
            <xsl:sequence select="accumulator-after('app:count')"/>
          </xsl:for-each>
        </xsl:template>
    """
    source = XdmDocument.parse(b'<doc><n id="1"/><n id="2"/></doc>')
    second = source.root_element.children[1]
    copy = _NodeFactory().clone(second, parent=None, deep=True)
    stylesheet_ = compiled(stylesheet(body))
    result = stylesheet_.transform(
        source=source,
        parameters={"p": copy},
        initial_template="main",
        delivery="document",
    )
    assert values(result) == ("2", "1", "1")


def test_source_document_use_accumulators_controls_applicability() -> None:
    base = """
        <xsl:accumulator name="app:count" as="xs:integer" initial-value="0" streamable="yes">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template name="main">
          <xsl:source-document href="urn:doc" use-accumulators="{}">
            <xsl:for-each select="doc/n">
              <xsl:sequence select="accumulator-before('app:count')"/>
            </xsl:for-each>
          </xsl:source-document>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc><n/><n/></doc>"})
    stylesheet_ = compiled(stylesheet(base.format("app:count")), resource_policy=policy)
    result = stylesheet_.transform(delivery="document", initial_template="main")
    assert values(result) == ("1", "2")

    all_accumulators = compiled(stylesheet(base.format("#all")), resource_policy=policy)
    assert values(all_accumulators.transform(delivery="document", initial_template="main")) == (
        "1",
        "2",
    )

    no_use = base.format("").replace(' use-accumulators=""', "")
    assert (
        error_code(
            lambda: compiled(stylesheet(no_use), resource_policy=policy).transform(
                delivery="document", initial_template="main"
            )
        )
        == "XTDE3362"
    )


def test_document_function_makes_all_accumulators_applicable() -> None:
    body = """
        <xsl:accumulator name="app:count" as="xs:integer" initial-value="0" streamable="yes">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:accumulator name="app:other" as="xs:integer" initial-value="0" streamable="yes">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template name="main">
          <xsl:source-document href="urn:doc" use-accumulators="app:count">
            <xsl:for-each select="doc/n[1]">
              <xsl:sequence select="accumulator-before('app:count')"/>
            </xsl:for-each>
          </xsl:source-document>
          <xsl:for-each select="document('urn:doc')/doc/n[1]">
            <xsl:sequence select="accumulator-before('app:other')"/>
          </xsl:for-each>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc><n/><n/></doc>"})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document",
        initial_template="main",
    )
    assert values(result) == ("1", "1")


def test_merge_source_use_accumulators_controls_applicability() -> None:
    body = """
        <xsl:accumulator name="app:count" as="xs:integer" initial-value="0" streamable="yes">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source for-each-source="('urn:a')" select="/doc/n"
              use-accumulators="app:count">
              <xsl:merge-key select="@k"/>
            </xsl:merge-source>
            <xsl:merge-action>
              <xsl:sequence select="accumulator-before('app:count')"/>
            </xsl:merge-action>
          </xsl:merge>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:a": b'<doc><n k="1">one</n><n k="2">two</n></doc>'})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document",
        initial_template="main",
    )
    assert values(result) == ("1", "2")

    no_use = body.replace(' use-accumulators="app:count"', "")
    assert (
        error_code(
            lambda: compiled(stylesheet(no_use), resource_policy=policy).transform(
                delivery="document", initial_template="main"
            )
        )
        == "XTDE3362"
    )


# ---------------------------------------------------------------------------
# Context and bridge boundaries


def test_current_item_is_preserved_in_rule_expressions_and_template_calls() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + count(current()/ancestor::*)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("1", "2", "3")

    template_body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n">
            <xsl:call-template name="app:increment">
              <xsl:with-param name="old" select="$value"/>
            </xsl:call-template>
          </xsl:accumulator-rule>
        </xsl:accumulator>
        <xsl:template name="app:increment">
          <xsl:param name="old" as="xs:integer"/>
          <xsl:sequence select="$old + count(current()/ancestor::*)"/>
        </xsl:template>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(template_body), SOURCE)) == ("1", "2", "3")

    function_body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:function name="app:increment" as="xs:integer">
          <xsl:param name="old" as="xs:integer"/>
          <xsl:sequence select="$old + 1"/>
        </xsl:function>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="app:increment($value)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(function_body), SOURCE)) == ("1", "2", "3")

    context_function = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:function name="app:ctx" as="item()">
          <xsl:sequence select="current()"/>
        </xsl:function>
        <xsl:accumulator name="app:a" as="item()*" initial-value="()">
          <xsl:accumulator-rule match="n" select="app:ctx()"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(context_function), SOURCE)) == "XTDE1360"


def test_attribute_and_namespace_nodes_are_not_visited() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="@*" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
            <xsl:sequence select="accumulator-after('app:a')"/>
          </xsl:for-each>
          <xsl:for-each select="doc/n[1]/@id">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), b'<doc><n id="1"/></doc>')) == "XTTE3360"

    no_attributes = body.replace(
        """
          <xsl:for-each select="doc/n[1]/@id">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>""",
        "",
    )
    assert values(transform(stylesheet(no_attributes), b'<doc><n id="1"/></doc>')) == (
        "0",
        "0",
    )


# ---------------------------------------------------------------------------
# Applicability through xsl:mode


def test_mode_use_accumulators_restricts_the_initial_match_selection() -> None:
    base = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0" streamable="yes">
          <xsl:accumulator-rule match="doc:n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:accumulator name="app:b" as="xs:integer" initial-value="0" streamable="yes">
          <xsl:accumulator-rule match="doc:n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc:doc/doc:n[1]">
            <xsl:sequence select="accumulator-before('{}')"/>
          </xsl:for-each>
        </xsl:template>
    """
    selected = '<xsl:mode use-accumulators="app:a"/>' + base.format("app:a")
    result = transform(
        stylesheet(selected),
        b'<doc xmlns="urn:example:doc"><n/></doc>',
    )
    assert values(result) == ("1",)

    other = '<xsl:mode use-accumulators="app:a"/>' + base.format("app:b")
    assert (
        error_code(
            lambda: transform(
                stylesheet(other),
                b'<doc xmlns="urn:example:doc"><n/></doc>',
            )
        )
        == "XTDE3362"
    )

    everything = '<xsl:mode use-accumulators="#all"/>' + base.format("app:b")
    assert values(
        transform(
            stylesheet(everything),
            b'<doc xmlns="urn:example:doc"><n/></doc>',
        )
    ) == ("1",)

    implicit = base.format("app:a")
    assert (
        error_code(
            lambda: transform(
                stylesheet(implicit),
                b'<doc xmlns="urn:example:doc"><n/></doc>',
            )
        )
        == "XTDE3362"
    )


def test_atomic_initial_match_selection_skips_tree_restriction() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:template match="*"><xsl:sequence select="'matched'"/></xsl:template>
    """
    result = compiled(stylesheet(body)).transform(
        initial_match_selection=(XdmAtomic.integer(1),),
        delivery="document",
    )
    assert values(result) == ("1",)


def test_mode_use_accumulators_errors() -> None:
    base = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="doc:n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:accumulator name="app:b" as="xs:integer" initial-value="0" streamable="yes">
          <xsl:accumulator-rule match="doc:n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/"/>
    """
    cases = (
        ('<xsl:mode use-accumulators="app:a"/>', "XTSE3300"),
        ('<xsl:mode use-accumulators="app:missing"/>', "XTSE3300"),
        ('<xsl:mode use-accumulators="app:b app:b"/>', "XTSE3300"),
        ('<xsl:mode use-accumulators="#all app:b"/>', "XTSE3300"),
        ('<xsl:mode use-accumulators="1bad"/>', "XTSE3300"),
        ('<xsl:mode use-accumulators="app:b app2:b"/>', "XTSE3300"),
        ('<xsl:mode use-accumulators="p:missing"/>', "XTSE3300"),
    )
    for declaration, code in cases:
        assert (
            error_code(lambda declaration=declaration: compiled(stylesheet(declaration + base)))
            == code
        ), declaration


def test_source_document_use_accumulator_errors() -> None:
    base = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:accumulator name="app:b" as="xs:integer" initial-value="0" streamable="yes">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template name="main">
          <xsl:source-document href="urn:doc" use-accumulators="{}"/>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc><n/></doc>"})
    cases = (
        ("app:a", "XTSE3300"),
        ("app:missing", "XTSE3300"),
        ("app:a app:a", "XTSE3300"),
        ("app:b app2:b", "XTSE3300"),
        ("#all app:a", "XTSE3300"),
        ("1bad", "XTSE3300"),
        ("p:missing", "XTSE3300"),
    )
    for declaration, code in cases:
        assert (
            error_code(
                lambda declaration=declaration: compiled(
                    stylesheet(base.format(declaration)),
                    resource_policy=policy,
                )
            )
            == code
        ), declaration


# ---------------------------------------------------------------------------
# Declaration and runtime errors


def test_accumulator_declaration_errors() -> None:
    cases = (
        (
            '<xsl:accumulator name="app:a"><xsl:accumulator-rule match="n"/></xsl:accumulator>',
            "XTSE0010",
        ),
        (
            '<xsl:accumulator name="app:a" initial-value="0"/>',
            "XTSE0010",
        ),
        (
            '<xsl:accumulator name="app:a" initial-value="0" select="1">'
            '<xsl:accumulator-rule match="n"/></xsl:accumulator>',
            "XTSE0090",
        ),
        (
            '<xsl:accumulator name="app:a" initial-value="0">text</xsl:accumulator>',
            "XTSE0010",
        ),
        (
            '<xsl:accumulator name="app:a" initial-value="0"><xsl:variable/></xsl:accumulator>',
            "XTSE0010",
        ),
        (
            '<xsl:accumulator name="app:a" initial-value="0">'
            '<xsl:accumulator-rule phase="start"/></xsl:accumulator>',
            "XTSE0010",
        ),
        (
            '<xsl:accumulator name="app:a" initial-value="0">'
            '<xsl:accumulator-rule match="n" phase="sideways"/></xsl:accumulator>',
            "XTSE0020",
        ),
        (
            '<xsl:accumulator name="app:a" initial-value="0">'
            '<xsl:accumulator-rule match="n" priority="bad"/></xsl:accumulator>',
            "XTSE0020",
        ),
        (
            '<xsl:accumulator name="app:a" initial-value="0">'
            '<xsl:accumulator-rule match="n" priority="NaN"/></xsl:accumulator>',
            "XTSE0020",
        ),
        (
            '<xsl:accumulator name="app:a" initial-value="0">'
            '<xsl:accumulator-rule match="n" select="1"><xsl:sequence select="2"/>'
            "</xsl:accumulator-rule></xsl:accumulator>",
            "XTSE0010",
        ),
        (
            '<xsl:accumulator name="app:a" initial-value="0" streamable="maybe">'
            '<xsl:accumulator-rule match="n"/></xsl:accumulator>',
            "XTSE0020",
        ),
    )
    for declaration, code in cases:
        assert (
            error_code(lambda declaration=declaration: compiled(stylesheet(declaration))) == code
        ), declaration


def test_rule_whitespace_with_select_is_allowed() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("1", "2", "3")


def test_accumulator_function_context_errors() -> None:
    no_context = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value"/>
        </xsl:accumulator>
        <xsl:template name="main">
          <xsl:sequence select="accumulator-before('app:a')"/>
        </xsl:template>
    """
    assert (
        error_code(lambda: transform(stylesheet(no_context), initial_template="main")) == "XTDE3350"
    )

    atomic_context = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="1">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(atomic_context), SOURCE)) == "XTTE3360"

    attribute_context = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n[1]/@id">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert (
        error_code(lambda: transform(stylesheet(attribute_context), b'<doc><n id="1"/></doc>'))
        == "XTTE3360"
    )

    undeclared = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:template match="/">
          <xsl:sequence select="accumulator-before('app:missing')"/>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(undeclared), SOURCE)) == "XTDE3340"

    bad_prefix = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:template match="/">
          <xsl:sequence select="accumulator-before('p:missing')"/>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(bad_prefix), SOURCE)) == "XTDE3340"


def test_circular_and_illegal_accumulator_references_are_static_errors() -> None:
    self_cycle = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n" select="accumulator-after('app:a')"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(self_cycle))
    assert caught.value.code == "XTDE3400"

    mutual = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + accumulator-before('app:b')"/>
        </xsl:accumulator>
        <xsl:accumulator name="app:b" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + accumulator-before('app:a')"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(mutual))
    assert caught.value.code == "XTDE3400"

    undeclared = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + accumulator-before('app:missing')"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(undeclared))
    assert caught.value.code == "XTDE3340"

    bad_prefix = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + accumulator-before('p:missing')"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(bad_prefix))
    assert caught.value.code == "XTDE3340"

    avt = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n">
            <xsl:value-of select="1" separator="{accumulator-before('app:a')}"/>
          </xsl:accumulator-rule>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(avt))
    assert caught.value.code == "XTDE3400"

    text_value_template = """
        <xsl:accumulator name="app:a" initial-value="0" expand-text="yes">
          <xsl:accumulator-rule match="n">x{accumulator-before('app:a')}</xsl:accumulator-rule>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(text_value_template))
    assert caught.value.code == "XTDE3400"

    dynamic_reference = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + function-lookup(QName('http://www.w3.org/2005/xpath-functions',
              'accumulator-before'), 1)('app:a')"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    compile_stylesheet(stylesheet(dynamic_reference))

    acyclic = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + accumulator-before('app:b')"/>
        </xsl:accumulator>
        <xsl:accumulator name="app:b" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    compile_stylesheet(stylesheet(acyclic))


# ---------------------------------------------------------------------------
# Fix round 1 regressions: static applicability, namespaces, scanning, locality


def test_source_document_use_accumulators_is_static_in_an_unused_template() -> None:
    body = """
        <xsl:template name="unused">
          <xsl:source-document href="urn:doc" use-accumulators="app:missing"/>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(body))) == "XTSE3300"


def test_merge_source_use_accumulators_is_static() -> None:
    body = """
        <xsl:template name="unused">
          <xsl:merge>
            <xsl:merge-source for-each-source="('urn:a')" select="/doc/n"
              use-accumulators="app:missing">
              <xsl:merge-key select="@k"/>
            </xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(body))) == "XTSE3300"


@pytest.mark.parametrize("form", ("true", "1", " yes "))
def test_streamable_boolean_forms_are_accepted_by_use_accumulators(form: str) -> None:
    body = f"""
        <xsl:mode use-accumulators="app:a"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0"
          streamable="{form}">
          <xsl:accumulator-rule match="doc:n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc:doc/doc:n[1]">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), b'<doc xmlns="urn:example:doc"><n/></doc>')) == ("1",)


def test_source_document_use_accumulators_accepts_streamable_true() -> None:
    body = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0"
          streamable="true">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template name="main">
          <xsl:source-document href="urn:doc" use-accumulators="app:a">
            <xsl:for-each select="doc/n">
              <xsl:sequence select="accumulator-before('app:a')"/>
            </xsl:for-each>
          </xsl:source-document>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:doc": b"<doc><n/><n/></doc>"})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document",
        initial_template="main",
    )
    assert values(result) == ("1", "2")


def test_mode_use_accumulators_uses_declaration_namespaces(tmp_path: Path) -> None:
    local_prefix = """
        <xsl:mode name="m" use-accumulators="p:a" xmlns:p="urn:example:app"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0"
          streamable="yes">
          <xsl:accumulator-rule match="doc:n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/" mode="m">
          <xsl:sequence select="accumulator-before('app:a')"/>
        </xsl:template>
    """
    assert values(
        transform(
            stylesheet(local_prefix),
            b'<doc xmlns="urn:example:doc"><n/></doc>',
            initial_mode="m",
        )
    ) == ("0",)

    module = tmp_path / "module.xsl"
    module.write_text(
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:p="{APP_NS}" xmlns:app="{APP_NS}" version="3.0">'
        '<xsl:mode name="m" use-accumulators="p:a"/>'
        '<xsl:template match="/" mode="m">'
        "<xsl:sequence select=\"accumulator-before('app:a')\"/>"
        "</xsl:template></xsl:stylesheet>",
        encoding="utf-8",
    )
    principal = stylesheet(
        '<xsl:include href="module.xsl"/>'
        '<xsl:accumulator name="app:a" as="xs:integer" initial-value="0" '
        'streamable="yes">'
        '<xsl:accumulator-rule match="doc:n" select="$value + 1"/>'
        "</xsl:accumulator>"
    )
    principal_path = tmp_path / "principal.xsl"
    principal_path.write_text(principal.decode(), encoding="utf-8")
    compiled_ = XsltProcessor().compile(principal_path)
    result = compiled_.transform(
        source=b'<doc xmlns="urn:example:doc"><n/></doc>',
        initial_mode="m",
        delivery="document",
    )
    assert values(result) == ("0",)


def test_mode_use_accumulators_uses_the_supplying_declaration() -> None:
    later_declaration = """
        <xsl:mode name="m"/>
        <xsl:mode name="m" use-accumulators="p:a" xmlns:p="urn:example:app"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0"
          streamable="yes">
          <xsl:accumulator-rule match="doc:n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/" mode="m">
          <xsl:sequence select="accumulator-before('app:a')"/>
        </xsl:template>
    """
    assert values(
        transform(
            stylesheet(later_declaration),
            b'<doc xmlns="urn:example:doc"><n/></doc>',
            initial_mode="m",
        )
    ) == ("0",)

    earlier_declaration = """
        <xsl:mode name="m" use-accumulators="p:a" xmlns:p="urn:example:app"/>
        <xsl:mode name="m"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0"
          streamable="yes">
          <xsl:accumulator-rule match="doc:n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template match="/" mode="m">
          <xsl:sequence select="accumulator-before('app:a')"/>
        </xsl:template>
    """
    assert values(
        transform(
            stylesheet(earlier_declaration),
            b'<doc xmlns="urn:example:doc"><n/></doc>',
            initial_mode="m",
        )
    ) == ("0",)


def test_static_scanning_ignores_literal_text_and_escaped_braces() -> None:
    literal_text = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n">
            <xsl:text>{accumulator-before('app:missing')}</xsl:text>
          </xsl:accumulator-rule>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    compile_stylesheet(stylesheet(literal_text))

    escaped_avt = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n">
            <xsl:value-of select="1"
              separator="{{accumulator-before('app:missing')}}"/>
          </xsl:accumulator-rule>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    compile_stylesheet(stylesheet(escaped_avt))


def test_rule_error_is_local_to_functionally_dependent_nodes() -> None:
    start_error = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n[2]" select="error()"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n[1]">
            <xsl:sequence select="accumulator-before('app:a')"/>
            <xsl:sequence select="accumulator-after('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(start_error), SOURCE)) == ("0", "0")

    start_error_n2 = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n[2]" select="error()"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n[2]">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(start_error_n2), SOURCE)) == "FOER0000"

    end_error = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n[1]" phase="end" select="error()"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n[1]">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(end_error), SOURCE)) == ("0",)

    end_error_after = end_error.replace("accumulator-before", "accumulator-after")
    assert error_code(lambda: transform(stylesheet(end_error_after), SOURCE)) == "FOER0000"

    end_error_n2 = end_error.replace("doc/n[1]", "doc/n[2]")
    assert error_code(lambda: transform(stylesheet(end_error_n2), SOURCE)) == "FOER0000"


def test_same_accumulator_preceding_node_recurrence_is_not_a_cycle() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (preceding-sibling::n[1])
              then preceding-sibling::n[1] ! accumulator-before('app:a')
              else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("1", "3", "7", "7")


def test_same_accumulator_predicate_only_recurrence_is_not_a_cycle() -> None:
    exact_probe = """
        <xsl:accumulator name="app:a" as="item()*" initial-value="()">
          <xsl:accumulator-rule match="n"
            select="preceding-sibling::n[1][accumulator-before('app:a')]"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    compile_stylesheet(stylesheet(exact_probe))

    preceding_sibling = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (preceding-sibling::n[1][accumulator-before('app:a') > 0])
              then 1 else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(preceding_sibling), SOURCE)) == (
        "1",
        "3",
        "5",
        "5",
    )

    ancestor = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (ancestor::doc[1][accumulator-before('app:a') >= 0])
              then 1 else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(ancestor), SOURCE)) == ("2", "4", "6")


def test_safe_focus_change_inside_same_context_predicate_is_not_a_cycle() -> None:
    dot_if = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (.[preceding-sibling::n[1] ! accumulator-before('app:a')])
              then 1 else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    # The predicate value is numeric, so XPath applies positional predicate
    # semantics: n2 sees 1 (selects the single context item), n3 sees 3 (does
    # not).  The recurrence itself is valid and must not be a static cycle.
    assert values(transform(stylesheet(dot_if), SOURCE)) == ("1", "3", "4", "4")

    dot_boolean = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (.[boolean(
              preceding-sibling::n[1] ! accumulator-before('app:a'))])
              then 1 else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(dot_boolean), SOURCE)) == (
        "1",
        "3",
        "5",
        "5",
    )

    slash = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (.[preceding-sibling::n[1]/accumulator-before('app:a')])
              then 1 else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(slash), SOURCE)) == ("1", "3", "4", "4")

    self_axis = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (self::n[
              preceding-sibling::n[1] ! accumulator-before('app:a')])
              then 1 else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(self_axis), SOURCE)) == ("1", "3", "4", "4")


def test_nested_same_context_predicate_under_a_focus_change_is_not_a_cycle() -> None:
    """A self-axis predicate nested in a preceding-node filter keeps its focus.

    The outer ``preceding-sibling`` predicate changes focus; the inner
    ``self::`` predicate keeps that preceding sibling as its context, so the
    accumulator call addresses an already computed node rather than the rule's
    own context node.  The recurrence is valid and must not be a static cycle.
    """
    exact = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (
              preceding-sibling::n[1][self::node()[accumulator-before('app:a') >= 0]]
            ) then 1 else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(exact), SOURCE)) == ("1", "3", "5", "5")

    element = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (
              preceding-sibling::n[1][self::element()[accumulator-before('app:a') >= 0]]
            ) then 1 else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(element), SOURCE)) == ("1", "3", "5", "5")

    dot = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (
              preceding-sibling::n[1][.[accumulator-before('app:a') >= 0]]
            ) then 1 else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(dot), SOURCE)) == ("1", "3", "5", "5")

    text = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + 1 + (if (
              preceding-sibling::n[1][self::text()[accumulator-before('app:a') >= 0]]
            ) then 1 else 0)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
          <xsl:sequence select="accumulator-after('app:a')"/>
        </xsl:template>
    """
    # ``n`` elements have no text-node children, so the inner predicate is
    # empty and the recurrence never contributes; the point is that the nested
    # self-axis predicate is not a static cycle.
    assert values(transform(stylesheet(text), SOURCE)) == ("1", "2", "3", "3")


def test_same_context_predicate_self_references_remain_static_cycles() -> None:
    current_node = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + count(.[accumulator-before('app:a')])"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(current_node))
    assert caught.value.code == "XTDE3400"

    self_axis = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + count(self::n[accumulator-before('app:a')])"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(self_axis))
    assert caught.value.code == "XTDE3400"

    self_wildcard = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + count(self::*[accumulator-before('app:a')])"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(self_wildcard))
    assert caught.value.code == "XTDE3400"

    self_node = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + count(self::node()[accumulator-before('app:a')])"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(self_node))
    assert caught.value.code == "XTDE3400"

    self_text = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + count(self::text()[accumulator-before('app:a')])"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(self_text))
    assert caught.value.code == "XTDE3400"

    self_element = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + count(self::element()[accumulator-before('app:a')])"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(self_element))
    assert caught.value.code == "XTDE3400"

    nested_self = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + count(self::node()[self::node()[accumulator-before('app:a')]])"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(nested_self))
    assert caught.value.code == "XTDE3400"

    nested_dot = """
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + count(.[self::node()[accumulator-before('app:a')]])"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    with pytest.raises(XsltStaticError) as caught:
        compile_stylesheet(stylesheet(nested_dot))
    assert caught.value.code == "XTDE3400"


def test_non_literal_self_reference_is_rejected_at_runtime() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n"
            select="$value + accumulator-before(concat('app:', 'a'))"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), SOURCE)) == "XTDE3400"


def test_unknown_accumulator_attributes_are_static_errors() -> None:
    declaration = """
        <xsl:accumulator name="app:a" initial-value="0"
          use-accumulators="#all">
          <xsl:accumulator-rule match="n"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    assert error_code(lambda: compiled(stylesheet(declaration))) == "XTSE0090"

    rule = """
        <xsl:accumulator name="app:a" initial-value="0">
          <xsl:accumulator-rule match="n" bogus="x"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    assert error_code(lambda: compiled(stylesheet(rule))) == "XTSE0090"


def test_accumulator_allows_foreign_namespaced_attributes() -> None:
    body = """
        <xsl:accumulator name="app:a" initial-value="0" app:extra="x">
          <xsl:accumulator-rule match="n" app:extra="x"/>
        </xsl:accumulator>
        <xsl:template name="main"/>
    """
    compile_stylesheet(stylesheet(body))


def test_declared_streamable_without_a_signature_is_false() -> None:
    from types import SimpleNamespace

    from pyxsd.xslt.processor import _declared_streamable

    assert _declared_streamable(SimpleNamespace(signature=None)) is False  # type: ignore[arg-type]


def test_compile_use_accumulators_requires_a_package() -> None:
    from pyxsd.xslt.compiler.context import StaticContext
    from pyxsd.xslt.compiler.instructions import Instruction
    from pyxsd.xslt.processor import _compile_use_accumulators

    instruction = Instruction(name=f"{{{XSLT_NS}}}source-document")
    with pytest.raises(XsltStaticError) as caught:
        _compile_use_accumulators("app:a", StaticContext(), instruction)
    assert caught.value.code == "XTSE3300"


def test_rule_captures_a_global_package_variable() -> None:
    body = """
        <xsl:mode use-accumulators="#all"/>
        <xsl:variable name="app:inc" select="10" visibility="public"/>
        <xsl:accumulator name="app:a" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + $app:inc"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each select="doc/n">
            <xsl:sequence select="accumulator-before('app:a')"/>
          </xsl:for-each>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), SOURCE)) == ("10", "20", "30")


def test_copy_of_is_supported_by_this_slice() -> None:
    body = """
        <xsl:template name="main">
          <xsl:copy-of select="/doc"/>
        </xsl:template>
    """
    result = transform(stylesheet(body), SOURCE, initial_template="main")

    assert result.principal[0].kind is NodeKind.ELEMENT
    assert result.principal_tree.root_element.name == "doc"
    assert result.principal_tree.root_element.string_value == "123"


# ---------------------------------------------------------------------------
# Runtime manager contracts


def _manager_definition(*rules: AccumulatorRule) -> AccumulatorDefinition:
    return AccumulatorDefinition(
        owner=None,
        name="a",
        initial_value=None,
        rules=rules,
    )


def _manager(
    definition: AccumulatorDefinition,
    *,
    evaluate_initial: Any = None,
    evaluate_rule: Any = None,
    coerce: Any = None,
    matches: Any = None,
) -> AccumulatorManager:
    if evaluate_initial is None:

        def evaluate_initial(definition: AccumulatorDefinition, root: XdmNode) -> XdmSequence:
            del definition, root
            return (XdmAtomic.integer(0),)

    if evaluate_rule is None:

        def evaluate_rule(
            definition: AccumulatorDefinition,
            rule: AccumulatorRule,
            node: XdmNode,
            old_value: XdmSequence,
        ) -> XdmSequence:
            del definition, node
            increment = 1 if rule.phase == "start" else 10
            return (XdmAtomic.integer(old_value[0].value + increment),)

    if coerce is None:

        def coerce(sequence: XdmSequence, definition: AccumulatorDefinition) -> XdmSequence:
            del definition
            return sequence

    if matches is None:

        def matches(
            definition: AccumulatorDefinition,
            rule: AccumulatorRule,
            node: XdmNode,
        ) -> bool:
            del definition, rule
            return node.kind is NodeKind.ELEMENT and node.local_name == "n"

    return AccumulatorManager(
        lookup=lambda package_name, expanded_name: definition if expanded_name == "a" else None,
        evaluate_initial=evaluate_initial,
        evaluate_rule=evaluate_rule,
        coerce=coerce,
        matches=matches,
    )


def test_manager_before_after_and_cache_reuse() -> None:
    root = XdmDocument.parse(b'<doc id="x"><n/><n/></doc>')
    definition = _manager_definition(
        AccumulatorRule(phase="start", order=0),
        AccumulatorRule(phase="end", order=1),
    )
    calls = 0

    def evaluate_initial(definition: AccumulatorDefinition, root: XdmNode) -> XdmSequence:
        nonlocal calls
        del definition, root
        calls += 1
        return (XdmAtomic.integer(0),)

    manager = _manager(definition, evaluate_initial=evaluate_initial)
    n1, n2 = (child for child in root.root_element.children if child.local_name == "n")
    assert manager.value(definition, root, "before") == (XdmAtomic.integer(0),)
    assert manager.value(definition, n1, "before") == (XdmAtomic.integer(1),)
    assert manager.value(definition, n1, "after") == (XdmAtomic.integer(11),)
    assert manager.value(definition, n2, "before") == (XdmAtomic.integer(12),)
    assert manager.value(definition, n2, "after") == (XdmAtomic.integer(22),)
    assert manager.value(definition, root, "after") == (XdmAtomic.integer(22),)
    assert calls == 1
    assert manager.definition(None, "a") is definition
    assert manager.definition(None, "b") is None


def test_manager_applicability_restriction_and_upgrade() -> None:
    root = XdmDocument.parse(b"<doc><n/></doc>")
    definition = _manager_definition(AccumulatorRule(phase="start", order=0))
    manager = _manager(definition)
    manager.restrict(root, frozenset())
    with pytest.raises(XsltDynamicError) as caught:
        manager.value(definition, root, "before")
    assert caught.value.code == "XTDE3362"
    manager.allow_all(root)
    assert manager.value(definition, root, "before") == (XdmAtomic.integer(0),)
    manager.restrict(root, frozenset({definition.key}))
    assert manager.value(definition, root, "before") == (XdmAtomic.integer(0),)

    with pytest.raises(TypeError):
        manager.restrict(XdmAtomic.integer(1), frozenset())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        manager.allow_all(XdmAtomic.integer(1))  # type: ignore[arg-type]


def test_manager_context_and_phase_errors() -> None:
    root = XdmDocument.parse(b'<doc id="x"><n/></doc>')
    definition = _manager_definition(AccumulatorRule(phase="start", order=0))
    manager = _manager(definition)
    with pytest.raises(XsltDynamicError) as caught:
        manager.value(definition, XdmAtomic.integer(1), "before")
    assert caught.value.code == "XTTE3360"
    attribute = root.root_element.attributes[0]
    with pytest.raises(XsltDynamicError) as caught:
        manager.value(definition, attribute, "before")
    assert caught.value.code == "XTTE3360"
    with pytest.raises(ValueError):
        manager.value(definition, root, "sideways")  # type: ignore[arg-type]


def test_manager_caches_dynamic_errors() -> None:
    root = XdmDocument.parse(b"<doc><n/></doc>")
    definition = _manager_definition(AccumulatorRule(phase="start", order=0))
    calls = 0

    def evaluate_initial(definition: AccumulatorDefinition, root: XdmNode) -> XdmSequence:
        nonlocal calls
        del definition, root
        calls += 1
        raise XsltDynamicError("initial value failed", code="XTDE3400")

    manager = _manager(definition, evaluate_initial=evaluate_initial)
    for phase in ("before", "after"):
        with pytest.raises(XsltDynamicError) as caught:
            manager.value(definition, root, phase)
        assert caught.value.code == "XTDE3400"
    assert calls == 1


def test_manager_detects_runtime_reentry() -> None:
    root = XdmDocument.parse(b"<doc><n/></doc>")
    definition = _manager_definition(AccumulatorRule(phase="start", order=0))
    holder: dict[str, AccumulatorManager] = {}

    def evaluate_rule(
        definition: AccumulatorDefinition,
        rule: AccumulatorRule,
        node: XdmNode,
        old_value: XdmSequence,
    ) -> XdmSequence:
        del rule, old_value
        return holder["manager"].value(definition, node, "before")

    manager = _manager(definition, evaluate_rule=evaluate_rule)
    holder["manager"] = manager
    node = next(
        child
        for child in root.root_element.children
        if child.kind is NodeKind.ELEMENT and child.local_name == "n"
    )
    with pytest.raises(XsltDynamicError) as caught:
        manager.value(definition, node, "before")
    assert caught.value.code == "XTDE3400"


# ---------------------------------------------------------------------------
# Static helper contracts


def test_parse_accumulator_names() -> None:
    assert parse_accumulator_names("") == ()
    assert parse_accumulator_names("   ") == ()
    assert parse_accumulator_names("app:a") == ("app:a",)
    assert parse_accumulator_names(" app:a   app:b ") == ("app:a", "app:b")
    assert parse_accumulator_names("#all") == ("#all",)
    for value in ("#all app:a", "app:a app:a"):
        with pytest.raises(ValueError):
            parse_accumulator_names(value)


def test_expand_accumulator_name() -> None:
    namespaces = {"app": APP_NS}
    assert expand_accumulator_name("a", namespaces) == "a"
    assert expand_accumulator_name("app:a", namespaces) == f"{{{APP_NS}}}a"
    assert expand_accumulator_name("Q{urn:a}a", namespaces) == "{urn:a}a"
    assert expand_accumulator_name("Q{}a", namespaces) == "a"
    assert expand_accumulator_name("{urn:a}a", namespaces) == "{urn:a}a"
    for value in ("", "Q{urn:a}", "{urn:a}", "{bad", "1bad", "app:", "Q{bad"):
        with pytest.raises(ValueError):
            expand_accumulator_name(value, namespaces)
    with pytest.raises(ValueError):
        expand_accumulator_name("missing:a", namespaces)


def test_iter_accumulator_calls() -> None:
    assert tuple(iter_accumulator_calls("accumulator-before('a')")) == (("before", "a"),)
    assert tuple(iter_accumulator_calls('fn:accumulator-after("a")')) == (("after", "a"),)
    assert tuple(iter_accumulator_calls("accumulator-before#1('a')")) == ()
    assert tuple(iter_accumulator_calls("accumulator-before($x)")) == (("before", None),)
    assert tuple(iter_accumulator_calls("'accumulator-before(''a'')'")) == ()
    assert tuple(
        iter_accumulator_calls("(: accumulator-before('a') :) accumulator-after('b')")
    ) == (("after", "b"),)
    assert tuple(iter_accumulator_calls("(: outer (: inner :) accumulator-before('a') :)")) == ()
    assert tuple(iter_accumulator_calls("accumulator-before('a''b')")) == (("before", "a'b"),)
    assert tuple(iter_accumulator_calls("myaccumulator-before('a')")) == ()
    assert tuple(iter_accumulator_calls("my:accumulator-before('a')")) == ()
    assert tuple(iter_accumulator_calls("accumulator-before ( 'a' )")) == (("before", "a"),)
    assert tuple(iter_accumulator_calls("accumulator-before('a') + accumulator-after('b')")) == (
        ("before", "a"),
        ("after", "b"),
    )
    assert tuple(iter_accumulator_calls("accumulator-before('unterminated")) == (
        ("before", "unterminated"),
    )
    assert tuple(iter_accumulator_calls("(: unterminated")) == ()


def test_same_context_accumulator_call() -> None:
    def check(source: str, expected: bool) -> None:
        start = source.index("accumulator-")
        assert same_context_accumulator_call(source, start) is expected, source

    check("accumulator-before('a')", True)
    check("x ! accumulator-before('a')", False)
    check("preceding-sibling::n[1][accumulator-before('a')]", False)
    check("preceding-sibling::n[accumulator-before('a')]", False)
    check("ancestor::doc[1][accumulator-before('a')]", False)
    check("text()[accumulator-before('a')]", False)
    check(".[accumulator-before('a')]", True)
    check("..[accumulator-before('a')]", False)
    check("self::n[accumulator-before('a')]", True)
    check("self::*[accumulator-before('a')]", True)
    check("self::node()[accumulator-before('a')]", True)
    check("self::text()[accumulator-before('a')]", True)
    check("self::element()[accumulator-before('a')]", True)
    check("child::node()[accumulator-before('a')]", False)
    check("node()[accumulator-before('a')]", False)
    check(".[preceding-sibling::n[1] ! accumulator-before('a')]", False)
    check(".[preceding-sibling::n[1]/accumulator-before('a')]", False)
    check("self::n[preceding-sibling::n[1] ! accumulator-before('a')]", False)
    check("preceding-sibling::n[1][self::node()[accumulator-before('a')]]", False)
    check("preceding-sibling::n[1][self::element()[accumulator-before('a')]]", False)
    check("preceding-sibling::n[1][self::*[accumulator-before('a')]]", False)
    check("preceding-sibling::n[1][.[accumulator-before('a')]]", False)
    check("self::node()[self::node()[accumulator-before('a')]]", True)
    check(".[self::node()[accumulator-before('a')]]", True)
    check("self::node()[.[accumulator-before('a')]]", True)
    check("$x[accumulator-before('a')]", True)
    check("?key[accumulator-before('a')]", True)
    check("f()[accumulator-before('a')]", True)
    check("(preceding-sibling::n[1])[accumulator-before('a')]", True)
    check("n[1][2][accumulator-before('a')]", False)
    check("attribute::x[accumulator-before('a')]", False)
    check("p:n[accumulator-before('a')]", False)
    check("preceding-sibling::n[1][(: x :) accumulator-before('a')]", False)
    check("preceding-sibling::n[1]['x'][accumulator-before('a')]", False)
    check("::*[accumulator-before('a')]", False)
    check(")[accumulator-before('a')]", True)
    check("][accumulator-before('a')]", True)
    check("[accumulator-before('a')]", True)
    check("$[accumulator-before('a')]", True)


def test_find_cycle() -> None:
    assert find_cycle({}) is None
    assert find_cycle({"a": ()}) is None
    assert find_cycle({"a": ("b",), "b": ()}) is None
    assert find_cycle({"a": ("a",)}) == ("a", "a")
    assert find_cycle({"a": ("b",), "b": ("a",)}) == ("a", "b", "a")
    assert find_cycle({"a": ("b",), "b": ("c",), "c": ()}) is None


def test_accumulator_elements_and_functions_are_available() -> None:
    body = """
        <xsl:template name="main">
          <xsl:sequence select="element-available('xsl:accumulator')"/>
          <xsl:sequence select="element-available('xsl:accumulator-rule')"/>
          <xsl:sequence select="function-available('accumulator-before')"/>
          <xsl:sequence select="function-available('accumulator-after')"/>
        </xsl:template>
    """
    assert values(transform(stylesheet(body), initial_template="main")) == (
        "true",
        "true",
        "true",
        "true",
    )


def test_accumulator_rule_priority_value_is_finite_decimal() -> None:
    rule = AccumulatorRule(phase="start", priority=Decimal("2.5"), order=3)
    assert rule.priority == Decimal("2.5")
    assert rule.order == 3
