"""Custom collations registered through the immutable extension registry.

Custom collations participate in ``xsl:sort``, ``xsl:for-each-group``, and
``xsl:merge`` ordering through the same :class:`Collation` seam as the built-in
collations.  General XPath string functions still only accept the collations
the underlying XPath engine recognizes; that boundary is deliberate.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from tests.xslt.support import stylesheet

from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import XsltExtensions, XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.iteration import Collation, resolve_collation

REVERSE = "urn:app:collation:reverse"
CASEFOLD = "urn:app:collation:casefold"


def reverse_compare(left: str, right: str) -> int:
    if left == right:
        return 0
    return -1 if left > right else 1


def casefold_compare(left: str, right: str) -> int:
    folded_left = left.casefold()
    folded_right = right.casefold()
    if folded_left == folded_right:
        return 0
    return -1 if folded_left < folded_right else 1


def reverse_sort_key(value: str) -> tuple[int, ...]:
    return tuple(-ord(char) for char in value)


def extensions() -> XsltExtensions:
    return XsltExtensions().collation(
        REVERSE,
        compare=reverse_compare,
        sort_key=reverse_sort_key,
    )


def compile_body(body: str, *, registry: XsltExtensions | None = None) -> Any:
    source = stylesheet(body)
    return XsltProcessor().compile(io.BytesIO(source), extensions=registry)


def test_registry_collation_requires_absolute_uri_and_callables() -> None:
    registry = XsltExtensions()
    with pytest.raises(ValueError, match="absolute"):
        registry.collation("relative/collation", compare=reverse_compare)
    with pytest.raises(ValueError, match="absolute"):
        registry.collation("", compare=reverse_compare)
    with pytest.raises(TypeError, match="compare"):
        registry.collation(REVERSE, compare=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="sort_key"):
        registry.collation(REVERSE, sort_key=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="compare or sort_key"):
        registry.collation(REVERSE)
    with pytest.raises(ValueError, match="already registered"):
        extensions().collation(REVERSE, compare=reverse_compare)


def test_custom_collation_kind_and_equality_ignore_callables() -> None:
    first = resolve_collation(
        REVERSE,
        code="XTDE1035",
        registry=extensions().collations,
    )
    second = resolve_collation(
        REVERSE,
        code="XTDE1035",
        registry=XsltExtensions().collation(REVERSE, compare=lambda left, right: 0).collations,
    )

    assert first.kind == "custom"
    assert first == second
    assert first.compare("a", "b") == 1
    assert first.equal("a", "b") is False
    assert first.sort_key is not None
    assert first.sort_key("ab") == (-ord("a"), -ord("b"))


def test_sort_key_alone_provides_ordering() -> None:
    collation = resolve_collation(
        REVERSE,
        code="XTDE1035",
        registry=XsltExtensions().collation(REVERSE, sort_key=reverse_sort_key).collations,
    )

    assert collation.compare("a", "c") == 1
    assert collation.compare("a", "a") == 0


def test_collation_validation_requires_a_usable_custom_callable() -> None:
    with pytest.raises(ValueError, match="kind"):
        Collation("urn:app:kind", kind="unknown")
    with pytest.raises(TypeError, match="compare"):
        Collation("urn:app:compare", kind="custom", custom_compare=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="sort key"):
        Collation("urn:app:key", kind="custom", sort_key=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="compare or sort key"):
        Collation("urn:app:empty", kind="custom")


def test_registry_miss_falls_back_to_builtin_resolution() -> None:
    registered = extensions().collations
    assert (
        resolve_collation(
            "http://www.w3.org/2005/xpath-functions/collation/codepoint",
            code="XTDE1035",
            registry=registered,
        ).kind
        == "codepoint"
    )


def test_custom_collation_orders_xsl_sort() -> None:
    compiled = compile_body(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:for-each select=\"('b', 'A', 'c')\">"
        f'<xsl:sort select="." collation="{REVERSE}"/>'
        '<xsl:value-of select="."/>'
        "</xsl:for-each>"
        "</xsl:template>",
        registry=extensions(),
    )

    assert compiled.transform(delivery="document").principal_tree.string_value == "cbA"


def test_custom_collation_groups_for_each_group() -> None:
    compiled = compile_body(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:for-each-group select=\"('a', 'A', 'b')\" group-by=\".\" "
        f'collation="{CASEFOLD}">'
        '<xsl:value-of select="current-grouping-key()"/>'
        "</xsl:for-each-group>"
        "</xsl:template>",
        registry=XsltExtensions().collation(CASEFOLD, compare=casefold_compare),
    )

    assert compiled.transform(delivery="document").principal_tree.string_value == "ab"


def test_custom_collation_orders_merge_keys() -> None:
    body = (
        '<xsl:template match="doc">'
        "<xsl:merge>"
        '<xsl:merge-source select="one/item" sort-before-merge="yes">'
        f'<xsl:merge-key select="@k" collation="{REVERSE}"/>'
        "</xsl:merge-source>"
        '<xsl:merge-source select="two/item" sort-before-merge="yes">'
        f'<xsl:merge-key select="@k" collation="{REVERSE}"/>'
        "</xsl:merge-source>"
        "<xsl:merge-action>"
        '<xsl:value-of select="current-merge-key()"/>'
        "</xsl:merge-action>"
        "</xsl:merge>"
        "</xsl:template>"
    )
    compiled = compile_body(body, registry=extensions())
    source = XdmDocument.parse(
        b"<doc><one><item k='a'/><item k='c'/></one><two><item k='b'/></two></doc>"
    )

    assert compiled.transform(source=source, delivery="document").principal_tree.string_value == (
        "cba"
    )


def test_unknown_collation_without_registry_keeps_its_error_codes() -> None:
    sort = compile_body(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:for-each select=\"('a')\">"
        f'<xsl:sort select="." collation="{REVERSE}"/>'
        '<xsl:value-of select="."/>'
        "</xsl:for-each>"
        "</xsl:template>"
    )
    with pytest.raises(XsltDynamicError) as caught:
        sort.transform(delivery="document")
    assert caught.value.code == "XTDE1035"

    grouping = compile_body(
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:for-each-group select="(\'a\')" group-by="." collation="{REVERSE}">'
        '<xsl:value-of select="current-grouping-key()"/>'
        "</xsl:for-each-group>"
        "</xsl:template>"
    )
    with pytest.raises(XsltDynamicError) as caught:
        grouping.transform(delivery="document")
    assert caught.value.code == "XTDE1110"


def test_registry_is_snapshotted_and_not_shared_by_late_updates() -> None:
    base = XsltExtensions()
    with_compare = base.collation(REVERSE, compare=reverse_compare)
    with_key = with_compare.collation(CASEFOLD, sort_key=reverse_sort_key)

    assert set(base.collations) == set()
    assert set(with_compare.collations) == {REVERSE}
    assert set(with_key.collations) == {REVERSE, CASEFOLD}

    compiled = compile_body(initial_sort(), registry=with_compare)
    with_key.collation("urn:app:collation:late", compare=casefold_compare)
    assert compiled.transform(delivery="document").principal_tree.string_value == "cbA"


def initial_sort() -> str:
    return (
        '<xsl:template name="xsl:initial-template">'
        "<xsl:for-each select=\"('b', 'A', 'c')\">"
        f'<xsl:sort select="." collation="{REVERSE}"/>'
        '<xsl:value-of select="."/>'
        "</xsl:for-each>"
        "</xsl:template>"
    )


def test_collation_identity_is_stable_in_compiled_expressions() -> None:
    collation = Collation("urn:app:example", kind="custom", custom_compare=reverse_compare)
    same = Collation("urn:app:example", kind="custom", custom_compare=casefold_compare)
    other = Collation("urn:app:example", kind="custom", custom_compare=casefold_compare)

    assert collation == same == other
    assert collation != Collation("urn:app:other", kind="custom", custom_compare=reverse_compare)
