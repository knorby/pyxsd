"""Grounded ``xsl:merge`` execution.

The tests exercise multiple merge sources, composite and typed keys,
collations, source-specific actions, empty groups, nested current merge state,
sort-before-merge, and the normative static/dynamic error codes.  Every
stylesheet is inline and every merge operates on grounded XDM sequences.
"""

from __future__ import annotations

import io
from typing import Any
from urllib.parse import urljoin

import pytest

from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.runtime.iteration import (
    MergeGroup,
    MergeItem,
    check_merge_order,
    compare_merge_key_sets,
    merge_groups,
    merge_key_equal,
    resolve_collation,
    sort_merge_input,
)

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
HTML_CASE_INSENSITIVE = (
    "http://www.w3.org/2005/xpath-functions/collation/html-ascii-case-insensitive"
)


def stylesheet(body: str, *, version: str = "3.0") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'version="{version}">{body}</xsl:stylesheet>'
    ).encode()


def compiled(source: bytes, **options: Any):
    return XsltProcessor(**options).compile(io.BytesIO(source))


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
    """An explicit in-memory document policy for grounded merge inputs."""

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
        raise AssertionError("the grounded merge resolver parses admitted bytes itself")


# ---------------------------------------------------------------------------
# Group ordering and state


def test_merge_two_sorted_sources_orders_groups_and_exposes_state() -> None:
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
                <entry key="{current-merge-key()}" pos="{position()}" size="{last()}">
                  <xsl:value-of select="current-merge-group()" separator=","/>
                </entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><a k="1">a1</a><a k="3">a3</a><b k="2">b2</b><b k="4">b4</b></doc>',
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [
        (
            entry.attributes[0].string_value,
            entry.attributes[1].string_value,
            entry.attributes[2].string_value,
            entry.string_value,
        )
        for entry in entries
    ] == [
        ("1", "1", "4", "a1"),
        ("2", "2", "4", "b2"),
        ("3", "3", "4", "a3"),
        ("4", "4", "4", "b4"),
    ]


def test_merge_action_context_item_is_the_first_group_item() -> None:
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
                <entry name="{name()}" first="{current-merge-group()[1] is .}"/>
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
    entry = element_children(result.principal_tree.root_element)[0]
    assert entry.attributes[0].string_value == "a"
    assert entry.attributes[1].string_value == "true"


def test_merge_composite_keys_and_numeric_key_components() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="doc/a">
                <xsl:merge-key select="@date"/>
                <xsl:merge-key select="@seq" data-type="number"/>
              </xsl:merge-source>
              <xsl:merge-source select="doc/b">
                <xsl:merge-key select="@date"/>
                <xsl:merge-key select="@seq" data-type="number"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{string-join(current-merge-key(), '|')}">
                  <xsl:value-of select="current-merge-group()" separator=","/>
                </entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        (
            b'<doc><a date="2020-01-01" seq="10">a1</a>'
            b'<a date="2020-01-02" seq="2">a2</a>'
            b'<b date="2020-01-01" seq="2">b1</b>'
            b'<b date="2020-01-02" seq="10">b2</b></doc>'
        ),
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [(entry.attributes[0].string_value, entry.string_value) for entry in entries] == [
        ("2020-01-01|2", "b1"),
        ("2020-01-01|10", "a1"),
        ("2020-01-02|2", "a2"),
        ("2020-01-02|10", "b2"),
    ]


def test_merge_keys_compare_with_the_selected_collation() -> None:
    body = f"""
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source name="left" select="('A', 'b')">
                <xsl:merge-key select="." collation="{HTML_CASE_INSENSITIVE}"/>
              </xsl:merge-source>
              <xsl:merge-source name="right" select="('a', 'B')">
                <xsl:merge-key select="." collation="{HTML_CASE_INSENSITIVE}"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{{current-merge-key()}}">
                  <xsl:value-of select="current-merge-group()" separator=","/>
                </entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    entries = element_children(result.principal_tree.root_element)
    assert [(entry.attributes[0].string_value, entry.string_value) for entry in entries] == [
        ("A", "A,a"),
        ("b", "b,B"),
    ]


def test_current_merge_key_keeps_the_atomized_type_for_text_data_type() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="doc/n">
                <xsl:merge-key select="xs:date(@d)" data-type="text"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry isdate="{current-merge-key() instance of xs:date}"
                  value="{current-merge-key()}"/>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n d="2020-01-01"/><n d="2020-01-02"/></doc>',
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [
        (entry.attributes[0].string_value, entry.attributes[1].string_value) for entry in entries
    ] == [("true", "2020-01-01"), ("true", "2020-01-02")]


def test_current_merge_key_is_the_first_item_for_equal_numeric_types() -> None:
    cases = (
        ("xs:double(0.1)", "xs:decimal('0.1')", "double"),
        ("xs:decimal('0.1')", "xs:double(0.1)", "decimal"),
    )
    for first, second, expected in cases:
        body = f"""
            <xsl:template name="main">
              <out>
                <xsl:merge>
                  <xsl:merge-source select="{first}">
                    <xsl:merge-key select="."/>
                  </xsl:merge-source>
                  <xsl:merge-source select="{second}">
                    <xsl:merge-key select="."/>
                  </xsl:merge-source>
                  <xsl:merge-action>
                    <entry count="{{count(current-merge-group())}}"
                      type="{{if (current-merge-key() instance of xs:double)
                        then 'double' else 'decimal'}}"/>
                  </xsl:merge-action>
                </xsl:merge>
              </out>
            </xsl:template>
        """
        result = transform(stylesheet(body), initial_template="main")
        entries = element_children(result.principal_tree.root_element)
        assert len(entries) == 1
        assert entries[0].attributes[0].string_value == "2"
        assert entries[0].attributes[1].string_value == expected


def test_merge_order_check_promotes_mixed_numeric_comparison() -> None:
    cases = (
        ("(xs:double(0.1), xs:decimal('0.1'))", "double"),
        ("(xs:decimal('0.1'), xs:double(0.1))", "decimal"),
    )
    for sequence, expected in cases:
        body = f"""
            <xsl:template name="main">
              <out>
                <xsl:merge>
                  <xsl:merge-source select="{sequence}">
                    <xsl:merge-key select="."/>
                  </xsl:merge-source>
                  <xsl:merge-action>
                    <entry count="{{count(current-merge-group())}}"
                      type="{{if (current-merge-key() instance of xs:double)
                        then 'double' else 'decimal'}}"/>
                  </xsl:merge-action>
                </xsl:merge>
              </out>
            </xsl:template>
        """
        result = transform(stylesheet(body), initial_template="main")
        entries = element_children(result.principal_tree.root_element)
        assert len(entries) == 1
        assert entries[0].attributes[0].string_value == "2"
        assert entries[0].attributes[1].string_value == expected


def test_merge_data_type_text_and_untyped_keys_become_strings() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="doc/n">
                <xsl:merge-key select="@k" data-type="text"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}"
                  is-string="{current-merge-key() instance of xs:string}"/>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="10"/><n k="2"/></doc>',
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [entry.attributes[0].string_value for entry in entries] == ["10", "2"]
    assert [entry.attributes[1].string_value for entry in entries] == ["true", "true"]


def test_merge_key_without_select_uses_sequence_constructor_or_context_item() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="doc/a">
                <xsl:merge-key><xsl:sequence select="@k"/></xsl:merge-key>
              </xsl:merge-source>
              <xsl:merge-source select="doc/b">
                <xsl:merge-key/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}">
                  <xsl:value-of select="current-merge-group()" separator=","/>
                </entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><a k="1">a1</a><b>1</b></doc>',
        initial_template="main",
    )
    entry = element_children(result.principal_tree.root_element)[0]
    assert entry.attributes[0].string_value == "1"
    assert entry.string_value == "a1,1"


def test_merge_source_names_distinguish_sources_and_missing_sources_are_empty() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source name="master" select="doc/m">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-source name="update" select="doc/u">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}">
                  <master><xsl:value-of select="current-merge-group('master')"/></master>
                  <update><xsl:value-of select="current-merge-group('update')"/></update>
                </entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><m k="1">m1</m><m k="3">m3</m><u k="2">u2</u><u k="3">u3</u></doc>',
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [
        (
            entry.attributes[0].string_value,
            element_children(entry)[0].string_value,
            element_children(entry)[1].string_value,
        )
        for entry in entries
    ] == [("1", "m1", ""), ("2", "", "u2"), ("3", "m3", "u3")]


def test_merge_unknown_source_name_is_xtde3490() -> None:
    body = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source name="known" select="(1)">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-action>
              <xsl:sequence select="current-merge-group('missing')"/>
            </xsl:merge-action>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTDE3490"


def test_merge_unnamed_sources_are_distinct_in_current_group() -> None:
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
                <entry count="{count(current-merge-group())}">
                  <xsl:value-of select="current-merge-group()" separator=","/>
                </entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><a k="1">a</a><b k="1">b</b></doc>',
        initial_template="main",
    )
    entry = element_children(result.principal_tree.root_element)[0]
    assert entry.attributes[0].string_value == "2"
    assert entry.string_value == "a,b"


def test_merge_empty_inputs_produce_no_groups() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="()">
                <xsl:merge-key select="."/>
              </xsl:merge-source>
              <xsl:merge-source select="()">
                <xsl:merge-key select="."/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry>unexpected</entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert element_children(result.principal_tree.root_element) == ()


def test_merge_empty_source_side_still_forms_groups_from_the_other_source() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source name="empty" select="()">
                <xsl:merge-key select="."/>
              </xsl:merge-source>
              <xsl:merge-source name="full" select="doc/n">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}" empty="{empty(current-merge-group('empty'))}">
                  <xsl:value-of select="current-merge-group()"/>
                </entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="1">n1</n><n k="2">n2</n></doc>',
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [
        (
            entry.attributes[0].string_value,
            entry.attributes[1].string_value,
            entry.string_value,
        )
        for entry in entries
    ] == [("1", "true", "n1"), ("2", "true", "n2")]


def test_merge_single_input_behaves_like_adjacent_grouping() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="doc/n">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}" count="{count(current-merge-group())}"/>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="1"/><n k="1"/><n k="2"/></doc>',
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [
        (entry.attributes[0].string_value, entry.attributes[1].string_value) for entry in entries
    ] == [("1", "2"), ("2", "1")]


def test_merge_preserves_duplicate_items_in_one_group() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="(doc/n[1], doc/n[1])">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry count="{count(current-merge-group())}"/>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), b'<doc><n k="1"/></doc>', initial_template="main")
    entry = element_children(result.principal_tree.root_element)[0]
    assert entry.attributes[0].string_value == "2"


def test_merge_descending_order_is_honored_across_sources() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="doc/a">
                <xsl:merge-key select="@k" order="descending"/>
              </xsl:merge-source>
              <xsl:merge-source select="doc/b">
                <xsl:merge-key select="@k" order="descending"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}"/>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><a k="4"/><a k="2"/><b k="3"/><b k="1"/></doc>',
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [entry.attributes[0].string_value for entry in entries] == ["4", "3", "2", "1"]


# ---------------------------------------------------------------------------
# Sort-before-merge and source selection


def test_merge_sort_before_merge_sorts_each_input_sequence() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="doc/n" sort-before-merge="yes">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}"/>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="3"/><n k="1"/><n k="2"/></doc>',
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [entry.attributes[0].string_value for entry in entries] == ["1", "2", "3"]


def test_sort_before_merge_uses_merge_wide_promoted_numeric_order() -> None:
    sort_source = "(xs:float(1), xs:decimal('0.1000000001'), xs:decimal('0.1'))"
    cases = (
        (
            f"""
            <xsl:merge-source select="{sort_source}" sort-before-merge="yes">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-source select="xs:double(2)">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            """
        ),
        (
            f"""
            <xsl:merge-source select="xs:double(2)">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-source select="{sort_source}" sort-before-merge="yes">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            """
        ),
    )
    for sources in cases:
        body = f"""
            <xsl:template name="main">
              <out>
                <xsl:merge>
                  {sources}
                  <xsl:merge-action>
                    <entry value="{{current-merge-key()}}"
                      type="{{if (current-merge-key() instance of xs:double)
                        then 'double'
                        else if (current-merge-key() instance of xs:float)
                        then 'float' else 'decimal'}}"/>
                  </xsl:merge-action>
                </xsl:merge>
              </out>
            </xsl:template>
        """
        result = transform(stylesheet(body), initial_template="main")
        entries = element_children(result.principal_tree.root_element)
        assert [
            (entry.attributes[0].string_value, entry.attributes[1].string_value)
            for entry in entries
        ] == [
            ("0.1", "decimal"),
            ("0.1000000001", "decimal"),
            ("1.0", "float"),
            ("2.0", "double"),
        ]


def test_sort_merge_input_accepts_explicit_promoted_comparison_keys() -> None:
    from pyxsd.xslt.runtime.iteration import ResolvedSortKey

    keys = (ResolvedSortKey(),)
    promoted = (ResolvedSortKey(numeric_mode="double"),)
    items = (
        MergeItem(XdmAtomic.float(1.0), 0, 0, 1, ((XdmAtomic.float(1.0),),)),
        MergeItem(
            XdmAtomic.decimal("0.1000000001"),
            0,
            0,
            2,
            ((XdmAtomic.decimal("0.1000000001"),),),
        ),
        MergeItem(XdmAtomic.decimal("0.1"), 0, 0, 3, ((XdmAtomic.decimal("0.1"),),)),
    )
    ordered = sort_merge_input(items, keys, comparison_keys=promoted)
    assert [item.item.lexical for item in ordered] == ["0.1", "0.1000000001", "1.0"]


def test_merge_unsorted_input_is_xtde2220() -> None:
    body = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="doc/n">
              <xsl:merge-key select="@k"/>
            </xsl:merge-source>
            <xsl:merge-action>
              <entry key="{current-merge-key()}"/>
            </xsl:merge-action>
          </xsl:merge>
        </xsl:template>
    """
    assert (
        error_code(
            lambda: transform(
                stylesheet(body),
                b'<doc><n k="2"/><n k="1"/></doc>',
                initial_template="main",
            )
        )
        == "XTDE2220"
    )


def test_merge_for_each_item_uses_anchor_focus_and_order() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source for-each-item="doc/g"
                select="if (position() = 1) then n[1] else n[2]">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-source for-each-item="doc/g"
                select="if (last() = 2) then n[2] else n[1]">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}" count="{count(current-merge-group())}">
                  <xsl:value-of select="current-merge-group()/@k" separator=","/>
                </entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><g><n k="1" p="1"/><n k="9" p="2"/></g>'
        b'<g><n k="2" p="1"/><n k="8" p="2"/></g></doc>',
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [
        (entry.attributes[0].string_value, entry.attributes[1].string_value) for entry in entries
    ] == [("1", "1"), ("8", "2"), ("9", "1")]


def test_merge_for_each_source_reads_documents_through_the_resource_policy() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source for-each-source="('urn:a', 'urn:b')" select="/doc/n"
                streamable="yes">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}" count="{count(current-merge-group())}"
                  size="{last()}"/>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    policy = MappingPolicy(
        {
            "urn:a": b'<doc><n k="1"/><n k="3"/></doc>',
            "urn:b": b'<doc><n k="2"/><n k="3"/></doc>',
        }
    )
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    entries = element_children(result.principal_tree.root_element)
    assert [
        (
            entry.attributes[0].string_value,
            entry.attributes[1].string_value,
            entry.attributes[2].string_value,
        )
        for entry in entries
    ] == [("1", "1", "3"), ("2", "1", "3"), ("3", "2", "3")]
    assert ("urn:a", None, "instance") in policy.reads
    assert ("urn:b", None, "instance") in policy.reads


def test_merge_source_select_uses_the_containing_focus_and_variables() -> None:
    body = """
        <xsl:template name="main">
          <xsl:variable name="nodes" select="doc/n"/>
          <out>
            <xsl:merge>
              <xsl:merge-source select="$nodes">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}"><xsl:value-of select="."/></entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="1">one</n><n k="2">two</n></doc>',
        initial_template="main",
    )
    entries = element_children(result.principal_tree.root_element)
    assert [(entry.attributes[0].string_value, entry.string_value) for entry in entries] == [
        ("1", "one"),
        ("2", "two"),
    ]


# ---------------------------------------------------------------------------
# Current merge state transitions


def test_nested_merge_shadows_and_restores_current_state() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source name="left" select="doc/l">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-source name="right" select="doc/r">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <outer key="{current-merge-key()}" count="{count(current-merge-group())}">
                  <xsl:merge>
                    <xsl:merge-source select="current-merge-group('left')/@k">
                      <xsl:merge-key select="."/>
                    </xsl:merge-source>
                    <xsl:merge-action>
                      <inner key="{current-merge-key()}"
                        count="{count(current-merge-group())}"/>
                    </xsl:merge-action>
                  </xsl:merge>
                  <after key="{current-merge-key()}" count="{count(current-merge-group())}"/>
                </outer>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><l k="1"/><l k="2"/><r k="1"/><r k="2"/></doc>',
        initial_template="main",
    )
    outer = element_children(result.principal_tree.root_element)
    assert [node.attributes[0].string_value for node in outer] == ["1", "2"]
    for node in outer:
        inner, after = element_children(node)
        assert inner.attributes[0].string_value == node.attributes[0].string_value
        assert inner.attributes[1].string_value == "1"
        assert after.attributes[0].string_value == node.attributes[0].string_value
        assert after.attributes[1].string_value == "2"


def test_merge_state_is_absent_outside_an_action() -> None:
    group = """
        <xsl:template name="main">
          <xsl:sequence select="current-merge-group()"/>
        </xsl:template>
    """
    key = """
        <xsl:template name="main">
          <xsl:sequence select="current-merge-key()"/>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(group), initial_template="main")) == "XTDE3480"
    assert error_code(lambda: transform(stylesheet(key), initial_template="main")) == "XTDE3510"


def test_merge_state_is_cleared_by_a_template_call() -> None:
    body = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="(1)">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-action>
              <xsl:call-template name="show"/>
            </xsl:merge-action>
          </xsl:merge>
        </xsl:template>
        <xsl:template name="show">
          <xsl:sequence select="current-merge-group()"/>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTDE3480"


def test_merge_state_is_absent_during_global_and_key_evaluation() -> None:
    global_variable = """
        <xsl:variable name="group" select="current-merge-group()"/>
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="(1)"><xsl:merge-key select="."/></xsl:merge-source>
            <xsl:merge-action><xsl:sequence select="$group"/></xsl:merge-action>
          </xsl:merge>
        </xsl:template>
    """
    assert (
        error_code(lambda: transform(stylesheet(global_variable), initial_template="main"))
        == "XTDE3480"
    )

    key_use = """
        <xsl:key name="k" match="n" use="current-merge-group()"/>
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="doc/n"><xsl:merge-key select="@k"/></xsl:merge-source>
              <xsl:merge-action>
                <xsl:value-of select="count(key('k', 'x'))"/>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    assert (
        error_code(
            lambda: transform(
                stylesheet(key_use),
                b'<doc><n k="1"/></doc>',
                initial_template="main",
            )
        )
        == "XTDE3480"
    )


def test_merge_state_survives_for_each_and_conditional_instructions() -> None:
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
                <xsl:for-each select="current-merge-group()">
                  <seen key="{current-merge-key()}" count="{count(current-merge-group())}"/>
                </xsl:for-each>
                <xsl:if test="count(current-merge-group()) = 2">
                  <paired key="{current-merge-key()}"/>
                </xsl:if>
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
    seen = element_children(result.principal_tree.root_element)
    assert [node.attributes[0].string_value for node in seen] == ["1", "1", "1"]
    assert seen[2].local_name == "paired"


# ---------------------------------------------------------------------------
# Static and dynamic errors


def test_merge_key_count_mismatch_is_xtse2200() -> None:
    body = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="(1)">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-source select="(1)">
              <xsl:merge-key select="."/>
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(body))) == "XTSE2200"


def test_merge_key_attribute_mismatch_is_xtde2210() -> None:
    order = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="(1)">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-source select="(1)">
              <xsl:merge-key select="." order="descending"/>
            </xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(order), initial_template="main")) == "XTDE2210"

    collation = f"""
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="('a')">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-source select="('a')">
              <xsl:merge-key select="." collation="{HTML_CASE_INSENSITIVE}"/>
            </xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert (
        error_code(lambda: transform(stylesheet(collation), initial_template="main")) == "XTDE2210"
    )


def test_merge_incomparable_keys_are_xtte2230() -> None:
    body = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="(1)">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-source select="('a')">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTTE2230"


def test_merge_key_sequence_with_multiple_items_is_xtte1020() -> None:
    body = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="doc/n">
              <xsl:merge-key select="(@a, @b)"/>
            </xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert (
        error_code(
            lambda: transform(
                stylesheet(body),
                b'<doc><n a="1" b="2"/></doc>',
                initial_template="main",
            )
        )
        == "XTTE1020"
    )


def test_merge_duplicate_source_names_are_xtse3190() -> None:
    body = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source name="same" select="(1)">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-source name="same" select="(2)">
              <xsl:merge-key select="."/>
            </xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(body))) == "XTSE3190"


def test_merge_structural_static_errors() -> None:
    cases = (
        "<xsl:template name='main'><xsl:merge><xsl:merge-action/></xsl:merge></xsl:template>",
        (
            "<xsl:template name='main'><xsl:merge>"
            "<xsl:merge-source select='(1)'><xsl:merge-key select='.'/></xsl:merge-source>"
            "</xsl:merge></xsl:template>"
        ),
        (
            "<xsl:template name='main'><xsl:merge>"
            "<xsl:merge-source select='(1)'/>"
            "<xsl:merge-action/></xsl:merge></xsl:template>"
        ),
        (
            "<xsl:template name='main'><xsl:merge>"
            "<xsl:merge-source><xsl:merge-key select='.'/></xsl:merge-source>"
            "<xsl:merge-action/></xsl:merge></xsl:template>"
        ),
        "<xsl:template name='main'><xsl:merge-source select='(1)'/></xsl:template>",
        "<xsl:template name='main'><xsl:merge-key select='.'/></xsl:template>",
        "<xsl:template name='main'><xsl:merge-action/></xsl:template>",
    )
    for body in cases:
        assert error_code(lambda body=body: compiled(stylesheet(body))) == "XTSE0010", body


def test_merge_content_model_and_fallback_handling() -> None:
    text = """
        <xsl:template name="main">
          <xsl:merge>text
            <xsl:merge-source select="(1)"><xsl:merge-key select="."/></xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(text))) == "XTSE0010"

    invalid_child = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:sequence select="1"/>
            <xsl:merge-source select="(1)"><xsl:merge-key select="."/></xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(invalid_child))) == "XTSE0010"

    after_action = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-action/>
            <xsl:merge-source select="(1)"><xsl:merge-key select="."/></xsl:merge-source>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(after_action))) == "XTSE0010"

    two_actions = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="(1)"><xsl:merge-key select="."/></xsl:merge-source>
            <xsl:merge-action/>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(two_actions))) == "XTSE0010"

    with_fallback = """
        <xsl:template name="main">
          <out>
            <xsl:merge xml:space="preserve">
              <xsl:fallback><xsl:sequence select="'fallback'"/></xsl:fallback>
              <xsl:merge-source select="(1)"><xsl:merge-key select="."/></xsl:merge-source>
              <xsl:merge-action><xsl:value-of select="."/></xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(with_fallback), initial_template="main")
    assert result.principal_tree.string_value == "1"


def test_merge_source_content_model_errors_and_fallback_handling() -> None:
    invalid_name = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source name="a:b" select="(1)"><xsl:merge-key select="."/></xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(invalid_name))) == "XTSE0010"

    text = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="(1)">text<xsl:merge-key select="."/></xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(text))) == "XTSE0010"

    invalid_child = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="(1)">
              <xsl:sequence select="1"/>
            </xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(invalid_child))) == "XTSE0010"

    with_fallback = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="(1)" xml:space="preserve">
                <xsl:fallback><xsl:sequence select="'fallback'"/></xsl:fallback>
                <xsl:merge-key select="."/>
              </xsl:merge-source>
              <xsl:merge-action><xsl:value-of select="."/></xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(with_fallback), initial_template="main")
    assert result.principal_tree.string_value == "1"


def test_merge_key_stable_attribute_is_xtse0010() -> None:
    body = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="(1)">
              <xsl:merge-key select="." stable="yes"/>
            </xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(body))) == "XTSE0010"


def test_merge_empty_key_sequences_group_together() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source select="doc/n">
                <xsl:merge-key select="@missing"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry count="{count(current-merge-group())}"
                  key-count="{count(current-merge-key())}"/>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b"<doc><n/><n/></doc>",
        initial_template="main",
    )
    entry = element_children(result.principal_tree.root_element)[0]
    assert entry.attributes[0].string_value == "2"
    assert entry.attributes[1].string_value == "0"


def test_merge_ignores_preserved_whitespace_text_nodes() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0" xml:space="preserve">'
        '<xsl:template name="main"><out><xsl:merge> '
        '<xsl:merge-source select="(1)"> <xsl:merge-key select="."/> </xsl:merge-source> '
        '<xsl:merge-action> <xsl:value-of select="."/> </xsl:merge-action> '
        "</xsl:merge></out></xsl:template></xsl:stylesheet>"
    ).encode()
    result = transform(source, initial_template="main")
    assert result.principal_tree.string_value.strip() == "1"


def test_merge_key_select_with_content_is_xtse3200() -> None:
    body = """
        <xsl:template name="main">
          <xsl:merge>
            <xsl:merge-source select="(1)">
              <xsl:merge-key select=".">
                <xsl:sequence select="1"/>
              </xsl:merge-key>
            </xsl:merge-source>
            <xsl:merge-action/>
          </xsl:merge>
        </xsl:template>
    """
    assert error_code(lambda: compiled(stylesheet(body))) == "XTSE3200"


def test_merge_source_attribute_combinations_are_static_errors() -> None:
    cases = (
        (
            "<xsl:template name='main'><xsl:merge>"
            "<xsl:merge-source for-each-item='(1)' select='.' streamable='yes'>"
            "<xsl:merge-key select='.'/></xsl:merge-source>"
            "<xsl:merge-action/></xsl:merge></xsl:template>",
            "XTSE3195",
        ),
        (
            "<xsl:template name='main'><xsl:merge>"
            "<xsl:merge-source for-each-item='(1)' for-each-source=\"('urn:a')\" select='.'>"
            "<xsl:merge-key select='.'/></xsl:merge-source>"
            "<xsl:merge-action/></xsl:merge></xsl:template>",
            "XTSE3195",
        ),
        (
            "<xsl:template name='main'><xsl:merge>"
            "<xsl:merge-source select='(1)' use-accumulators='acc'>"
            "<xsl:merge-key select='.'/></xsl:merge-source>"
            "<xsl:merge-action/></xsl:merge></xsl:template>",
            "XTSE3195",
        ),
        (
            "<xsl:template name='main'><xsl:merge>"
            "<xsl:merge-source select='(1)' validation='strip'>"
            "<xsl:merge-key select='.'/></xsl:merge-source>"
            "<xsl:merge-action/></xsl:merge></xsl:template>",
            "XTSE0010",
        ),
        (
            "<xsl:template name='main'><xsl:merge>"
            "<xsl:merge-source for-each-source=\"('urn:a')\" select='.'"
            " validation='strip' type='xs:anyType'>"
            "<xsl:merge-key select='.'/></xsl:merge-source>"
            "<xsl:merge-action/></xsl:merge></xsl:template>",
            "XTSE1505",
        ),
        (
            "<xsl:template name='main'><xsl:merge>"
            "<xsl:merge-source select='(1)' validation='bogus'>"
            "<xsl:merge-key select='.'/></xsl:merge-source>"
            "<xsl:merge-action/></xsl:merge></xsl:template>",
            "XTSE0020",
        ),
        (
            "<xsl:template name='main'><xsl:merge>"
            "<xsl:merge-source select='(1)' streamable='bogus'>"
            "<xsl:merge-key select='.'/></xsl:merge-source>"
            "<xsl:merge-action/></xsl:merge></xsl:template>",
            "XTSE0020",
        ),
    )
    for body, code in cases:
        assert error_code(lambda body=body: compiled(stylesheet(body))) == code, body


def test_merge_for_each_source_accepts_validation_options() -> None:
    body = """
        <xsl:accumulator name="acc" as="xs:integer" initial-value="0" streamable="yes">
          <xsl:accumulator-rule match="n" select="$value + 1"/>
        </xsl:accumulator>
        <xsl:template name="main">
          <out>
            <xsl:merge>
              <xsl:merge-source for-each-source="('urn:a')" select="/doc/n"
                validation="strip" use-accumulators="acc">
                <xsl:merge-key select="@k"/>
              </xsl:merge-source>
              <xsl:merge-action>
                <entry key="{current-merge-key()}"><xsl:value-of select="."/></entry>
              </xsl:merge-action>
            </xsl:merge>
          </out>
        </xsl:template>
    """
    policy = MappingPolicy({"urn:a": b'<doc><n k="1">one</n></doc>'})
    result = compiled(stylesheet(body), resource_policy=policy).transform(
        delivery="document", initial_template="main"
    )
    entry = element_children(result.principal_tree.root_element)[0]
    assert entry.attributes[0].string_value == "1"
    assert entry.string_value == "one"


# ---------------------------------------------------------------------------
# Merge algorithm value contracts


def _item(
    value: int,
    key: str,
    *,
    source: int = 0,
    anchor: int = 0,
    position: int = 1,
) -> MergeItem:
    return MergeItem(
        XdmAtomic.integer(value),
        source,
        anchor,
        position,
        ((XdmAtomic.string(key),),),
    )


def test_merge_groups_value_contract() -> None:
    item = XdmAtomic.integer(1)
    key = ((XdmAtomic.string("k"),),)
    group = MergeGroup((MergeItem(item, 0, 0, 1, key),), key)
    assert group.items[0].item is item
    with pytest.raises(ValueError, match="never empty"):
        MergeGroup((), key)
    with pytest.raises(ValueError, match="source"):
        MergeItem(item, -1, 0, 1, key)
    with pytest.raises(ValueError, match="key"):
        MergeItem(item, 0, 0, 1, ((), object()))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="XDM item"):
        MergeItem(object(), 0, 0, 1, key)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tuple"):
        MergeItem(item, 0, 0, 1, [])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="merge items"):
        MergeGroup((object(),), key)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tuple"):
        MergeGroup((MergeItem(item, 0, 0, 1, key),), [])  # type: ignore[arg-type]


def test_merge_groups_order_across_inputs_and_empty_inputs() -> None:
    from pyxsd.xslt.runtime.iteration import ResolvedSortKey

    collation = resolve_collation(None, code="XTDE1035")
    keys = (ResolvedSortKey(collation=collation),)
    groups = merge_groups(
        (
            (_item(1, "a", position=1), _item(2, "c", position=2)),
            (),
            (_item(3, "a", source=2, position=1), _item(4, "b", source=2, position=2)),
        ),
        keys,
    )
    assert [
        (group.key[0][0].lexical, [item.item.value for item in group.items]) for group in groups
    ] == [
        ("a", [1, 3]),
        ("b", [4]),
        ("c", [2]),
    ]
    assert groups[0].items[0].source == 0
    assert groups[0].items[1].source == 2


def test_merge_order_checks_and_sorting_helpers() -> None:
    from pyxsd.xslt.runtime.iteration import ResolvedSortKey

    collation = resolve_collation(None, code="XTDE1035")
    keys = (ResolvedSortKey(collation=collation),)
    sorted_items = (_item(1, "a"), _item(2, "b"))
    check_merge_order(sorted_items, keys)
    with pytest.raises(XsltError) as caught:
        check_merge_order((_item(1, "b"), _item(2, "a")), keys)
    assert caught.value.code == "XTDE2220"
    assert [item.item.value for item in sort_merge_input((_item(1, "b"), _item(2, "a")), keys)] == [
        2,
        1,
    ]


def test_merge_order_check_promotes_mixed_numeric_values_directly() -> None:
    from pyxsd.xslt.runtime.iteration import ResolvedSortKey

    keys = (ResolvedSortKey(),)
    double = MergeItem(XdmAtomic.double(0.1), 0, 0, 1, ((XdmAtomic.double(0.1),),))
    decimal = MergeItem(XdmAtomic.decimal("0.1"), 0, 0, 2, ((XdmAtomic.decimal("0.1"),),))
    check_merge_order((double, decimal), keys)
    check_merge_order((decimal, double), keys)


def test_merge_key_comparison_maps_incomparable_values_to_xtte2230() -> None:
    from pyxsd.xslt.runtime.iteration import ResolvedSortKey

    keys = (ResolvedSortKey(),)
    with pytest.raises(XsltError) as caught:
        compare_merge_key_sets(((XdmAtomic.integer(1),),), ((XdmAtomic.string("a"),),), keys)
    assert caught.value.code == "XTTE2230"
    assert merge_key_equal(
        ((XdmAtomic.integer(1),),),
        ((XdmAtomic.integer(1),),),
        keys,
    )
    assert not merge_key_equal(((XdmAtomic.integer(1),),), (), keys)
    with pytest.raises(XsltError) as caught:
        compare_merge_key_sets(
            ((XdmAtomic.integer(1), XdmAtomic.integer(2)),),
            ((XdmAtomic.integer(1),),),
            keys,
        )
    assert caught.value.code == "XTTE1020"


def test_merge_groups_rejects_mismatched_key_specifications() -> None:
    from pyxsd.xslt.runtime.iteration import ResolvedSortKey

    keys = (ResolvedSortKey(),)
    with pytest.raises(ValueError, match="key specification"):
        merge_groups(((_item(1, "a"),),), keys + keys)
