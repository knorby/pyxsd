"""Grouping instructions and current-group state.

The tests cover all four ``xsl:for-each-group`` algorithms, composite and
multi-valued keys, collation equality, group ordering/sorting, nested state
isolation, and the normative error codes.  Every stylesheet is inline.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.runtime.iteration import (
    Completion,
    Group,
    IterationItem,
    IterationState,
    atomic_equal,
    deep_equal_sequences,
    group_by,
    resolve_collation,
)

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"
HTML_CASE_INSENSITIVE = (
    "http://www.w3.org/2005/xpath-functions/collation/html-ascii-case-insensitive"
)
UCA = "http://www.w3.org/2013/collation/UCA"


def stylesheet(body: str, *, version: str = "3.0") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="{version}">{body}</xsl:stylesheet>'
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


# ---------------------------------------------------------------------------
# group-by


def test_group_by_first_appearance_and_current_group() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-by="@k">
              <g key="{current-grouping-key()}" pos="{position()}" size="{last()}">
                <xsl:value-of select="current-group()" separator=","/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="b">1</n><n k="a">2</n><n k="b">3</n></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [group.string_value for group in groups] == ["1,3", "2"]
    assert [
        (
            group.attributes[0].string_value,
            group.attributes[1].string_value,
            group.attributes[2].string_value,
        )
        for group in groups
    ] == [
        ("b", "1", "2"),
        ("a", "2", "2"),
    ]


def test_group_by_supports_multiple_keys_per_item() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-by="(@a, @b)">
              <g key="{current-grouping-key()}">
                <xsl:value-of select="current-group()/@id" separator=","/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n id="1" a="x" b="y"/><n id="2" a="y"/><n id="3" a="x"/></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [(group.attributes[0].string_value, group.string_value) for group in groups] == [
        ("x", "1,3"),
        ("y", "1,2"),
    ]


def test_group_by_deduplicates_keys_within_one_item_and_allows_no_keys() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-by="(@a, @a)">
              <g key="{current-grouping-key()}">
                <xsl:value-of select="current-group()/@id" separator=","/>
              </g>
            </xsl:for-each-group>
            <xsl:for-each-group select="doc/n" group-by="()">
              <g>unexpected</g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n id="1" a="x"/><n id="2" a="x"/></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [(group.attributes[0].string_value, group.string_value) for group in groups] == [
        ("x", "1,2")
    ]


def test_group_by_composite_keys_compare_full_sequences() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-by="(@a, @b)" composite="yes">
              <g key="{string-join(current-grouping-key(), '/')}">
                <xsl:value-of select="current-group()/@id" separator=","/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n id="1" a="x" b="y"/><n id="2" a="x" b="y"/><n id="3" a="x"/></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [(group.attributes[0].string_value, group.string_value) for group in groups] == [
        ("x/y", "1,2"),
        ("x", "3"),
    ]


def test_group_by_composite_empty_key_is_a_real_group() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="(1, 2, 3)" group-by="()" composite="yes">
              <g key="{count(current-grouping-key())}">
                <xsl:value-of select="current-group()" separator=","/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    groups = element_children(result.principal_tree.root_element)
    assert len(groups) == 1
    assert groups[0].attributes[0].string_value == "0"
    assert groups[0].string_value == "1,2,3"


def test_group_by_numeric_promotion_and_nan_equality() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="(1, 2, 3)" group-by="xs:double('NaN')">
              <g><xsl:value-of select="current-group()" separator=","/></g>
            </xsl:for-each-group>
            <xsl:for-each-group select="(xs:integer(1), xs:double(1), xs:decimal('1.0'))"
              group-by=".">
              <g count="{count(current-group())}"/>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    groups = element_children(result.principal_tree.root_element)
    assert [group.string_value for group in groups] == ["1,2,3", ""]
    assert groups[1].attributes[0].string_value == "3"


def test_group_by_untyped_values_are_cast_to_strings() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-by="@k">
              <g key="{current-grouping-key()}" type="{current-grouping-key() instance of xs:string}">
                <xsl:value-of select="current-group()/@id" separator=","/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n id="1" k="1"/><n id="2" k="1"/></doc>',
        initial_template="main",
    )
    group = element_children(result.principal_tree.root_element)[0]
    assert group.attributes[1].string_value == "true"
    assert group.string_value == "1,2"


# ---------------------------------------------------------------------------
# group-adjacent


def test_group_adjacent_only_combines_neighbouring_equal_keys() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-adjacent="@k">
              <g key="{current-grouping-key()}">
                <xsl:value-of select="current-group()" separator=","/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="a">1</n><n k="a">2</n><n k="b">3</n><n k="a">4</n></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [(group.attributes[0].string_value, group.string_value) for group in groups] == [
        ("a", "1,2"),
        ("b", "3"),
        ("a", "4"),
    ]


def test_group_adjacent_requires_exactly_one_key() -> None:
    body = """
        <xsl:template name="main">
          <xsl:for-each-group select="(1, 2)" group-adjacent="()">
            <g/>
          </xsl:for-each-group>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTTE1100"

    multiple = """
        <xsl:template name="main">
          <xsl:for-each-group select="(1, 2)" group-adjacent="(., .)">
            <g/>
          </xsl:for-each-group>
        </xsl:template>
    """
    assert (
        error_code(lambda: transform(stylesheet(multiple), initial_template="main")) == "XTTE1100"
    )


def test_group_adjacent_composite_accepts_sequences_and_empty_keys() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="(1, 2, 3)" group-adjacent="(., .)" composite="yes">
              <g key="{count(current-grouping-key())}">
                <xsl:value-of select="current-group()" separator=","/>
              </g>
            </xsl:for-each-group>
            <xsl:for-each-group select="(1, 2)" group-adjacent="()" composite="yes">
              <g key="{count(current-grouping-key())}">
                <xsl:value-of select="current-group()" separator=","/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    groups = element_children(result.principal_tree.root_element)
    assert [group.string_value for group in groups] == ["1", "2", "3", "1,2"]
    assert [group.attributes[0].string_value for group in groups] == ["2", "2", "2", "0"]


# ---------------------------------------------------------------------------
# group-starting-with and group-ending-with


def test_group_starting_with_creates_groups_at_matching_items() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-starting-with="n[@start]">
              <g><xsl:value-of select="current-group()" separator=","/></g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n>0</n><n start="1">1</n><n>2</n><n start="1">3</n><n>4</n></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [group.string_value for group in groups] == ["0", "1,2", "3,4"]


def test_group_ending_with_closes_groups_after_matching_items() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-ending-with="n[@end]">
              <g><xsl:value-of select="current-group()" separator=","/></g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n>1</n><n end="1">2</n><n>3</n><n>4</n><n end="1">5</n></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [group.string_value for group in groups] == ["1,2", "3,4,5"]


def test_start_and_end_grouping_have_no_current_grouping_key() -> None:
    for algorithm in ("group-starting-with", "group-ending-with"):
        body = f"""
            <xsl:template name="main">
              <out>
                <xsl:for-each-group select="doc/n" {algorithm}="n">
                  <g><xsl:sequence select="current-grouping-key()"/></g>
                </xsl:for-each-group>
              </out>
            </xsl:template>
        """
        assert (
            error_code(
                lambda body=body: transform(
                    stylesheet(body),
                    b"<doc><n>1</n></doc>",
                    initial_template="main",
                )
            )
            == "XTDE1071"
        )


def test_start_and_end_grouping_still_expose_current_group() -> None:
    cases = (
        ("group-starting-with", "n[@start]"),
        ("group-ending-with", "n[@end]"),
    )
    for algorithm, pattern in cases:
        body = f"""
            <xsl:template name="main">
              <out>
                <xsl:for-each-group select="doc/n" {algorithm}="{pattern}">
                  <g><xsl:value-of select="current-group()" separator=","/></g>
                </xsl:for-each-group>
              </out>
            </xsl:template>
        """
        result = transform(
            stylesheet(body),
            b"<doc><n>1</n><n>2</n></doc>",
            initial_template="main",
        )
        groups = element_children(result.principal_tree.root_element)
        assert [group.string_value for group in groups] == ["1,2"]


# ---------------------------------------------------------------------------
# Ordering, focus, and nested group state


def test_groups_are_sorted_by_current_group_and_key() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-by="@k">
              <xsl:sort select="count(current-group())" data-type="number" order="descending"/>
              <xsl:sort select="current-grouping-key()" data-type="text"/>
              <g key="{current-grouping-key()}" pos="{position()}" size="{last()}">
                <xsl:value-of select="count(current-group())"/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="c"/><n k="a"/><n k="b"/><n k="a"/><n k="a"/></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [
        (
            group.attributes[0].string_value,
            group.attributes[1].string_value,
            group.attributes[2].string_value,
            group.string_value,
        )
        for group in groups
    ] == [
        ("a", "1", "3", "3"),
        ("b", "2", "3", "1"),
        ("c", "3", "3", "1"),
    ]


def test_group_sort_uses_outer_focus_for_avts() -> None:
    body = """
        <xsl:template name="main">
          <xsl:variable name="order" select="'descending'"/>
          <out>
            <xsl:for-each-group select="doc/n" group-by="@k">
              <xsl:sort select="current-grouping-key()" data-type="text" order="{$order}"/>
              <g key="{current-grouping-key()}"/>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="a"/><n k="c"/><n k="b"/></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [group.attributes[0].string_value for group in groups] == ["c", "b", "a"]


def test_nested_groups_isolate_and_restore_state() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-by="@k">
              <outer key="{current-grouping-key()}">
                <xsl:for-each-group select="current-group()" group-by="@k">
                  <inner key="{current-grouping-key()}">
                    <xsl:value-of select="count(current-group())"/>
                  </inner>
                </xsl:for-each-group>
                <after key="{current-grouping-key()}" count="{count(current-group())}"/>
              </outer>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="a"/><n k="b"/><n k="a"/></doc>',
        initial_template="main",
    )
    outer = element_children(result.principal_tree.root_element)
    assert [node.attributes[0].string_value for node in outer] == ["a", "b"]
    for node in outer:
        inner, after = element_children(node)
        assert inner.attributes[0].string_value == node.attributes[0].string_value
        assert after.attributes[0].string_value == node.attributes[0].string_value
        expected_count = "2" if node.attributes[0].string_value == "a" else "1"
        assert after.attributes[1].string_value == expected_count


def test_nested_for_each_preserves_the_enclosing_current_group() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-by="@k">
              <g key="{current-grouping-key()}">
                <xsl:for-each select="current-group()">
                  <xsl:sequence select="count(current-group())"/>
                </xsl:for-each>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="a"/><n k="a"/></doc>',
        initial_template="main",
    )
    group = element_children(result.principal_tree.root_element)[0]
    assert group.string_value == "2 2"


def test_empty_population_produces_no_groups_or_body_output() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="()" group-by=".">
              <g>unexpected</g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert element_children(result.principal_tree.root_element) == ()


def test_on_completion_retains_the_enclosing_group_and_grouping_key() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-by="@k">
              <g>
                <xsl:iterate select="()">
                  <xsl:on-completion>
                    <xsl:call-template name="show-group"/>
                  </xsl:on-completion>
                </xsl:iterate>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
        <xsl:template name="show-group">
          <shown count="{count(current-group())}" key="{current-grouping-key()}"/>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="a"/><n k="a"/></doc>',
        initial_template="main",
    )
    shown = element_children(element_children(result.principal_tree.root_element)[0])[0]
    assert shown.attributes[0].string_value == "2"
    assert shown.attributes[1].string_value == "a"


def test_on_completion_retains_group_state_inside_perform_sort() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-by="@k">
              <xsl:iterate select="()">
                <xsl:on-completion>
                  <xsl:perform-sort select="current-group()">
                    <xsl:sort select="@id" data-type="number"/>
                  </xsl:perform-sort>
                </xsl:on-completion>
              </xsl:iterate>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n k="a" id="2"/><n k="a" id="1"/></doc>',
        initial_template="main",
    )
    assert [
        next(attribute.string_value for attribute in child.attributes if attribute.name == "id")
        for child in result.principal_tree.root_element.children
    ] == ["1", "2"]


# ---------------------------------------------------------------------------
# Collations


def test_grouping_collation_merges_case_variants() -> None:
    body = f"""
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="('A', 'a', 'B', 'b')" group-by="."
              collation="{HTML_CASE_INSENSITIVE}">
              <g key="{{current-grouping-key()}}">
                <xsl:value-of select="current-group()" separator=","/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    groups = element_children(result.principal_tree.root_element)
    assert [(group.attributes[0].string_value, group.string_value) for group in groups] == [
        ("A", "A,a"),
        ("B", "B,b"),
    ]


def test_grouping_uses_the_instruction_default_collation() -> None:
    body = f"""
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="('A', 'a', 'B')" group-by="."
              default-collation="{HTML_CASE_INSENSITIVE}">
              <g key="{{current-grouping-key()}}">
                <xsl:value-of select="current-group()" separator=","/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    groups = element_children(result.principal_tree.root_element)
    assert [group.string_value for group in groups] == ["A,a", "B"]


def test_uca_primary_strength_ignores_case_and_accents_for_grouping() -> None:
    body = f"""
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="('a', 'A', 'á', 'b')" group-by="."
              collation="{UCA}?lang=en;strength=primary">
              <g key="{{current-grouping-key()}}">
                <xsl:value-of select="current-group()" separator=","/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    groups = element_children(result.principal_tree.root_element)
    assert [group.string_value for group in groups] == ["a,A,á", "b"]


def test_unknown_grouping_collation_is_xtde1110() -> None:
    body = """
        <xsl:template name="main">
          <xsl:for-each-group select="(1)" group-by="." collation="urn:unknown:collation">
            <g/>
          </xsl:for-each-group>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTDE1110"


def test_grouping_collation_avt_is_evaluated() -> None:
    body = f"""
        <xsl:template name="main">
          <xsl:variable name="collation" select="'{HTML_CASE_INSENSITIVE}'"/>
          <out>
            <xsl:for-each-group select="('A', 'a')" group-by="."
              collation="{{$collation}}">
              <g><xsl:value-of select="current-group()" separator=","/></g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert [
        group.string_value for group in element_children(result.principal_tree.root_element)
    ] == ["A,a"]


# ---------------------------------------------------------------------------
# Static and dynamic errors


def test_grouping_algorithm_cardinality_static_errors() -> None:
    none = stylesheet(
        '<xsl:template name="main">'
        '<xsl:for-each-group select="1"><g/></xsl:for-each-group>'
        "</xsl:template>"
    )
    assert error_code(lambda: compiled(none)) == "XTSE1080"

    multiple = stylesheet(
        '<xsl:template name="main">'
        '<xsl:for-each-group select="1" group-by="." group-adjacent="."><g/>'
        "</xsl:for-each-group></xsl:template>"
    )
    assert error_code(lambda: compiled(multiple)) == "XTSE1080"

    missing_select = stylesheet(
        '<xsl:template name="main"><xsl:for-each-group group-by="."><g/>'
        "</xsl:for-each-group></xsl:template>"
    )
    assert error_code(lambda: compiled(missing_select)) == "XTSE0010"


def test_collation_and_composite_reject_pattern_grouping() -> None:
    for attribute in ('collation="urn:x"', 'composite="yes"'):
        source = stylesheet(
            '<xsl:template name="main">'
            f'<xsl:for-each-group select="1" group-starting-with="x" {attribute}>'
            "<g/></xsl:for-each-group></xsl:template>"
        )
        assert error_code(lambda source=source: compiled(source)) == "XTSE1090"


def test_group_key_function_item_raises_foty0013() -> None:
    body = """
        <xsl:template name="main">
          <xsl:for-each-group select="(1, 2)" group-by="function() { 1 }">
            <g/>
          </xsl:for-each-group>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "FOTY0013"


def test_grouping_pattern_can_use_a_global_variable() -> None:
    body = """
        <xsl:variable name="name" select="'start'"/>
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="doc/n" group-starting-with="n[@*[local-name() = $name]]">
              <g><xsl:value-of select="current-group()" separator=","/></g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n>0</n><n start="1">1</n><n>2</n></doc>',
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [group.string_value for group in groups] == ["0", "1,2"]


def test_group_by_preserves_repeated_population_occurrences_of_one_node() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="(doc/n[1], doc/n[1])" group-by="'k'">
              <g count="{count(current-group())}"/>
            </xsl:for-each-group>
            <xsl:for-each-group select="(doc/n[1], doc/n[1])" group-by="('a', 'b')"
              composite="yes">
              <g count="{count(current-group())}"/>
            </xsl:for-each-group>
            <xsl:for-each-group select="(doc/n[1], doc/n[1])" group-by="('a', 'b')">
              <g key="{current-grouping-key()}" count="{count(current-group())}"/>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b"<doc><n/></doc>",
        initial_template="main",
    )
    groups = element_children(result.principal_tree.root_element)
    assert [group.attributes[0].string_value for group in groups[:2]] == ["2", "2"]
    assert [
        (group.attributes[0].string_value, group.attributes[1].string_value) for group in groups[2:]
    ] == [("a", "2"), ("b", "2")]


def test_group_by_deep_equal_uses_codepoint_default() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="('A', 'a')" group-by=".">
              <g key="{current-grouping-key()}"><xsl:value-of select="current-group()" separator=","/></g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    groups = element_children(result.principal_tree.root_element)
    assert [group.string_value for group in groups] == ["A", "a"]


# ---------------------------------------------------------------------------
# Grouping value contracts


def test_atomic_grouping_equality_covers_atomic_families() -> None:
    from pyxsd import xsd_data_types as xsd

    collation = resolve_collation(None, code="XTDE1110")
    boolean = XdmAtomic.boolean(True)
    qname = XdmAtomic.typed(xsd.QName("a"), xsd.QName)
    date = XdmAtomic.typed(xsd.Date("2020-01-01"), xsd.Date)
    other_date = XdmAtomic.typed(xsd.Date("2020-01-02"), xsd.Date)

    assert atomic_equal(boolean, XdmAtomic.boolean(True), collation)
    assert not atomic_equal(boolean, XdmAtomic.boolean(False), collation)
    assert atomic_equal(qname, XdmAtomic.typed(xsd.QName("a"), xsd.QName), collation)
    assert not atomic_equal(qname, XdmAtomic.string("a"), collation)
    assert atomic_equal(date, date, collation)
    assert not atomic_equal(date, other_date, collation)
    assert not atomic_equal(
        date,
        XdmAtomic.typed(xsd.Duration("P1D"), xsd.Duration),
        collation,
    )


def test_deep_equal_sequences_checks_lengths_and_item_kinds() -> None:
    collation = resolve_collation(None, code="XTDE1110")
    assert not deep_equal_sequences(
        (XdmAtomic.integer(1),),
        (XdmAtomic.integer(1), XdmAtomic.integer(2)),
        collation,
    )
    assert not deep_equal_sequences(
        (object(),),  # type: ignore[arg-type]
        (object(),),  # type: ignore[arg-type]
        collation,
    )


def test_group_and_iteration_state_validation() -> None:
    item = XdmAtomic.integer(1)
    with pytest.raises(ValueError, match="never empty"):
        Group((), item, None)
    with pytest.raises(ValueError, match="initial item"):
        Group((item, XdmAtomic.integer(2)), XdmAtomic.integer(2), None)

    with pytest.raises(ValueError, match="iteration kind"):
        IterationState("unknown", (), (), {})
    with pytest.raises(ValueError, match="iteration control"):
        IterationState("for-each", (), (), {}, control="unknown")

    state = IterationState(
        "iterate",
        (),
        (IterationItem(item, 1, 1),),
        {"x": ()},
        parameters={"x": ()},
        on_completion=Completion(None, None, ()),
    )
    assert state.index == 0
    assert isinstance(state.base_variables, dict)


def test_group_by_boolean_and_temporal_keys_use_value_equality() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:for-each-group select="(true(), true(), false())" group-by=".">
              <g key="{current-grouping-key()}">
                <xsl:value-of select="current-group()" separator=","/>
              </g>
            </xsl:for-each-group>
            <xsl:for-each-group select="(xs:date('2020-01-01'), xs:date('2020-01-01'))"
              group-by=".">
              <g key="{current-grouping-key()}">
                <xsl:value-of select="count(current-group())"/>
              </g>
            </xsl:for-each-group>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    groups = element_children(result.principal_tree.root_element)
    assert [(group.attributes[0].string_value, group.string_value) for group in groups] == [
        ("true", "true,true"),
        ("false", "false"),
        ("2020-01-01", "2"),
    ]


def test_group_by_ignores_duplicate_keys_supplied_for_one_item() -> None:
    collation = resolve_collation(None, code="XTDE1110")
    item = XdmAtomic.string("a")
    key = (XdmAtomic.string("k"),)
    groups = group_by(
        (item,),
        ((key, key),),
        collation,
    )
    assert len(groups) == 1
    assert groups[0].items == (item,)

    repeated = group_by(
        (item, item),
        ((key,), (key,)),
        collation,
    )
    assert len(repeated) == 1
    assert repeated[0].items == (item, item)


def test_atomic_equal_handles_comparison_failures_as_unequal() -> None:
    class BadEquality:
        def __hash__(self) -> int:
            return 1

        def __eq__(self, other: object) -> bool:
            raise TypeError("cannot compare")

        def __str__(self) -> str:
            return "bad"

    collation = resolve_collation(None, code="XTDE1110")
    left = XdmAtomic(BadEquality(), "urn:bad")
    right = XdmAtomic(BadEquality(), "urn:bad")
    assert atomic_equal(left, right, collation) is False
