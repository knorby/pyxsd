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
    [
        "xml-to-json(.)",
        "(fn:xml-to-json#1)(/*)",
        "fn:xml-to-json(/*, map{})",
        "(fn:xml-to-json#2)(/*, map{})",
    ],
)
def test_spec_array_example_returns_one_xdm_string(expression: str) -> None:
    result = _evaluate(
        expression,
        f'<array xmlns="{FN_NS}"><number>1</number><string>is</string><boolean>1</boolean></array>',
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
        "<number>NaN</number>",
        "<number>INF</number>",
        "<number>-INF</number>",
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


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("'yes'", id="C100"),
        pytest.param("'true'", id="C101"),
        pytest.param("()", id="C102"),
        pytest.param("(true(), false())", id="C103"),
        pytest.param("xs:untypedAtomic('2')", id="C104"),
        pytest.param("1", id="numeric"),
        pytest.param("map{}", id="map"),
        pytest.param("[(), [true()]]", id="nested-array"),
        pytest.param("/*", id="empty-element"),
    ],
)
def test_failed_indent_conversion_raises_xpty0004(value: str) -> None:
    with pytest.raises(XsltError) as caught:
        _evaluate(f"xml-to-json(/*, map{{'indent': {value}}})", f'<null xmlns="{FN_NS}"/>')
    assert caught.value.code == "XPTY0004"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("'indent'", "true()"),
        ("xs:anyURI('indent')", "xs:untypedAtomic(' 1 ')"),
        ("xs:untypedAtomic('indent')", "/*/@pretty"),
    ],
)
def test_indent_formats_nested_containers(key: str, value: str) -> None:
    body = (
        f'<root xmlns:fn="{FN_NS}" pretty="true"><fn:map>'
        '<fn:array key="a"><fn:null/><fn:array/></fn:array><fn:map key="b"/></fn:map></root>'
    )
    assert _evaluate(f"xml-to-json(/*/*, map{{{key}: {value}}})", body) == (
        XdmAtomic.string('{\n  "a": [\n    null,\n    []\n  ],\n  "b": {}\n}'),
    )


@pytest.mark.parametrize(
    "options",
    [
        "map{'indent':false()}",
        "map{'indent':xs:untypedAtomic('0')}",
        "map{'encoding':'ascii', 'method':'adaptive'}",
    ],
)
def test_compact_output_is_independent_of_serialization_options(options: str) -> None:
    body = f'<array xmlns="{FN_NS}"><string>café/😀</string><null/></array>'
    assert _evaluate(f"xml-to-json(., {options})", body) == (XdmAtomic.string('["café/😀",null]'),)


@pytest.mark.parametrize(
    "body",
    [
        "<null unknown='x'/>",
        "<null fn:unknown='x'/>",
        "<string fn:escaped='true'>text</string>",
        "<boolean escaped='false'>true</boolean>",
        "<array><null escaped-key='false'/></array>",
        "<map><null fn:key='x'/></map>",
        "<map><null key='x' escaped-key='maybe'/></map>",
        "<string escaped='maybe'>text</string>",
        "<boolean>\u00a0true</boolean>",
        "<number>1\u00a0</number>",
    ],
)
def test_invalid_attribute_or_scalar_lexical_form_raises_fojs0006(body: str) -> None:
    with pytest.raises(XsltError) as caught:
        _evaluate("xml-to-json(/*/*)", f'<root xmlns="{FN_NS}" xmlns:fn="{FN_NS}">{body}</root>')
    assert caught.value.code == "FOJS0006"


@pytest.mark.parametrize(
    "flags", ["key='ignored'", "escaped-key='ignored'", "key='x' escaped-key='1'"]
)
def test_root_key_metadata_and_foreign_attributes_are_ignored(flags: str) -> None:
    body = (
        f'<array xmlns="{FN_NS}" xmlns:foreign="urn:other" {flags} foreign:escaped="true">'
        '<string foreign:key="ignored" foreign:escaped="true">/</string></array>'
    )
    assert _evaluate("xml-to-json(.)", body) == (XdmAtomic.string('["/"]'),)


@pytest.mark.parametrize(
    "members",
    [
        "<null key='same'/><string key='same'>other kind</string>",
        r"<null key='\n' escaped-key='true'/><null key='\u000A' escaped-key='1'/>",
        r"<null key='&#x9;'/><null key='\t' escaped-key='true'/>",
        r"<null key='😀'/><null key='\uD83D\uDE00' escaped-key='true'/>",
        r"<map key='outer'><null key='\n'/><null key='\n' escaped-key='true'/></map>",
    ],
)
def test_duplicate_map_keys_raise_fojs0006(members: str) -> None:
    with pytest.raises(XsltError) as caught:
        _evaluate("xml-to-json(.)", f'<map xmlns="{FN_NS}">{members}</map>')
    assert caught.value.code == "FOJS0006"


def test_map_key_comparison_expands_only_marked_escapes_and_is_local() -> None:
    body = (
        rf'<map xmlns="{FN_NS}"><null key="\n" escaped-key="0"/>'
        r'<null key="\n" escaped-key="true"/>'
        r'<map key="nested"><null key="\n"/></map></map>'
    )
    assert _evaluate("xml-to-json(.)", body) == (
        XdmAtomic.string(r'{"\\n":null,"\n":null,"nested":{"\\n":null}}'),
    )


@pytest.mark.parametrize("escape", ["\\", r"\q", r"\u12", r"\u12G4", r"\\\q"])
@pytest.mark.parametrize("key", [False, True], ids=["string", "key"])
def test_invalid_preescaped_lexical_forms_raise_fojs0007(escape: str, key: bool) -> None:
    body = (
        f'<map xmlns="{FN_NS}"><null key="{escape}" escaped-key="true"/></map>'
        if key
        else f'<string xmlns="{FN_NS}" escaped="true">{escape}</string>'
    )
    with pytest.raises(XsltError) as caught:
        _evaluate("xml-to-json(.)", body)
    assert caught.value.code == "FOJS0007"


def test_preescaped_keys_and_strings_preserve_escape_spelling_and_escape_raw_characters() -> None:
    body = (
        rf'<map xmlns="{FN_NS}"><string key="a/\/&#x9;&quot;\u00e9" escaped-key="true"'
        r' escaped=" &#x9;1 ">é😀/\/\\&quot;&#xA;&#x9F;\uD83d\uDe00\b\f\n\r\t\"'
        r'</string><string key="ordinary" escaped="0">/\q</string></map>'
    )
    assert _evaluate("xml-to-json(., map{'encoding':'ascii'})", body) == (
        XdmAtomic.string(
            r'{"a\/\/\t\"\u00e9":"é😀\/\/\\\"\n\u009F\uD83d\uDe00\b\f\n\r\t\"","ordinary":"/\\q"}'
        ),
    )


@pytest.mark.parametrize(
    ("lexical", "expected"),
    [
        (" -0.0 ", "-0"),
        ("0e20", "0"),
        ("+001.2300", "1.23"),
        ("1e-6", "0.000001"),
        ("1e-7", "1.0E-7"),
        ("9.999999e5", "999999.9"),
        ("1e6", "1.0E6"),
        ("-1.25e20", "-1.25E20"),
        ("9007199254740993", "9.007199254740992E15"),
    ],
)
def test_numbers_use_xpath_double_to_string_lexical_form(lexical: str, expected: str) -> None:
    assert _evaluate("xml-to-json(.)", f'<number xmlns="{FN_NS}">{lexical}</number>') == (
        XdmAtomic.string(expected),
    )
