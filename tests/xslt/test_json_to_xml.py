"""Native JSON parsing into the F&O XML vocabulary."""

from __future__ import annotations

import io
from xml.sax.saxutils import quoteattr

import pytest
from tests.xslt.support import stylesheet

from pyxsd.xdm import XdmSequence
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltError

FN = "http://www.w3.org/2005/xpath-functions"
BASE = "https://example.test/styles/main.xsl"


def evaluate(expression: str, *, schema_aware: bool = True) -> XdmSequence:
    source = stylesheet(
        f'<xsl:template name="main"><xsl:sequence select={quoteattr(expression)}/></xsl:template>',
        attributes=f' xml:base="{BASE}"',
        namespaces={"fn": FN},
    )
    return (
        XsltProcessor(schema_aware=schema_aware)
        .compile(io.BytesIO(source))
        .transform(initial_template="main", delivery="raw")
        .principal
    )


@pytest.mark.parametrize(
    "expression",
    [
        "json-to-xml($text)",
        "fn:json-to-xml($text, map{'validate':false(), 'unknown':()})",
        "(fn:json-to-xml#1)($text)",
        "(fn:json-to-xml#2)($text, map{})",
    ],
)
def test_json_to_xml_returns_one_document_with_the_static_base(expression: str) -> None:
    (doc,) = evaluate(f"""let $text := '﻿{{"a":[1.20,true,null,"s"]}}' return {expression}""")
    assert isinstance(doc, XdmDocument)
    assert doc.base_uri == BASE
    assert doc.document_uri is None
    (root,) = doc.children
    assert root.name == f"{{{FN}}}map"
    assert root.parent is doc
    (array,) = root.children
    assert array.name == f"{{{FN}}}array"
    key = array.attribute("key")
    assert key is not None and key.string_value == "a"
    assert [(node.name, node.string_value) for node in array.children] == [
        (f"{{{FN}}}number", "1.20"),
        (f"{{{FN}}}boolean", "true"),
        (f"{{{FN}}}null", ""),
        (f"{{{FN}}}string", "s"),
    ]
    assert not array.children[2].children


def test_json_to_xml_empty_input() -> None:
    assert evaluate("json-to-xml((), map{})") == ()


@pytest.mark.parametrize(
    ("expression", "code"),
    [
        ("json-to-xml('[')", "FOJS0001"),
        (r"""json-to-xml('"\x00"')""", "FOJS0001"),
        ("json-to-xml(1)", "XPTY0004"),
        ("json-to-xml('null', ())", "XPTY0004"),
        ("json-to-xml('null', map{'liberal':()})", "XPTY0004"),
        ("json-to-xml('null', map{'validate':'yes'})", "XPTY0004"),
        ("json-to-xml('null', map{'escape':(true(),false())})", "XPTY0004"),
        ("json-to-xml('null', map{'duplicates':true()})", "XPTY0004"),
        ("json-to-xml('null', map{'duplicates':'use-last'})", "FOJS0005"),
        ("json-to-xml('null', map{'fallback':concat#2})", "XPTY0004"),
        ("json-to-xml('null', map{'escape':true(), 'fallback':string#1})", "FOJS0005"),
        (r"""json-to-xml('{"a":1,"\u0061":2}', map{'duplicates':'reject'})""", "FOJS0003"),
    ],
)
def test_json_to_xml_reports_exact_errors(expression: str, code: str) -> None:
    with pytest.raises(XsltError) as caught:
        evaluate(expression)
    assert caught.value.code == code


@pytest.mark.parametrize("schema_aware", [False, True])
def test_json_to_xml_validation_is_explicitly_unavailable(schema_aware: bool) -> None:
    with pytest.raises(XsltError) as caught:
        evaluate("json-to-xml('[]', map{'validate':true()})", schema_aware=schema_aware)
    assert caught.value.code == "FOJS0004"


@pytest.mark.parametrize(("options", "size"), [("map{}", 2), ("map{'duplicates':'use-first'}", 1)])
def test_json_to_xml_preserves_member_order(options: str, size: int) -> None:
    (doc,) = evaluate(r"""json-to-xml('{"a":1,"\u0061":2}', """ + options + ")")
    assert isinstance(doc, XdmDocument)
    members = doc.children[0].children
    assert [member.string_value for member in members] == ["1", "2"][:size]


@pytest.mark.parametrize(
    ("options", "key", "value", "escaped"),
    [
        ("map{}", "\t\ufffd", '"%/\U0001d11e\ufffd\ufffd\\\x7f', False),
        (
            "map{'escape':true()}",
            r"\t\u0000",
            '"%/\U0001d11e' + r"\uDEAD\uD800" + "\\\\" + r"\u007F",
            True,
        ),
        (
            "map{'fallback':function($s as xs:string) as xs:string { '[' || $s || ']' }}",
            "\t[\\u0000]",
            '"%/\U0001d11e[\\uDEAD][\\uD800]\\\x7f',
            False,
        ),
    ],
)
def test_json_to_xml_normalizes_special_characters(
    options: str, key: str, value: str, escaped: bool
) -> None:
    (doc,) = evaluate(
        r"""json-to-xml('{"\t\u0000":"\"\u0025\/\uD834\uDD1E\uDEAD\uD800\\\u007f"}', """
        + options
        + ")"
    )
    assert isinstance(doc, XdmDocument)
    (member,) = doc.children[0].children
    attribute = member.attribute("key")
    assert attribute is not None and attribute.string_value == key
    assert member.string_value == value
    assert (member.attribute("escaped") is not None) is escaped
    assert (member.attribute("escaped-key") is not None) is escaped


@pytest.mark.parametrize(
    ("expression", "name"),
    [
        ("json-to-xml('{}')", f"{{{FN}}}map"),
        ("json-to-xml('[]')", f"{{{FN}}}array"),
    ],
)
def test_json_to_xml_empty_containers_have_no_children(expression: str, name: str) -> None:
    (doc,) = evaluate(expression)
    assert isinstance(doc, XdmDocument)
    (root,) = doc.children
    assert root.name == name
    assert not root.children


def test_json_to_xml_root_escaped_string_marks_itself_escaped() -> None:
    (doc,) = evaluate(r"""json-to-xml('"a\\b"', map{'escape':true()})""")
    assert isinstance(doc, XdmDocument)
    (root,) = doc.children
    assert root.string_value == r"a\\b"
    assert root.attribute("escaped") is not None


def test_json_to_xml_renders_a_false_boolean() -> None:
    (doc,) = evaluate("json-to-xml('[false]')")
    assert isinstance(doc, XdmDocument)
    (array,) = doc.children
    (member,) = array.children
    assert member.name == f"{{{FN}}}boolean"
    assert member.string_value == "false"


def test_json_to_xml_rejects_retain_with_validation() -> None:
    with pytest.raises(XsltError) as caught:
        evaluate("json-to-xml('null', map{'validate':true(), 'duplicates':'retain'})")
    assert caught.value.code == "FOJS0005"


@pytest.mark.parametrize(
    "options",
    [
        "map{'fallback':1}",
        "map{'fallback':function($s){('a','b')}}",
        "map{'fallback':function($s){map{}}}",
    ],
)
def test_json_to_xml_rejects_fallback_results_that_are_not_one_string(options: str) -> None:
    with pytest.raises(XsltError) as caught:
        evaluate(r"""json-to-xml('"\u0000"', """ + options + ")")
    assert caught.value.code == "XPTY0004"


def test_json_to_xml_accepts_a_node_fallback_result() -> None:
    (doc,) = evaluate(
        r"""json-to-xml('"\u0000"', map{'fallback':function($s){parse-xml('<j>X</j>')}})"""
    )
    assert isinstance(doc, XdmDocument)
    (root,) = doc.children
    assert root.string_value == "X"


@pytest.mark.parametrize("argument", ["('a','b')", "json-to-xml#1"])
def test_json_to_xml_rejects_non_single_and_non_atomic_text(argument: str) -> None:
    with pytest.raises(XsltError) as caught:
        evaluate(f"json-to-xml({argument})")
    assert caught.value.code == "XPTY0004"


def test_json_to_xml_atomizes_a_node_text_argument() -> None:
    (doc,) = evaluate("""json-to-xml(parse-xml('<j>"x"</j>'))""")
    assert isinstance(doc, XdmDocument)
    (root,) = doc.children
    assert root.name == f"{{{FN}}}string"
    assert root.string_value == "x"


def test_json_to_xml_preserves_a_carriage_return_in_text() -> None:
    (doc,) = evaluate(r"""json-to-xml('"\r"')""")
    assert isinstance(doc, XdmDocument)
    (root,) = doc.children
    assert root.string_value == "\r"
