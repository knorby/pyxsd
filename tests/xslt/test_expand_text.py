"""Effective ``expand-text`` scope, text value templates, and escaped braces.

Every assertion is behavioral through the public processor.  The cases pin the
nearest ``[xsl:]expand-text`` scope for literal result text, ``xsl:text``,
nested instructions, and analyze-string branch content, plus the AVT/TVT brace
rules.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"


def stylesheet(body: str, *, version: str = "3.0", attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="{version}"{attributes}>{body}</xsl:stylesheet>'
    ).encode()


def compiled(source: bytes, **options: Any):
    return XsltProcessor(**options).compile(io.BytesIO(source))


def transform(
    source: bytes,
    input_source: bytes | None = b"<doc/>",
    *,
    delivery: str = "document",
    **options: Any,
):
    stylesheet_ = compiled(source)
    if input_source is None:
        return stylesheet_.transform(delivery=delivery, **options)
    return stylesheet_.transform(source=input_source, delivery=delivery, **options)


def text(call: Any) -> str:
    return call().principal_tree.string_value


def error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def test_literal_result_text_expands_when_effective() -> None:
    source = stylesheet(
        '<xsl:template name="main" expand-text="yes">'
        "<p>{1 + 1} {{literal}} {concat('a', 'b')}</p>"
        "</xsl:template>"
    )
    assert text(lambda: transform(source, initial_template="main")) == "2 {literal} ab"


def test_root_expand_text_is_inherited_and_template_scope_can_disable_it() -> None:
    source = stylesheet(
        '<xsl:template name="main"><a>{1 + 1}</a>'
        '<b xsl:expand-text="no">{1 + 1}</b></xsl:template>',
        attributes=' expand-text="yes"',
    )
    assert text(lambda: transform(source, initial_template="main")) == "2{1 + 1}"


def test_literal_result_element_scope_applies_to_its_text_and_children() -> None:
    source = stylesheet(
        '<xsl:template name="main"><a xsl:expand-text="yes">{1}<b>{2}</b></a>'
        "<c>{3}</c></xsl:template>"
    )
    assert text(lambda: transform(source, initial_template="main")) == "12{3}"


def test_xsl_text_can_override_effective_expand_text() -> None:
    source = stylesheet(
        '<xsl:template name="main" expand-text="yes">'
        '<xsl:text expand-text="no">{1 + 1}</xsl:text>'
        '<xsl:text expand-text="yes">[{1 + 1}]</xsl:text>'
        "</xsl:template>"
    )
    assert text(lambda: transform(source, initial_template="main")) == "{1 + 1}[2]"


def test_text_value_template_braces_are_escaped_and_comments_are_ignored() -> None:
    source = stylesheet(
        '<xsl:template name="main" expand-text="yes">{{{1 + 1}}}{(:comment:)}{\'{\'}</xsl:template>'
    )
    assert text(lambda: transform(source, initial_template="main")) == "{2}{"


def test_attribute_value_templates_expand_independently_of_expand_text() -> None:
    source = stylesheet(
        '<xsl:template name="main"><a title="{{literal}} {1 + 1}" '
        'xsl:expand-text="no">text</a></xsl:template>'
    )
    tree = transform(source, initial_template="main").principal_tree
    assert tree.root_element.attribute("title").string_value == "{literal} 2"


def test_attribute_value_template_keeps_braces_inside_xpath_string_literals() -> None:
    source = stylesheet(
        "<xsl:template name=\"main\"><a title=\"{concat('{', '}')}\"/></xsl:template>"
    )
    tree = transform(source, initial_template="main").principal_tree
    assert tree.root_element.attribute("title").string_value == "{}"


def test_text_nodes_around_nested_instructions_keep_their_tvt_scope() -> None:
    source = stylesheet(
        '<xsl:template name="main" expand-text="yes">'
        "a{1}<xsl:value-of select=\"'b'\"/>c{2}"
        "</xsl:template>"
    )
    assert text(lambda: transform(source, initial_template="main")) == "a1bc2"


def test_analyze_string_branch_text_follows_effective_expand_text() -> None:
    source = stylesheet(
        '<xsl:template name="main" expand-text="yes">'
        '<xsl:analyze-string select="\'a1\'" regex="([a-z])([0-9])">'
        "<xsl:matching-substring>[{regex-group(1)}{regex-group(2)}]</xsl:matching-substring>"
        "</xsl:analyze-string>"
        "</xsl:template>"
    )
    assert text(lambda: transform(source, initial_template="main")) == "[a1]"


def test_unescaped_right_brace_in_text_value_template_is_static_error() -> None:
    source = stylesheet('<xsl:template name="main" expand-text="yes">bad } brace</xsl:template>')
    assert error_code(lambda: compiled(source)) == "XTSE0370"


def test_unescaped_left_brace_in_text_value_template_is_static_error() -> None:
    source = stylesheet('<xsl:template name="main" expand-text="yes">bad { brace</xsl:template>')
    assert error_code(lambda: compiled(source)) == "XTSE0350"


def test_invalid_expand_text_value_is_static_error() -> None:
    source = stylesheet(
        '<xsl:template name="main"><xsl:text expand-text="maybe">x</xsl:text></xsl:template>'
    )
    assert error_code(lambda: compiled(source)) == "XTSE0020"


def test_escaped_braces_are_literal_in_attribute_value_templates() -> None:
    source = stylesheet('<xsl:template name="main"><a title="{{{{}}}}"/></xsl:template>')
    tree = transform(source, initial_template="main").principal_tree
    assert tree.root_element.attribute("title").string_value == "{{}}"
