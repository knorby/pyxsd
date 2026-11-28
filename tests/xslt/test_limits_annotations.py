"""Atomic type-annotation capacity inherited across nested transforms."""

from __future__ import annotations

import pytest
from tests.xslt.limits_support import assert_exhausted, compile_body
from tests.xslt.support import stylesheet

from pyxsd.xdm import XdmAtomic
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.frames import ExecutionLimits

ANNOTATION_BODY = '<xsl:param name="values"/><xsl:sequence select="reverse($values)"/>'


def _annotation_values(count):
    return tuple(XdmAtomic.integer(n) for n in range(1000, 1000 + count))


def test_annotation_capacity_is_inclusive():
    compiled = compile_body(ANNOTATION_BODY)
    values = _annotation_values(3)
    limits = ExecutionLimits(max_annotations=2)
    assert (
        compiled.transform(
            delivery="raw", template_parameters={"values": values[:2]}, limits=limits
        ).principal
        == values[:2][::-1]
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw", template_parameters={"values": values}, limits=limits)
    assert_exhausted(caught.value, "atomic type-annotation", "max_annotations", 2, 3)


def test_disabling_annotation_capacity_admits_more_than_the_default():
    compiled = compile_body(ANNOTATION_BODY)
    values = _annotation_values(262_145)
    kwargs = {"delivery": "raw", "template_parameters": {"values": values}}
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(**kwargs)
    assert_exhausted(caught.value, "atomic type-annotation", "max_annotations", 262_144, 262_145)
    assert (
        compiled.transform(**kwargs, limits=ExecutionLimits(max_annotations=None)).principal
        == values[::-1]
    )


@pytest.mark.parametrize(("parent_count", "count"), [(1, 3), (2, 5)])
def test_nested_transform_inherits_annotation_capacity(tmp_path, parent_count, count):
    child = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:variable name="values" select="1000 to {999 + count}"/>'
        '<xsl:variable name="reversed" select="reverse($values)"/>'
        '<xsl:value-of select="count($reversed)"/>'
        "</xsl:template>"
    )
    path = tmp_path / "nested-annotation-limit.xsl"
    path.write_bytes(child)
    compiled = compile_body(
        f'<xsl:variable name="parent" select="1000 to {999 + parent_count}"/>'
        '<xsl:sequence select="reverse($parent)"/>'
        '<xsl:sequence select="transform(map{'
        f"'stylesheet-location': '{path.as_uri()}', 'delivery-format': 'raw'"
        '})?output"/>'
    )
    cap = parent_count + count - 1
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw", limits=ExecutionLimits(max_annotations=cap))
    assert_exhausted(caught.value, "atomic type-annotation", "max_annotations", cap, cap + 1)
    assert "nested-annotation-limit.xsl" in str(caught.value)
    assert compiled.transform(
        delivery="raw", limits=ExecutionLimits(max_annotations=cap + 1)
    ).principal[-1].string_value == str(count)
    assert compiled.transform(
        delivery="raw", limits=ExecutionLimits(max_annotations=None)
    ).principal[-1].string_value == str(count)
