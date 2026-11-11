"""Stable XSLT sorting, collations, data types, and sort-key errors.

The tests exercise ``xsl:sort`` in ``xsl:for-each``, ``xsl:apply-templates``,
``xsl:for-each-group``, and ``xsl:perform-sort``.  Every stylesheet is inline,
and each assertion checks observable ordering rather than implementation
details.
"""

from __future__ import annotations

import io
import math
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError, XsltError
from pyxsd.xslt.profiles import RecoveryPolicy
from pyxsd.xslt.runtime.iteration import (
    ResolvedSortKey,
    _as_float,
    _as_float32,
    compare_sort_key_sets,
    resolve_collation,
    sort_records,
)

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"
CODEPOINT = "http://www.w3.org/2005/xpath-functions/collation/codepoint"
HTML_CASE_INSENSITIVE = (
    "http://www.w3.org/2005/xpath-functions/collation/html-ascii-case-insensitive"
)
UCA = "http://www.w3.org/2013/collation/UCA"


def stylesheet(body: str, *, version: str = "3.0", attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="{version}"{attributes}>{body}</xsl:stylesheet>'
    ).encode()


def compiled(source: bytes, **options: Any):
    return XsltProcessor(**options).compile(io.BytesIO(source))


def transform(
    source: bytes,
    input_source: bytes | None = None,
    *,
    delivery: str = "raw",
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


def sort_strings(sequence: str, sorts: str = '<xsl:sort select="."/>') -> tuple[str, ...]:
    body = f"""
        <xsl:template name="main">
          <xsl:perform-sort select="{sequence}">{sorts}</xsl:perform-sort>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    return tuple(item.lexical for item in result.principal if isinstance(item, XdmAtomic))


# ---------------------------------------------------------------------------
# Ordering, multiple keys, and stability


def test_sort_multiple_keys_major_to_minor_and_stable_ties() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:perform-sort select="doc/n">
              <xsl:sort select="@a" data-type="number"/>
              <xsl:sort select="@b" data-type="text"/>
            </xsl:perform-sort>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n id="1" a="2" b="x"/><n id="2" a="1" b="y"/>'
        b'<n id="3" a="1" b="x"/><n id="4" a="1" b="x"/></doc>',
        initial_template="main",
        delivery="document",
    )
    ids = [
        child.attributes[0].string_value
        for child in result.principal_tree.root_element.children
        if child.name == "n"
    ]
    assert ids == ["3", "4", "2", "1"]


def test_sort_descending_reverses_only_unequal_keys() -> None:
    body = """
        <xsl:template name="main">
          <out>
            <xsl:perform-sort select="doc/n">
              <xsl:sort select="@a" data-type="number" order="descending"/>
            </xsl:perform-sort>
          </out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n id="1" a="1"/><n id="2" a="2"/><n id="3" a="2"/><n id="4" a="0"/></doc>',
        initial_template="main",
        delivery="document",
    )
    ids = [
        child.attributes[0].string_value
        for child in result.principal_tree.root_element.children
        if child.name == "n"
    ]
    assert ids == ["2", "3", "1", "4"]


def test_sort_stability_is_observable_with_explicit_no_value() -> None:
    body = """
        <xsl:template name="main">
          <xsl:perform-sort select="doc/n">
            <xsl:sort select="@key" data-type="text" stable="no"/>
          </xsl:perform-sort>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n id="1" key="x"/><n id="2" key="x"/><n id="3" key="x"/></doc>',
        initial_template="main",
    )
    # An implementation-dependent order must still be a complete permutation;
    # this implementation deliberately retains initial order.
    assert tuple(item.attributes[0].string_value for item in result.principal) == (
        "1",
        "2",
        "3",
    )


# ---------------------------------------------------------------------------
# Data types and empty keys


def test_data_type_number_and_text_use_different_conversions() -> None:
    assert sort_strings("('10', '2', '1')", '<xsl:sort data-type="number"/>') == (
        "1",
        "2",
        "10",
    )
    assert sort_strings("(10, 2, 1)", '<xsl:sort data-type="text"/>') == (
        "1",
        "10",
        "2",
    )
    assert sort_strings(
        "('1', '01', 'x')",
        '<xsl:sort data-type="number"/>',
    ) == ("x", "1", "01")


def test_empty_sort_keys_precede_or_follow_ordinary_values() -> None:
    ascending = sort_strings(
        "(3, 1, 2)",
        '<xsl:sort select="if (. = 2) then () else ." data-type="number"/>',
    )
    assert ascending == ("2", "1", "3")

    descending = sort_strings(
        "(3, 1, 2)",
        '<xsl:sort select="if (. = 2) then () else ." data-type="number" order="descending"/>',
    )
    assert descending == ("3", "1", "2")


def test_sort_key_sequence_constructor_and_default_dot_expression() -> None:
    body = """
        <xsl:template name="main">
          <xsl:perform-sort select="('b', 'a', 'c')">
            <xsl:sort>
              <xsl:sequence select="upper-case(.)"/>
            </xsl:sort>
          </xsl:perform-sort>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert tuple(item.lexical for item in result.principal) == ("a", "b", "c")


# ---------------------------------------------------------------------------
# Collations, language, and case order


def test_case_order_and_lang_are_applied_without_an_explicit_collation() -> None:
    upper = sort_strings(
        "('b', 'A', 'a', 'B', 'C', 'c')",
        '<xsl:sort select="." data-type="text" lang="en-GB" case-order="upper-first"/>',
    )
    assert upper == ("A", "a", "B", "b", "C", "c")

    lower = sort_strings(
        "('b', 'A', 'a', 'B', 'C', 'c')",
        '<xsl:sort select="." data-type="text" lang="en-GB" case-order="lower-first"/>',
    )
    assert lower == ("a", "A", "b", "B", "c", "C")


def test_codepoint_and_ascii_case_insensitive_collations() -> None:
    assert sort_strings(
        "('b', 'A', 'a', 'B')",
        f'<xsl:sort select="." data-type="text" collation="{CODEPOINT}"/>',
    ) == ("A", "B", "a", "b")

    assert sort_strings(
        "('b', 'A', 'a', 'B')",
        f'<xsl:sort select="." data-type="text" collation="{HTML_CASE_INSENSITIVE}"/>',
    ) == ("A", "a", "b", "B")


def test_collation_overrides_lang_and_case_order() -> None:
    values = sort_strings(
        "('B', 'a', 'A', 'b')",
        '<xsl:sort select="." data-type="text" lang="en" case-order="lower-first" '
        f'collation="{CODEPOINT}"/>',
    )
    assert values == ("A", "B", "a", "b")


def test_uca_strength_parameters_affect_ordering() -> None:
    primary = sort_strings(
        "('b', 'A', 'á', 'a')",
        f'<xsl:sort select="." data-type="text" collation="{UCA}?lang=en;strength=primary"/>',
    )
    assert primary == ("A", "á", "a", "b")

    tertiary = sort_strings(
        "('á', 'a', 'A')",
        '<xsl:sort select="." data-type="text" '
        f'collation="{UCA}?lang=en;strength=tertiary;caseFirst=upper"/>',
    )
    assert tertiary[0] == "A"
    assert set(tertiary) == {"A", "a", "á"}


def test_uca_fallback_no_rejects_the_collation() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:perform-sort select="(1)">'
        f'<xsl:sort select="." collation="{UCA}?lang=en;fallback=no"/>'
        "</xsl:perform-sort></xsl:template>"
    )
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTDE1035"


def test_unknown_sort_collation_is_xtde1035_even_under_recovery() -> None:
    body = (
        '<xsl:template name="main">'
        '<xsl:perform-sort select="(1)">'
        '<xsl:sort select="." collation="urn:unknown:collation"/>'
        "</xsl:perform-sort></xsl:template>"
    )
    assert (
        error_code(
            lambda: compiled(
                stylesheet(body),
                recovery_policy=RecoveryPolicy(),
            ).transform(
                initial_template="main",
                delivery="raw",
            )
        )
        == "XTDE1035"
    )


def test_default_collation_does_not_change_xsl_sort_order() -> None:
    body = f"""
        <xsl:template name="main">
          <xsl:perform-sort select="('b', 'A', 'a', 'B')" default-collation="{HTML_CASE_INSENSITIVE}">
            <xsl:sort select="." data-type="text"/>
          </xsl:perform-sort>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert tuple(item.lexical for item in result.principal) == ("A", "B", "a", "b")


# ---------------------------------------------------------------------------
# Errors


def test_incomparable_ordinary_sort_values_raise_xtde1030() -> None:
    assert (
        error_code(
            lambda: sort_strings(
                "(1, 'x')",
                '<xsl:sort select="."/>',
            )
        )
        == "XTDE1030"
    )
    assert (
        error_code(
            lambda: sort_strings(
                "(true(), false())",
                '<xsl:sort select="."/>',
            )
        )
        == "XTDE1030"
    )


def test_multi_item_sort_key_raises_xtte1020_but_1_0_uses_first_item() -> None:
    body = """
        <xsl:template name="main">
          <xsl:perform-sort select="(2, 1)">
            <xsl:sort select="(., .)" data-type="number"/>
          </xsl:perform-sort>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTTE1020"

    compatible = stylesheet(
        """
        <xsl:template name="main">
          <xsl:perform-sort select="(2, 1)">
            <xsl:sort select="(., .)" data-type="number"/>
          </xsl:perform-sort>
        </xsl:template>
        """,
        version="1.0",
    )
    result = transform(compatible, initial_template="main")
    assert tuple(item.lexical for item in result.principal) == ("1", "2")


def test_sort_static_content_errors() -> None:
    select_and_content = stylesheet(
        '<xsl:template name="main">'
        '<xsl:for-each select="1">'
        '<xsl:sort select=".">text</xsl:sort>'
        "</xsl:for-each></xsl:template>"
    )
    assert error_code(lambda: compiled(select_and_content)) == "XTSE1015"

    late_stable = stylesheet(
        '<xsl:template name="main">'
        '<xsl:for-each select="1">'
        '<xsl:sort select="."/>'
        '<xsl:sort select="." stable="yes"/>'
        "</xsl:for-each></xsl:template>"
    )
    assert error_code(lambda: compiled(late_stable)) == "XTSE1017"

    perform_sort_content = stylesheet(
        '<xsl:template name="main">'
        '<xsl:perform-sort select="1">'
        '<xsl:sort select="."/>'
        "<xsl:sequence select=\"'extra'\"/>"
        "</xsl:perform-sort></xsl:template>"
    )
    assert error_code(lambda: compiled(perform_sort_content)) == "XTSE1040"

    misplaced = stylesheet('<xsl:template name="main"><xsl:sort select="."/></xsl:template>')
    assert error_code(lambda: compiled(misplaced)) == "XTSE0010"


def test_sort_avt_errors_use_xtde0030() -> None:
    for attribute in (
        'order="{$bad}"',
        'case-order="{$bad}"',
        'stable="{$bad}"',
    ):
        body = (
            '<xsl:template name="main">'
            '<xsl:variable name="bad" select="\'bogus\'"/>'
            '<xsl:perform-sort select="(1)">'
            f'<xsl:sort select="." {attribute}/>'
            "</xsl:perform-sort></xsl:template>"
        )
        assert (
            error_code(lambda body=body: transform(stylesheet(body), initial_template="main"))
            == "XTDE0030"
        )

    empty_data_type = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="empty" select="''"/>
          <xsl:perform-sort select="(1)">
            <xsl:sort select="." data-type="{$empty}"/>
          </xsl:perform-sort>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(empty_data_type, initial_template="main")) == "XTDE0030"


def test_sort_with_preserved_whitespace_between_sort_components() -> None:
    body = """
        <xsl:template name="main">
          <xsl:for-each select="(1)" xml:space="preserve">
            <xsl:sort select="."/>   <xsl:sort select="."/>
            <xsl:sequence select="."/>
          </xsl:for-each>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert any(item == XdmAtomic.integer(1) for item in result.principal)
    assert any(
        not isinstance(item, XdmAtomic) and item.string_value.strip() == ""
        for item in result.principal
    )


def test_perform_sort_sequence_constructor_is_the_initial_sequence() -> None:
    body = """
        <xsl:template name="main">
          <xsl:perform-sort>
            <xsl:sort select="." data-type="number"/>
            <xsl:sequence select="3"/>
            <xsl:sequence select="1"/>
            <xsl:sequence select="2"/>
          </xsl:perform-sort>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert tuple(item.lexical for item in result.principal) == ("1", "2", "3")


def test_perform_sort_requires_a_sort_component() -> None:
    body = stylesheet(
        """
        <xsl:template name="main">
          <xsl:perform-sort select="(1, 2)"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(body)) == "XTSE0010"


def test_implementation_defined_data_type_falls_back_to_native_ordering() -> None:
    body = """
        <xsl:template name="main">
          <xsl:perform-sort select="(2, 10, 1)">
            <xsl:sort select="." data-type="xs:string"/>
          </xsl:perform-sort>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert tuple(item.lexical for item in result.principal) == ("1", "2", "10")


# ---------------------------------------------------------------------------
# sort as a component of other instructions


def test_for_each_sort_changes_processing_order_and_focus() -> None:
    body = """
        <xsl:template name="main">
          <xsl:for-each select="doc/n">
            <xsl:sort select="number(.)" data-type="number" order="descending"/>
            <xsl:sequence select="concat(position(), ':', .)"/>
          </xsl:for-each>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b"<doc><n>1</n><n>3</n><n>2</n></doc>",
        initial_template="main",
    )
    assert tuple(item.lexical for item in result.principal) == (
        "1:3",
        "2:2",
        "3:1",
    )


def test_apply_templates_sort_changes_selection_order() -> None:
    body = """
        <xsl:template match="/">
          <out>
            <xsl:apply-templates select="doc/n">
              <xsl:sort select="number(.)" data-type="number"/>
            </xsl:apply-templates>
          </out>
        </xsl:template>
        <xsl:template match="n"><xsl:value-of select="."/></xsl:template>
    """
    result = transform(
        stylesheet(body),
        b"<doc><n>3</n><n>1</n><n>2</n></doc>",
        delivery="document",
    )
    assert result.principal_tree.string_value == "123"


def test_perform_sort_in_a_stylesheet_function_returns_a_sequence() -> None:
    body = """
        <xsl:function name="my:sort" as="xs:anyAtomicType*">
          <xsl:param name="input" as="xs:anyAtomicType*"/>
          <xsl:perform-sort select="$input">
            <xsl:sort select="." data-type="number"/>
          </xsl:perform-sort>
        </xsl:function>
        <xsl:template name="main">
          <xsl:sequence select="my:sort((10, 2, 1))"/>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert tuple(item.lexical for item in result.principal) == ("1", "2", "10")


def test_sort_key_focus_and_current_function() -> None:
    body = """
        <xsl:template name="main">
          <xsl:perform-sort select="doc/n">
            <xsl:sort select="concat(position(), '/', last(), ':', current()/@key)"
              data-type="text"/>
          </xsl:perform-sort>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b'<doc><n key="b"/><n key="a"/><n key="c"/></doc>',
        initial_template="main",
        delivery="raw",
    )
    # position()/last() in a sort key are based on the initial sequence, so the
    # keys are 1/3:b, 2/3:a, 3/3:c and sort by their textual form.
    assert tuple(item.attributes[0].string_value for item in result.principal) == (
        "b",
        "a",
        "c",
    )


def test_sort_key_error_is_not_recovered_by_document_recovery_policy() -> None:
    body = """
        <xsl:template name="main">
          <xsl:perform-sort select="(1, 2)">
            <xsl:sort select="document('urn:missing:resource')"/>
          </xsl:perform-sort>
        </xsl:template>
    """
    # The document-retrieval recovery rule may recover the document() failure;
    # sorting must then see two empty keys and retain input order rather than
    # producing an implementation-order error.
    result = compiled(
        stylesheet(body),
        recovery_policy=RecoveryPolicy(rules=frozenset({"document-retrieval"})),
    ).transform(initial_template="main", delivery="raw")
    assert tuple(item.lexical for item in result.principal) == ("1", "2")


# ---------------------------------------------------------------------------
# Collation and sort-record contracts


def test_resolve_collation_contracts_and_parameters() -> None:
    caseblind = resolve_collation(
        "http://www.w3.org/2010/09/qt-fots-catalog/collation/caseblind",
        code="XTDE1035",
    )
    assert caseblind.equal("A", "a")
    assert caseblind.compare("a", "b") < 0

    numeric = resolve_collation(
        f"{UCA}?lang=en;numeric=yes;strength=primary",
        code="XTDE1035",
    )
    assert numeric.compare("a2", "a10") < 0

    with pytest.raises(TypeError, match="collation URI"):
        resolve_collation(1, code="XTDE1035")  # type: ignore[arg-type]

    with pytest.raises(XsltDynamicError) as invalid_strength:
        resolve_collation(f"{UCA}?strength=invalid", code="XTDE1035")
    assert invalid_strength.value.code == "XTDE1035"

    with pytest.raises(XsltDynamicError) as invalid_case:
        resolve_collation(f"{UCA}?caseFirst=sideways", code="XTDE1035")
    assert invalid_case.value.code == "XTDE1035"

    with pytest.raises(XsltDynamicError) as relative:
        resolve_collation(
            "relative-collation",
            code="XTDE1035",
            base_uri="http://example.com/styles/main.xsl",
        )
    assert "http://example.com/styles/relative-collation" in str(relative.value)

    tolerant = resolve_collation(f"{UCA}?;lang=en;unknown", code="XTDE1035")
    assert tolerant.kind == "uca"


def test_resolved_sort_key_validation() -> None:
    with pytest.raises(ValueError, match="sort order"):
        ResolvedSortKey(order="sideways")
    with pytest.raises(ValueError, match="data type"):
        ResolvedSortKey(data_type="date")
    with pytest.raises(ValueError, match="numeric comparison"):
        ResolvedSortKey(numeric_mode="sideways")
    with pytest.raises(TypeError, match="Collation"):
        ResolvedSortKey(collation="codepoint")  # type: ignore[arg-type]


def test_numeric_promotion_uses_one_comparison_type_for_a_whole_sort_key() -> None:
    exact = ResolvedSortKey()
    first = (XdmAtomic.decimal("0.1"),)
    second = (XdmAtomic.decimal("0.10000000000000001"),)
    assert compare_sort_key_sets((first,), (second,), (exact,)) < 0

    as_double = ResolvedSortKey(numeric_mode="double")
    assert compare_sort_key_sets((first,), (second,), (as_double,)) == 0

    records = (0, 1, 2)
    key_values = (
        ((XdmAtomic.decimal("0.1"),),),
        ((XdmAtomic.double(0.1),),),
        ((XdmAtomic.decimal("0.10000000000000001"),),),
    )
    assert sort_records(records, (ResolvedSortKey(),), key_values) == records

    as_float = ResolvedSortKey(numeric_mode="float")
    assert (
        compare_sort_key_sets(
            ((XdmAtomic.decimal("0.1"),),),
            ((XdmAtomic.float(0.1),),),
            (as_float,),
        )
        == 0
    )
    assert math.isinf(_as_float32(10**300))

    float_records = (0, 1)
    float_keys = (
        ((XdmAtomic.decimal("0.1"),),),
        ((XdmAtomic.float(0.1),),),
    )
    assert sort_records(float_records, (ResolvedSortKey(),), float_keys) == float_records

    with pytest.raises(XsltDynamicError) as multi_item:
        sort_records(
            (0, 1),
            (ResolvedSortKey(),),
            (
                ((XdmAtomic.integer(1), XdmAtomic.integer(2)),),
                ((XdmAtomic.integer(1),),),
            ),
        )
    assert multi_item.value.code == "XTTE1020"


def test_sort_records_requires_one_key_tuple_per_record() -> None:
    with pytest.raises(ValueError, match="one sort key tuple"):
        sort_records((1, 2), (ResolvedSortKey(),), ((1,),))


def test_compare_sort_key_sets_rejects_multi_item_keys() -> None:
    key = ResolvedSortKey()
    with pytest.raises(XsltDynamicError) as caught:
        compare_sort_key_sets(
            ((XdmAtomic.integer(1), XdmAtomic.integer(2)),),
            ((XdmAtomic.integer(1),),),
            (key,),
        )
    assert caught.value.code == "XTTE1020"


def test_number_data_type_handles_booleans_and_empty_strings() -> None:
    assert sort_strings(
        "(true(), false(), '')",
        '<xsl:sort select="." data-type="number"/>',
    ) == ("", "false", "true")


def test_native_sort_orders_temporal_values_and_rejects_binary_values() -> None:
    assert sort_strings(
        "(xs:date('2020-01-03'), xs:date('2020-01-01'), xs:date('2020-01-02'))",
        '<xsl:sort select="."/>',
    ) == ("2020-01-01", "2020-01-02", "2020-01-03")

    assert (
        error_code(
            lambda: sort_strings(
                "(xs:base64Binary('YQ=='), xs:base64Binary('Yg=='))",
                '<xsl:sort select="."/>',
            )
        )
        == "XTDE1030"
    )


def test_native_sort_numeric_promotion_and_nan_order() -> None:
    assert sort_strings(
        "(xs:double('NaN'), 2, 1.5, xs:double(1.5))",
        '<xsl:sort select="."/>',
    ) == ("NaN", "1.5", "1.5", "2")


def test_qname_sort_keys_raise_xtde1030() -> None:
    body = """
        <xsl:template name="main">
          <xsl:perform-sort select="(xs:QName('a'), xs:QName('b'))">
            <xsl:sort select="."/>
          </xsl:perform-sort>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "XTDE1030"


def test_sort_nan_equality_and_exact_numeric_ties() -> None:
    assert sort_strings(
        "(xs:double('NaN'), 1, xs:double('NaN'))",
        '<xsl:sort select="."/>',
    ) == ("NaN", "NaN", "1")

    assert sort_strings(
        "(2, 1, 2)",
        '<xsl:sort select="."/>',
    ) == ("1", "2", "2")

    assert sort_strings(
        "(xs:double('NaN'), 1, xs:double('NaN'))",
        '<xsl:sort select="." data-type="number"/>',
    ) == ("NaN", "NaN", "1")

    assert math.isinf(_as_float(10**400))
    assert math.isnan(_as_float("not-a-number"))


def test_native_sort_rejects_mixed_temporal_types_and_orders_equal_dates() -> None:
    assert (
        error_code(
            lambda: sort_strings(
                "(xs:date('2020-01-01'), xs:dateTime('2020-01-01T00:00:00'))",
                '<xsl:sort select="."/>',
            )
        )
        == "XTDE1030"
    )
    assert sort_strings(
        "(xs:date('2020-01-01'), xs:date('2020-01-01'))",
        '<xsl:sort select="."/>',
    ) == ("2020-01-01", "2020-01-01")


def test_unorderable_custom_atomic_values_raise_xtde1030() -> None:
    class Unorderable:
        def __hash__(self) -> int:
            return 1

        def __eq__(self, other: object) -> bool:
            return False

        def __lt__(self, other: object) -> bool:
            raise TypeError("not orderable")

        def __str__(self) -> str:
            return "custom"

    left = XdmAtomic(Unorderable(), "urn:custom")
    right = XdmAtomic(Unorderable(), "urn:custom")
    with pytest.raises(XsltDynamicError) as caught:
        compare_sort_key_sets(((left,),), ((right,),), (ResolvedSortKey(),))
    assert caught.value.code == "XTDE1030"


def test_sort_key_sequence_comparison_rejects_empty_and_non_atomic_edges() -> None:
    key = ResolvedSortKey()
    assert compare_sort_key_sets(((),), ((XdmAtomic.integer(1),),), (key,)) < 0
    assert compare_sort_key_sets(((XdmAtomic.integer(1),),), ((),), (key,)) > 0

    with pytest.raises(XsltDynamicError) as non_atomic:
        compare_sort_key_sets(((object(),),), ((object(),),), (key,))
    assert non_atomic.value.code == "XPTY0004"


def test_uca_secondary_strength_ignores_case_and_numeric_compare() -> None:
    secondary = resolve_collation(f"{UCA}?strength=secondary", code="XTDE1035")
    assert secondary.equal("A", "a")
    assert not secondary.equal("A", "á")

    tertiary = resolve_collation(f"{UCA}?strength=tertiary", code="XTDE1035")
    assert tertiary.compare("a1", "a2") < 0

    with pytest.raises(XsltDynamicError) as missing:
        resolve_collation("relative", code="XTDE1035", base_uri=None)
    assert missing.value.code == "XTDE1035"
