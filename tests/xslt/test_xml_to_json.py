"""Native XDM conversion from the XML representation of JSON."""

from __future__ import annotations

import pytest
from tests.xslt.support import stylesheet, transform

from pyxsd.xdm import XdmAtomic, XdmSequence
from pyxsd.xslt.errors import XsltError

FN_NS = "http://www.w3.org/2005/xpath-functions"
XS_STRING = "{http://www.w3.org/2001/XMLSchema}string"


def _evaluate(expression: str, body: str) -> XdmSequence:
    source = stylesheet(
        f'<xsl:template name="main"><xsl:sequence select="{expression}"/></xsl:template>',
        namespaces={"fn": FN_NS},
    )
    return transform(source, body.encode(), initial_template="main", delivery="raw").principal


@pytest.mark.parametrize(
    "expression",
    ["xml-to-json(.)", "fn:xml-to-json(/*, map{})", "(fn:xml-to-json#2)(/*, map{})"],
)
def test_spec_array_example_returns_one_xdm_string(expression: str) -> None:
    result = _evaluate(
        expression,
        f'<array xmlns="{FN_NS}"><number>1</number><string>is</string>'
        "<boolean>true</boolean></array>",
    )
    (item,) = result
    assert isinstance(item, XdmAtomic)
    assert item.type_name == XS_STRING
    assert item.value == '[1,"is",true]'


def test_nested_members_keep_document_order_and_ignore_non_data_nodes() -> None:
    body = f"""
        <?before root?>
        <map xmlns="{FN_NS}">
            <!-- before the first entry -->
            <array key="z">
                <null/>
                <?between items?>
                <map><boolean key="flag"> 0 </boolean><number key="n">-.25</number></map>
            </array>
            <string key="a"> with<!-- ignored --> space </string>
        </map>
    """
    assert _evaluate("fn:xml-to-json(.)", body) == (
        XdmAtomic.string('{"z":[null,{"flag":false,"n":-0.25}],"a":" with space "}'),
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [("<map/>", "{}"), ("<array/>", "[]"), ("<string/>", '""'), ("<null/>", "null")],
)
def test_empty_elements_have_kind_specific_representations(body: str, expected: str) -> None:
    assert _evaluate("fn:xml-to-json(/*/*)", f'<root xmlns="{FN_NS}">{body}</root>') == (
        XdmAtomic.string(expected),
    )


@pytest.mark.parametrize("expression", ["xml-to-json(())", "xml-to-json((), map{})"])
def test_empty_input_returns_empty_sequence(expression: str) -> None:
    assert _evaluate(expression, "<source/>") == ()


def test_string_and_key_escaping() -> None:
    body = (
        f'<map xmlns="{FN_NS}"><string key="&quot;\\&#x9;/">café/\\&quot;&#xA;&#x7F;</string></map>'
    )
    assert _evaluate("xml-to-json(.)", body) == (
        XdmAtomic.string(r'{"\"\\\t/":"café/\\\"\n\u007F"}'),
    )


@pytest.mark.parametrize(
    "content",
    ["", "<fn:null/><fn:map/>", "<xsl:text> </xsl:text><fn:null/>"],
)
def test_document_must_have_one_element_and_no_text(content: str) -> None:
    source = stylesheet(
        '<xsl:template name="main"><xsl:variable name="doc" as="document-node()">'
        f"<xsl:document>{content}</xsl:document></xsl:variable>"
        '<xsl:sequence select="fn:xml-to-json($doc)"/></xsl:template>',
        namespaces={"fn": FN_NS},
    )
    with pytest.raises(XsltError) as caught:
        transform(source, initial_template="main", delivery="raw")
    assert caught.value.code == "FOJS0006"


@pytest.mark.parametrize(
    "body",
    [
        "<null xmlns='urn:other'/>",
        "<unknown/>",
        "<array>text</array>",
        "<array><null key='not allowed'/></array>",
        "<map><null/></map>",
        "<string><null/></string>",
        "<null> </null>",
        "<boolean/>",
        "<number/>",
        "<number>1e500</number>",
    ],
)
def test_invalid_vocabulary_raises_fojs0006(body: str) -> None:
    with pytest.raises(XsltError) as caught:
        _evaluate("xml-to-json(/*/*)", f'<root xmlns="{FN_NS}">{body}</root>')
    assert caught.value.code == "FOJS0006"


def test_non_element_node_is_invalid_input() -> None:
    with pytest.raises(XsltError) as caught:
        _evaluate("xml-to-json(/*/text())", "<root>text</root>")
    assert caught.value.code == "FOJS0006"


@pytest.mark.parametrize(
    "expression",
    ["xml-to-json(1)", "xml-to-json((., /*))", "xml-to-json(., ())", "xml-to-json(., 1)"],
)
def test_arguments_must_match_function_signature(expression: str) -> None:
    with pytest.raises(XsltError) as caught:
        _evaluate(expression, f'<null xmlns="{FN_NS}"/>')
    assert caught.value.code == "XPTY0004"
