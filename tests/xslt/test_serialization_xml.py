"""Serialization parameter resolution and the XML-family output methods.

These tests operate directly on XDM values and nodes, so serialization is
exercised independently of stylesheet execution.  A separate section covers
the small integration seam that populates ``ResultEntry.serialized`` for
stylesheet delivery.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from tests.xslt.support import stylesheet, transform

from pyxsd.xdm import XdmAtomic, XdmFunction, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt.errors import XsltError, XsltSerializationError, XsltStaticError
from pyxsd.xslt.processor import XsltProcessor
from pyxsd.xslt.runtime.builder import ResultBuilder
from pyxsd.xslt.serialization import SerializationParameters, Serializer

XHTML_NS = "http://www.w3.org/1999/xhtml"
XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
SECONDARY_URI = "http://example.com/secondary.xml"


def _document(xml: bytes) -> XdmDocument:
    return XdmDocument.parse(xml)


def _serialize(sequence: tuple[object, ...], **values: object) -> str:
    parameters = SerializationParameters.merge(explicit=values)
    return Serializer.serialize(sequence, parameters)  # type: ignore[arg-type]


def _error_code(action: object) -> str | None:
    assert callable(action)
    with pytest.raises(XsltSerializationError) as error:
        action()
    return error.value.code


# ---------------------------------------------------------------------------
# XML declaration, doctype, escaping


def test_xml_declaration_version_encoding_and_standalone() -> None:
    document = _document(b"<root/>")

    assert _serialize((document,)) == '<?xml version="1.0" encoding="UTF-8"?><root/>'
    assert (
        _serialize((document,), version="1.1", encoding="utf-16", standalone=True)
        == '<?xml version="1.1" encoding="utf-16" standalone="yes"?><root/>'
    )
    assert (
        _serialize((document,), standalone=False)
        == '<?xml version="1.0" encoding="UTF-8" standalone="no"?><root/>'
    )
    assert _serialize((document,), omit_xml_declaration=True) == "<root/>"
    assert _serialize(()) == ""


def test_xml_text_and_attribute_escaping() -> None:
    document = _document(
        b'<r a="x&amp;y&#9;z&#10;&#13;&quot;&lt;">a &amp; b &lt; c ]]&gt; &#13; end</r>'
    )

    assert _serialize((document,), omit_xml_declaration=True) == (
        '<r a="x&amp;y&#x9;z&#xA;&#xD;&quot;&lt;">a &amp; b &lt; c ]]&gt; &#xD; end</r>'
    )


def test_xml_empty_elements_and_namespace_declarations() -> None:
    document = _document(
        b'<root xmlns="urn:default" xmlns:p="urn:p">'
        b'<p:child p:a="1"/>'
        b'<plain xmlns=""><p:child/></plain>'
        b'<p:child xmlns:p="urn:q"/>'
        b"</root>"
    )

    assert _serialize((document,), omit_xml_declaration=True) == (
        '<root xmlns="urn:default" xmlns:p="urn:p">'
        '<p:child p:a="1"/>'
        '<plain xmlns=""><p:child/></plain>'
        '<p:child xmlns:p="urn:q"/>'
        "</root>"
    )


def test_xml_generates_missing_prefixes_deterministically() -> None:
    document = _document(b"<source/>")
    element = XdmNode(
        NodeKind.ELEMENT,
        document=document,
        node_id=100,
        source_name=document.source_name,
        source_index=document.source_index,
        name="{urn:generated}root",
        prefix="missing",
        namespace_uri="urn:generated",
        in_scope_namespaces={"": ""},
    )

    assert _serialize((element,), omit_xml_declaration=True) == (
        '<ns0:root xmlns:ns0="urn:generated"/>'
    )

    collision = XdmNode(
        NodeKind.ELEMENT,
        document=document,
        node_id=101,
        source_name=document.source_name,
        source_index=document.source_index,
        name="{urn:generated}root",
        prefix="missing",
        namespace_uri="urn:generated",
        in_scope_namespaces={"": "", "ns0": "urn:other"},
    )
    assert _serialize((collision,), omit_xml_declaration=True) == (
        '<ns1:root xmlns:ns0="urn:other" xmlns:ns1="urn:generated"/>'
    )


def test_xml_and_attribute_names_use_the_reserved_xml_prefix() -> None:
    document = _document(b'<r xml:lang="en"/>')

    assert _serialize((document,), omit_xml_declaration=True) == '<r xml:lang="en"/>'

    element = XdmNode(
        NodeKind.ELEMENT,
        document=document,
        node_id=100,
        source_name=document.source_name,
        source_index=document.source_index,
        name="{http://www.w3.org/XML/1998/namespace}space",
        prefix="xml",
        namespace_uri="http://www.w3.org/XML/1998/namespace",
        in_scope_namespaces={"xml": "http://www.w3.org/XML/1998/namespace"},
    )
    assert _serialize((element,), omit_xml_declaration=True) == "<xml:space/>"


def test_xml_generates_an_attribute_prefix_when_none_is_bound() -> None:
    document = _document(b"<source/>")
    element = XdmNode(
        NodeKind.ELEMENT,
        document=document,
        node_id=100,
        source_name=document.source_name,
        source_index=document.source_index,
        name="root",
        in_scope_namespaces={"": ""},
    )
    attribute = XdmNode(
        NodeKind.ATTRIBUTE,
        document=document,
        node_id=101,
        source_name=document.source_name,
        source_index=document.source_index,
        parent=element,
        name="{urn:a}flag",
        value="yes",
        prefix="missing",
        namespace_uri="urn:a",
        in_scope_namespaces={"": ""},
    )
    element._append_attribute(attribute)

    assert _serialize((element,), omit_xml_declaration=True) == (
        '<root xmlns:ns0="urn:a" ns0:flag="yes"/>'
    )


def test_constructed_element_keeps_its_lexical_prefix_through_serialization() -> None:
    builder = ResultBuilder.temporary()
    builder.element("{urn:a}out", namespaces={"": "urn:a"})
    builder.element(
        "{urn:a}item",
        namespaces={"": "urn:a", "p": "urn:a"},
        prefix="p",
    )
    builder.end_element()
    builder.end_element()
    document = builder.to_tree()

    assert _serialize((document,), omit_xml_declaration=True) == (
        '<out xmlns="urn:a"><p:item xmlns:p="urn:a"/></out>'
    )


def test_constructed_undeclaration_is_serialized_under_a_rebound_default() -> None:
    builder = ResultBuilder.temporary()
    builder.element("{urn:1}a", namespaces={"": "urn:1"})
    builder.element("{urn:2}b", namespaces={"": "urn:2"})
    builder.element("c")
    builder.end_element()
    builder.end_element()
    builder.end_element()
    document = builder.to_tree()

    assert _serialize((document,), omit_xml_declaration=True) == (
        '<a xmlns="urn:1"><b xmlns="urn:2"><c xmlns=""/></b></a>'
    )


def test_namespace_nodes_raise_senr0001_and_documents_are_replaced_by_children() -> None:
    document = _document(b'<r xmlns="urn:default">text<!--note--><?pi data?></r>')

    namespace = document.root_element.namespace_nodes[0]
    with pytest.raises(XsltSerializationError) as error:
        _serialize((namespace,), omit_xml_declaration=True)
    assert error.value.code == "SENR0001"
    assert _serialize((document,), omit_xml_declaration=True) == (
        '<r xmlns="urn:default">text<!--note--><?pi data?></r>'
    )

    empty_text = XdmNode(
        NodeKind.TEXT,
        document=document,
        node_id=100,
        source_name=document.source_name,
        source_index=document.source_index,
        value="",
    )
    assert _serialize((empty_text,), omit_xml_declaration=True) == ""


def test_xml_doctype_with_and_without_public_identifier() -> None:
    document = _document(b"<root/>")

    assert (
        _serialize((document,), omit_xml_declaration=True, doctype_system="sys.dtd")
        == '<!DOCTYPE root SYSTEM "sys.dtd"><root/>'
    )
    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            doctype_public="pub",
            doctype_system="sys.dtd",
        )
        == '<!DOCTYPE root PUBLIC "pub" "sys.dtd"><root/>'
    )
    assert (
        _serialize((document,), omit_xml_declaration=True, doctype_system='sys"1')
        == "<!DOCTYPE root SYSTEM 'sys\"1'><root/>"
    )
    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            doctype_public="pub",
            doctype_system="sys\"1'2",
        )
        == '<!DOCTYPE root PUBLIC "pub" "sys&quot;1\'2"><root/>'
    )


def test_doctype_follows_leading_comments_and_precedes_the_root() -> None:
    document = _document(b"<!--before--><root/><!--after-->")

    assert (
        _serialize((document,), omit_xml_declaration=True, doctype_system="sys.dtd")
        == '<!--before--><!DOCTYPE root SYSTEM "sys.dtd"><root/><!--after-->'
    )
    assert _serialize((document,), indent=True, doctype_system="sys.dtd") == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!--before-->\n<!DOCTYPE root SYSTEM "sys.dtd">\n<root/>\n<!--after-->'
    )


def test_xml_indent_and_byte_exact_unindented_output() -> None:
    document = _document(b"<r>  <a> x </a>\n <b><c/></b>  </r>")

    assert _serialize((document,), omit_xml_declaration=True) == (
        "<r>  <a> x </a>\n <b><c/></b>  </r>"
    )
    assert _serialize((document,), omit_xml_declaration=True, indent=True) == (
        "<r>\n  <a> x </a>\n  <b>\n    <c/>\n  </b>\n</r>"
    )
    assert _serialize((document,), indent=True) == (
        '<?xml version="1.0" encoding="UTF-8"?>\n<r>\n  <a> x </a>\n  <b>\n    <c/>\n  </b>\n</r>'
    )

    padded = _document(b"<r/>")
    leading = XdmNode(
        NodeKind.TEXT,
        document=padded,
        node_id=100,
        source_name=padded.source_name,
        source_index=padded.source_index,
        value="\n",
    )
    trailing = XdmNode(
        NodeKind.TEXT,
        document=padded,
        node_id=101,
        source_name=padded.source_name,
        source_index=padded.source_index,
        value="\n",
    )
    sequence = (leading, padded.root_element, trailing)
    assert _serialize(sequence, omit_xml_declaration=True) == "\n<r/>\n"
    assert _serialize(sequence, omit_xml_declaration=True, indent=True) == "<r/>"
    assert _serialize(sequence, indent=True) == ('<?xml version="1.0" encoding="UTF-8"?>\n<r/>')


def test_xml_cdata_sections_and_embedded_terminator() -> None:
    document = _document(b"<r><script>a]]&gt;b</script><p>plain</p></r>")

    assert _serialize(
        (document,),
        omit_xml_declaration=True,
        cdata_section_elements=("script",),
    ) == ("<r><script><![CDATA[a]]]]><![CDATA[>b]]></script><p>plain</p></r>")

    namespaced = _document(b'<script xmlns="urn:x">a</script>')
    assert (
        _serialize(
            (namespaced,),
            omit_xml_declaration=True,
            cdata_section_elements=("Q{urn:x}script",),
        )
        == '<script xmlns="urn:x"><![CDATA[a]]></script>'
    )
    assert (
        _serialize(
            (namespaced,),
            omit_xml_declaration=True,
            cdata_section_elements=("script",),
        )
        == '<script xmlns="urn:x">a</script>'
    )
    assert (
        _serialize(
            (_document(b"<script>a</script>"),), method="html", cdata_section_elements=("script",)
        )
        == "<script>a</script>"
    )


def test_cdata_names_expand_using_stylesheet_namespaces() -> None:
    expanded = SerializationParameters.from_attributes(
        {"cdata-section-elements": "p:script other", "suppress-indentation": "p:block"},
        namespaces={"p": "urn:p", "": "urn:default"},
    )

    assert expanded.cdata_section_elements == ("Q{urn:p}script", "Q{urn:default}other")
    assert expanded.suppress_indentation == ("Q{urn:p}block",)

    unchanged = SerializationParameters.from_attributes(
        {"cdata-section-elements": "Q{urn:p}script {urn:c}local missing:x"}
    )
    assert unchanged.cdata_section_elements == (
        "Q{urn:p}script",
        "{urn:c}local",
        "missing:x",
    )

    already_expanded = SerializationParameters.from_attributes(
        {"cdata-section-elements": ["Q{urn:p}script", "missing:x"]},
        namespaces={"p": "urn:p"},
    )
    assert already_expanded.cdata_section_elements == ("Q{urn:p}script", "missing:x")

    assert (
        _serialize(
            (_document(b'<p:script xmlns:p="urn:p">a</p:script>'),),
            omit_xml_declaration=True,
            cdata_section_elements=("p:script",),
        )
        == '<p:script xmlns:p="urn:p">a</p:script>'
    )


def test_xml_top_level_item_separator_and_comment_only_document() -> None:
    document = _document(b"<r><!--one--><!--two--></r>")
    first, second = document.root_element.children

    assert (
        _serialize((first, second), omit_xml_declaration=True, item_separator="|")
        == "<!--one-->|<!--two-->"
    )


def test_xml_adjacent_atomics_and_zero_length_text() -> None:
    document = _document(b"<r/>")
    empty_text = XdmNode(
        NodeKind.TEXT,
        document=document,
        node_id=100,
        source_name=document.source_name,
        source_index=document.source_index,
        value="",
    )

    assert (
        _serialize((XdmAtomic.string("a"), empty_text, XdmAtomic.integer(2)), method="html")
        == "a 2"
    )
    assert _serialize((empty_text, XdmAtomic.string("a")), method="html") == "a"
    assert _serialize((XdmAtomic.string(""),), method="html") == ""


def test_xml_method_serializes_text_only_and_mixed_content() -> None:
    assert _serialize((XdmAtomic.string("a < b"),), omit_xml_declaration=True) == "a &lt; b"
    assert (
        _serialize((XdmAtomic.string("a"), XdmAtomic.integer(2)), omit_xml_declaration=True)
        == "a 2"
    )

    document = _document(b"<r><!--note--><?pi data?></r>")
    comment, instruction = document.root_element.children
    assert (
        _serialize((comment, XdmAtomic.string("text"), instruction), omit_xml_declaration=True)
        == "<!--note-->text<?pi data?>"
    )


def test_multiple_top_level_elements_raise_sere0003() -> None:
    first = _document(b"<one/>")
    second = _document(b"<two/>")

    with pytest.raises(XsltSerializationError) as error:
        _serialize((first.root_element, second.root_element), omit_xml_declaration=True)

    assert error.value.code == "SERE0003"


# ---------------------------------------------------------------------------
# XHTML


def test_xhtml_empty_element_form_and_script_escaping() -> None:
    document = _document(
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        b"<br/><script>a &lt; b &amp; c</script>"
        b"</body></html>"
    )

    assert _serialize((document,), method="xhtml", omit_xml_declaration=True) == (
        f'<html xmlns="{XHTML_NS}"><body><br /><script>a &lt; b &amp; c</script></body></html>'
    )


def test_xhtml_doctype_when_a_system_identifier_is_supplied() -> None:
    document = _document(b'<html xmlns="http://www.w3.org/1999/xhtml"/>')

    assert _serialize(
        (document,),
        method="xhtml",
        omit_xml_declaration=True,
        doctype_system="xhtml1-strict.dtd",
    ) == (f'<!DOCTYPE html SYSTEM "xhtml1-strict.dtd"><html xmlns="{XHTML_NS}"></html>')


# ---------------------------------------------------------------------------
# HTML


def test_html_void_elements_script_text_and_boolean_attributes() -> None:
    document = _document(
        b"<html><body>"
        b'<br/><img src="x"/><div></div>'
        b'<input checked="checked" disabled="" selected="selected" value="v"/>'
        b"<script>if (a &lt; b &amp;&amp; c) {}</script>"
        b"<p>x ]]&gt; y</p>"
        b"</body></html>"
    )

    assert _serialize((document,), method="html") == (
        "<html><body>"
        '<br><img src="x"><div></div>'
        '<input checked disabled selected value="v">'
        "<script>if (a < b && c) {}</script>"
        "<p>x ]]&gt; y</p>"
        "</body></html>"
    )


def test_html_doctype_uses_the_root_local_name() -> None:
    document = _document(b"<html><body/></html>")

    assert (
        _serialize(
            (document,),
            method="html",
            doctype_public="pub",
            doctype_system="sys",
        )
        == '<!DOCTYPE html PUBLIC "pub" "sys"><html><body></body></html>'
    )


def test_html_non_boolean_empty_attribute_is_not_minimized() -> None:
    document = _document(b'<p data-x=""/>')

    assert _serialize((document,), method="html") == '<p data-x=""></p>'


def test_html_boolean_value_and_top_level_text() -> None:
    document = _document(b'<input checked="true"/>')

    assert _serialize((document,), method="html") == '<input checked="true">'
    assert _serialize((XdmAtomic.string("a < b"),), method="html") == "a &lt; b"


# ---------------------------------------------------------------------------
# Suppression, content type, and namespace undeclaration


def _element_tree(
    *,
    root_scope: dict[str, str],
    child_scope: dict[str, str],
    root_name: str = "root",
    root_prefix: str = "",
    root_namespace: str | None = None,
) -> XdmNode:
    document = _document(b"<source/>")
    root = XdmNode(
        NodeKind.ELEMENT,
        document=document,
        node_id=100,
        source_name=document.source_name,
        source_index=document.source_index,
        name=root_name,
        prefix=root_prefix,
        namespace_uri=root_namespace,
        in_scope_namespaces=root_scope,
    )
    child = XdmNode(
        NodeKind.ELEMENT,
        document=document,
        node_id=101,
        source_name=document.source_name,
        source_index=document.source_index,
        parent=root,
        name="child",
        in_scope_namespaces=child_scope,
    )
    root._append_child(child)
    return root


def test_suppress_indentation_matches_expanded_names_at_any_depth() -> None:
    document = _document(
        b'<root xmlns:ex="urn:example">'
        b"<ex:block><wrap><child/></wrap></ex:block>"
        b"<plain><child/></plain>"
        b"</root>"
    )

    assert _serialize(
        (document,),
        omit_xml_declaration=True,
        indent=True,
        suppress_indentation=("Q{urn:example}block",),
    ) == (
        '<root xmlns:ex="urn:example">\n'
        "  <ex:block><wrap><child/></wrap></ex:block>\n"
        "  <plain>\n"
        "    <child/>\n"
        "  </plain>\n"
        "</root>"
    )
    assert _serialize(
        (document,),
        omit_xml_declaration=True,
        indent=True,
        suppress_indentation=("block",),
    ) == (
        '<root xmlns:ex="urn:example">\n'
        "  <ex:block>\n"
        "    <wrap>\n"
        "      <child/>\n"
        "    </wrap>\n"
        "  </ex:block>\n"
        "  <plain>\n"
        "    <child/>\n"
        "  </plain>\n"
        "</root>"
    )

    spaced = _document(b'<root xmlns:ex="urn:example"><ex:block> <wrap/> </ex:block></root>')
    assert (
        _serialize(
            (spaced,),
            omit_xml_declaration=True,
            indent=True,
            suppress_indentation=("Q{urn:example}block",),
        )
        == '<root xmlns:ex="urn:example">\n  <ex:block> <wrap/> </ex:block>\n</root>'
    )


def test_suppress_indentation_applies_to_xhtml_and_html_names() -> None:
    xhtml = _document(
        f'<html xmlns="{XHTML_NS}"><body>'
        "<div><p><span><b>x</b></span></p></div>"
        "<div><span><b>y</b></span></div>"
        "</body></html>".encode()
    )
    assert _serialize(
        (xhtml,),
        method="xhtml",
        omit_xml_declaration=True,
        indent=True,
        suppress_indentation=(f"Q{{{XHTML_NS}}}p",),
    ) == (
        f'<html xmlns="{XHTML_NS}">\n'
        "  <body>\n"
        "    <div>\n"
        "      <p><span><b>x</b></span></p>\n"
        "    </div>\n"
        "    <div>\n"
        "      <span>\n"
        "        <b>y</b>\n"
        "      </span>\n"
        "    </div>\n"
        "  </body>\n"
        "</html>"
    )

    html = _document(
        b"<html><body>"
        b"<div><table><tr><td>x</td></tr></table></div>"
        b"<div><child/></div>"
        b"</body></html>"
    )
    assert _serialize(
        (html,),
        method="html",
        indent=True,
        suppress_indentation=("table",),
    ) == (
        "<html>\n"
        "  <body>\n"
        "    <div>\n"
        "      <table><tr><td>x</td></tr></table>\n"
        "    </div>\n"
        "    <div>\n"
        "      <child></child>\n"
        "    </div>\n"
        "  </body>\n"
        "</html>"
    )


def test_html_content_type_meta_is_the_first_element_child() -> None:
    document = _document(b"<html><head><title>t</title></head><body/></html>")

    assert _serialize(
        (document,),
        method="html",
        media_type="text/html",
        encoding="UTF-8",
    ) == (
        "<html><head>"
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
        "<title>t</title>"
        "</head><body></body></html>"
    )

    text_head = _document(b"<html><head>the head</head></html>")
    assert _serialize((text_head,), method="html") == (
        "<html><head>the head"
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
        "</head></html>"
    )


def test_xhtml_content_type_meta_uses_the_head_namespace_and_declared_media() -> None:
    namespaced = _document(
        f'<html xmlns="{XHTML_NS}"><head><title>t</title></head><body/></html>'.encode()
    )
    assert _serialize(
        (namespaced,),
        method="xhtml",
        include_content_type=True,
        media_type="application/xhtml+xml",
        encoding="utf-8",
        omit_xml_declaration=True,
    ) == (
        f'<html xmlns="{XHTML_NS}"><head>'
        '<meta http-equiv="Content-Type" content="application/xhtml+xml; charset=utf-8" />'
        "<title>t</title></head><body></body></html>"
    )

    plain = _document(b"<html><head/></html>")
    assert (
        _serialize(
            (plain,),
            method="xhtml",
            omit_xml_declaration=True,
        )
        == "<html><head /></html>"
    )


def test_xhtml_null_namespace_head_is_an_xml_island_without_html5() -> None:
    document = _document(
        b'<html><head><meta http-equiv="Content-Type" content="old"/><title>t</title></head></html>'
    )
    expected = (
        '<html><head><meta http-equiv="Content-Type" content="old" /><title>t</title></head></html>'
    )

    assert _serialize((document,), method="xhtml", omit_xml_declaration=True) == expected
    # HTML 5.0 additionally treats null-namespace elements as HTML elements.
    assert _serialize(
        (document,),
        method="xhtml",
        omit_xml_declaration=True,
        **{"html-version": "5.0"},
    ) == "<!DOCTYPE html>" + expected.replace(
        '<meta http-equiv="Content-Type" content="old" />',
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />',
    )


def test_include_content_type_replaces_an_existing_meta_case_insensitively() -> None:
    document = _document(
        b"<html><head><!--keep-->"
        b'<meta http-equiv=" Content-Type " content="old"/>'
        b'<meta charset="utf-8"/>'
        b'<meta http-equiv="refresh" content="0"/>'
        b"<title>t</title></head></html>"
    )
    assert _serialize((document,), method="html", media_type="text/html") == (
        "<html><head><!--keep-->"
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
        '<meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0">'
        "<title>t</title></head></html>"
    )

    uppercase = _document(
        b'<HTML><HEAD><META HTTP-EQUIV=" Content-Type " CONTENT="old"/>'
        b"<TITLE>t</TITLE></HEAD><BODY/></HTML>"
    )
    assert _serialize((uppercase,), method="html") == (
        "<HTML><HEAD>"
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
        "<TITLE>t</TITLE></HEAD><BODY></BODY></HTML>"
    )


def test_include_content_type_is_disabled_and_does_not_apply_to_xml() -> None:
    document = _document(
        b'<html><head><meta http-equiv="Content-Type" content="old"/></head></html>'
    )

    assert _serialize((document,), method="html", include_content_type=False) == (
        '<html><head><meta http-equiv="Content-Type" content="old"></head></html>'
    )
    assert (
        _serialize(
            (_document(b"<html><head/></html>"),),
            method="xml",
            omit_xml_declaration=True,
            include_content_type=True,
            media_type="text/html",
        )
        == "<html><head/></html>"
    )


def test_include_content_type_ignores_foreign_elements_and_html_namespaces() -> None:
    foreign_meta = _document(
        (
            f'<html xmlns="{XHTML_NS}"><head xmlns:x="urn:x">'
            '<x:meta http-equiv="Content-Type" content="old"/>'
            "<title>t</title></head></html>"
        ).encode()
    )
    assert _serialize(
        (foreign_meta,),
        method="xhtml",
        omit_xml_declaration=True,
    ) == (
        f'<html xmlns="{XHTML_NS}"><head xmlns:x="urn:x">'
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />'
        '<x:meta http-equiv="Content-Type" content="old" />'
        "<title>t</title></head></html>"
    )

    foreign_head = _document(b'<html><x:head xmlns:x="urn:x"/></html>')
    assert (
        _serialize(
            (foreign_head,),
            method="xhtml",
            omit_xml_declaration=True,
        )
        == '<html><x:head xmlns:x="urn:x" /></html>'
    )

    html_namespaced = _document(f'<html xmlns="{XHTML_NS}"><head/></html>'.encode())
    assert _serialize((html_namespaced,), method="html") == (
        "<html><head>"
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
        "</head></html>"
    )


def test_include_content_type_indents_the_inserted_meta_with_the_head() -> None:
    document = _document(
        f'<html xmlns="{XHTML_NS}"><head><title>t</title></head><body/></html>'.encode()
    )

    assert _serialize(
        (document,),
        method="xhtml",
        omit_xml_declaration=True,
        indent=True,
    ) == (
        f'<html xmlns="{XHTML_NS}">\n'
        "  <head>\n"
        '    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />\n'
        "    <title>t</title>\n"
        "  </head>\n"
        "  <body></body>\n"
        "</html>"
    )


def test_xhtml_content_type_meta_declares_the_namespace_for_a_prefixed_head() -> None:
    document = _document(
        f'<h:html xmlns:h="{XHTML_NS}"><h:head><h:title>t</h:title></h:head></h:html>'.encode()
    )

    assert _serialize(
        (document,),
        method="xhtml",
        omit_xml_declaration=True,
    ) == (
        f'<h:html xmlns:h="{XHTML_NS}"><h:head>'
        f'<meta xmlns="{XHTML_NS}" http-equiv="Content-Type" '
        'content="text/html; charset=UTF-8" />'
        "<h:title>t</h:title></h:head></h:html>"
    )


def test_undeclare_prefixes_with_xml_11_emits_namespace_undeclarations() -> None:
    tree = _element_tree(
        root_scope={"p": "urn:p"},
        child_scope={},
        root_name="{urn:p}root",
        root_prefix="p",
        root_namespace="urn:p",
    )

    assert (
        _serialize(
            (tree,),
            omit_xml_declaration=True,
            version="1.1",
            undeclare_prefixes=True,
        )
        == '<p:root xmlns:p="urn:p"><child xmlns:p=""/></p:root>'
    )
    assert (
        _serialize(
            (tree,),
            omit_xml_declaration=True,
            version="1.1",
            undeclare_prefixes=False,
        )
        == '<p:root xmlns:p="urn:p"><child/></p:root>'
    )
    assert (
        _serialize(
            (tree,),
            method="xhtml",
            omit_xml_declaration=True,
            version="1.1",
            undeclare_prefixes=True,
        )
        == '<p:root xmlns:p="urn:p"><child xmlns:p="" /></p:root>'
    )

    retained = _element_tree(
        root_scope={"p": "urn:p"},
        child_scope={"p": "urn:p"},
        root_name="{urn:p}root",
        root_prefix="p",
        root_namespace="urn:p",
    )
    assert (
        _serialize(
            (retained,),
            omit_xml_declaration=True,
            version="1.1",
            undeclare_prefixes=True,
        )
        == '<p:root xmlns:p="urn:p"><child/></p:root>'
    )

    defaulted = _element_tree(root_scope={"": "urn:d"}, child_scope={})
    assert (
        _serialize(
            (defaulted,),
            omit_xml_declaration=True,
            version="1.1",
            undeclare_prefixes=True,
        )
        == '<root xmlns="urn:d"><child xmlns=""/></root>'
    )

    empty_binding = _element_tree(root_scope={"p": ""}, child_scope={})
    assert (
        _serialize(
            (empty_binding,),
            omit_xml_declaration=True,
            version="1.1",
            undeclare_prefixes=True,
        )
        == "<root><child/></root>"
    )


def test_undeclare_prefixes_with_version_1_is_sepm0010() -> None:
    tree = _element_tree(
        root_scope={"p": "urn:p"},
        child_scope={},
        root_name="{urn:p}root",
        root_prefix="p",
        root_namespace="urn:p",
    )

    cases = (
        lambda: _serialize(
            (tree,), omit_xml_declaration=True, version="1.0", undeclare_prefixes=True
        ),
        lambda: _serialize(
            (tree,),
            method="xhtml",
            omit_xml_declaration=True,
            version="1.0",
            undeclare_prefixes=True,
        ),
    )
    assert [_error_code(case) for case in cases] == ["SEPM0010", "SEPM0010"]

    assert (
        _serialize(
            (tree,),
            method="html",
            version="1.0",
            undeclare_prefixes=True,
        )
        == "<root><child></child></root>"
    )
    assert _serialize((tree,), method="text", version="1.0", undeclare_prefixes=True) == ""


# ---------------------------------------------------------------------------
# Parameters and errors


def test_merge_layers_later_layers_win() -> None:
    parameters = SerializationParameters.merge(
        {
            "method": "xml",
            "indent": "yes",
            "encoding": "utf-8",
            "doctype-system": "named.dtd",
            "build-tree": "no",
        },
        {"indent": "no", "doctype-system": "override.dtd"},
        {"indent": True, "omit_xml_declaration": True, "standalone": "omit"},
    )

    assert parameters.method == "xml"
    assert parameters.indent is True
    assert parameters.encoding == "utf-8"
    assert parameters.doctype_system == "override.dtd"
    assert parameters.omit_xml_declaration is True
    assert parameters.standalone is None


def test_parameter_defaults_and_eqname_methods() -> None:
    parameters = SerializationParameters()

    assert parameters.method == "xml"
    assert parameters.version == "1.0"
    assert parameters.encoding == "UTF-8"
    assert parameters.indent is False
    assert parameters.omit_xml_declaration is False
    assert parameters.standalone is None
    assert parameters.cdata_section_elements == ()
    assert parameters.suppress_indentation == ()
    assert parameters.escape_uri_attributes is True
    assert parameters.include_content_type is True
    assert parameters.byte_order_mark is False
    assert parameters.undeclare_prefixes is False
    assert SerializationParameters(method="Q{urn:x}html").method == "html"
    assert SerializationParameters(method="p:text").method == "text"


def test_parameter_list_values_accept_strings_and_sequences() -> None:
    from_strings = SerializationParameters(
        cdata_section_elements="a Q{urn:x}b",
        suppress_indentation="c  d",
    )
    from_sequence = SerializationParameters(
        cdata_section_elements=("a",),
        suppress_indentation=("b",),
    )
    normalized = SerializationParameters(
        indent="no",
        omit_xml_declaration="false",
        standalone="no",
        normalization_form="nfc",
        cdata_section_elements=None,
        doctype_system="",
        suppress_indentation=("c", ""),
        media_type="text/plain",
        item_separator="",
    )

    assert from_strings.cdata_section_elements == ("a", "Q{urn:x}b")
    assert from_strings.suppress_indentation == ("c", "d")
    assert from_sequence.cdata_section_elements == ("a",)
    assert from_sequence.suppress_indentation == ("b",)
    assert normalized.indent is False
    assert normalized.omit_xml_declaration is False
    assert normalized.standalone is False
    assert normalized.normalization_form == "NFC"
    assert normalized.cdata_section_elements == ()
    assert normalized.doctype_system is None
    assert normalized.suppress_indentation == ("c",)
    assert normalized.media_type == "text/plain"
    assert normalized.item_separator == ""
    assert SerializationParameters(normalization_form="none").normalization_form == "none"
    assert SerializationParameters(normalization_form="").normalization_form is None
    assert SerializationParameters(standalone="yes").standalone is True

    base = SerializationParameters(method="text", encoding="ascii")
    layered = SerializationParameters.from_attributes(
        {"build-tree": "no", "indent": "yes"}, base=base
    )
    assert layered.method == "text"
    assert layered.encoding == "ascii"
    assert layered.indent is True


def test_invalid_parameters_raise_sepm0016() -> None:
    cases = (
        lambda: SerializationParameters(method="yaml"),
        lambda: SerializationParameters(method="XML"),
        lambda: SerializationParameters(method=1),  # type: ignore[arg-type]
        lambda: SerializationParameters(method="Q{broken"),
        lambda: SerializationParameters(encoding=""),
        lambda: SerializationParameters(encoding=1),  # type: ignore[arg-type]
        lambda: SerializationParameters(version="-1"),
        lambda: SerializationParameters(version="latest"),
        lambda: SerializationParameters(version=""),
        lambda: SerializationParameters(version="nan"),
        lambda: SerializationParameters(version="inf"),
        lambda: SerializationParameters(normalization_form=1),  # type: ignore[arg-type]
        lambda: SerializationParameters(indent="maybe"),
        lambda: SerializationParameters(indent=1),  # type: ignore[arg-type]
        lambda: SerializationParameters(standalone="sometimes"),
        lambda: SerializationParameters(standalone=1),  # type: ignore[arg-type]
        lambda: SerializationParameters(byte_order_mark="perhaps"),
        lambda: SerializationParameters(media_type=1),  # type: ignore[arg-type]
        lambda: SerializationParameters(cdata_section_elements=5),  # type: ignore[arg-type]
        lambda: SerializationParameters(cdata_section_elements=("a", 1)),  # type: ignore[arg-type]
        lambda: SerializationParameters(version=True),
        lambda: SerializationParameters.merge({"bogus-attribute": "x"}),
        lambda: SerializationParameters.merge({"xml:lang": "en"}),
        lambda: SerializationParameters.from_attributes(
            {"cdata-section-elements": ("p:a", 1)}, namespaces={"p": "urn:p"}
        ),
        lambda: SerializationParameters.from_attributes(
            {"cdata-section-elements": 5}, namespaces={"p": "urn:p"}
        ),
    )

    codes = [_error_code(case) for case in cases]

    assert codes == ["SEPM0016"] * len(cases)

    with pytest.raises(XsltSerializationError) as encoding_error:
        SerializationParameters(encoding="not-a-real-encoding")
    assert encoding_error.value.code == "SESU0007"
    with pytest.raises(XsltSerializationError) as normalization_error:
        SerializationParameters(normalization_form="NFX")
    assert normalization_error.value.code == "SESU0011"


def test_non_ascii_result_uses_character_references_in_text_and_attributes() -> None:
    document = _document('<r a="café">café</r>'.encode())

    assert _serialize((document,), omit_xml_declaration=True, encoding="ascii") == (
        '<r a="caf&#xE9;">caf&#xE9;</r>'
    )
    assert _serialize((document,), omit_xml_declaration=True, encoding="utf-8") == (
        '<r a="café">café</r>'
    )

    destination = io.BytesIO()
    Serializer.write(
        (document,),
        SerializationParameters(omit_xml_declaration=True, encoding="ascii"),
        destination,
    )
    assert destination.getvalue() == b'<r a="caf&#xE9;">caf&#xE9;</r>'


def test_unrepresentable_non_text_contexts_raise_sere0008() -> None:
    comment_document = _document("<r><!--café--></r>".encode())
    comment = comment_document.root_element.children[0]
    element_document = _document("<café/>".encode())
    cdata_document = _document("<r><x>café</x></r>".encode())
    pi_document = _document("<r><?pi café?></r>".encode())
    instruction = pi_document.root_element.children[0]

    cases = (
        lambda: _serialize((comment,), omit_xml_declaration=True, encoding="ascii"),
        lambda: _serialize((element_document,), omit_xml_declaration=True, encoding="ascii"),
        lambda: _serialize((instruction,), omit_xml_declaration=True, encoding="ascii"),
    )

    codes = [_error_code(case) for case in cases]

    assert codes == ["SERE0008"] * len(cases)
    # A CDATA section splits around an unrepresentable character instead.
    assert (
        _serialize(
            (cdata_document,),
            omit_xml_declaration=True,
            encoding="ascii",
            cdata_section_elements=("x",),
        )
        == "<r><x><![CDATA[caf]]>&#xE9;</x></r>"
    )


def test_invalid_sequences_raise_senr0001() -> None:
    first = _document(b"<one/>")

    def function_item() -> str:
        function = XdmFunction(lambda arguments, environment, closure: (), arity=0)
        return _serialize((function,), omit_xml_declaration=True)

    def map_item() -> str:
        return _serialize((XdmMap.from_mapping({"k": 1}),), omit_xml_declaration=True)

    def namespace_child() -> str:
        element = XdmNode(
            NodeKind.ELEMENT,
            document=first,
            node_id=100,
            source_name=first.source_name,
            source_index=first.source_index,
            name="root",
            in_scope_namespaces={"": ""},
        )
        element._append_child(first.root_element.namespace_nodes[0])
        return _serialize((element,), omit_xml_declaration=True)

    cases = (
        function_item,
        map_item,
        namespace_child,
    )

    with pytest.raises(XsltSerializationError) as attribute_error:
        _serialize(
            (_document(b"<r a='1'/>").root_element.attributes[0],), omit_xml_declaration=True
        )
    assert attribute_error.value.code == "SENR0001"

    codes = [_error_code(case) for case in cases]
    assert codes == ["SENR0001"] * len(cases)


def test_serialize_accepts_a_mapping_of_parameters() -> None:
    document = _document(b"<root/>")

    assert (
        Serializer.serialize((document,), {"method": "xml", "omit-xml-declaration": "yes"})
        == "<root/>"
    )
    assert Serializer.serialize((document,)) == ('<?xml version="1.0" encoding="UTF-8"?><root/>')
    with pytest.raises(TypeError, match="parameters"):
        Serializer.serialize((document,), object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="destination"):
        Serializer.write((document,), None, object())


# ---------------------------------------------------------------------------
# Stylesheet delivery integration


def test_stylesheet_delivery_serializes_principal_and_secondary() -> None:
    source = stylesheet(
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">'
        "<principal><xsl:value-of select=\"'x'\"/></principal>"
        '<xsl:result-document href="secondary.txt" method="text">secondary</xsl:result-document>'
        "</xsl:template>"
    )

    result = transform(
        source,
        delivery="stylesheet",
        output_uri="http://example.com/principal.xml",
    )

    assert result.principal_entry.serialized == (
        '<principal xmlns:xs="http://www.w3.org/2001/XMLSchema">x</principal>'
    )
    secondary = result.secondary["http://example.com/secondary.txt"]
    assert secondary.serialized == "secondary"


def test_document_delivery_leaves_serialized_empty() -> None:
    source = stylesheet(
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">'
        "<principal/>"
        '<xsl:result-document href="secondary.xml"><secondary/></xsl:result-document>'
        "</xsl:template>"
    )

    result = transform(
        source,
        delivery="document",
        output_uri="http://example.com/principal.xml",
    )

    assert result.principal_entry.serialized is None
    secondary = result.secondary["http://example.com/secondary.xml"]
    assert secondary.serialized is None


def test_stylesheet_delivery_serializes_json_and_adaptive_methods() -> None:
    source = stylesheet(
        '<xsl:output method="json" build-tree="no"/>'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:sequence select=\"map{'k': 1}\"/>"
        "</xsl:template>"
    )

    result = transform(source, delivery="stylesheet")

    assert result.principal_entry.serialized == '{"k":1}'
    assert result.principal_entry.serialization["method"] == "json"


def test_stylesheet_delivery_serializes_a_text_only_xml_result() -> None:
    source = stylesheet(
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">just text</xsl:template>'
    )

    result = transform(source, delivery="stylesheet")

    assert result.principal_entry.serialized == "just text"


def test_a_foreign_namespace_output_attribute_is_ignored() -> None:
    source = stylesheet(
        '<xsl:output xmlns:ext="urn:ext" method="xml" '
        'omit-xml-declaration="yes" ext:unknown="x"/>'
        '<xsl:template name="xsl:initial-template"><out/></xsl:template>'
    )

    result = transform(source, delivery="stylesheet")

    assert result.principal_entry.serialized == (
        '<out xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
    )


def test_stylesheet_cdata_prefixed_qname_matches_a_namespaced_result() -> None:
    source = stylesheet(
        '<xsl:output xmlns:p="urn:p" method="xml" omit-xml-declaration="yes" '
        'cdata-section-elements="Q{urn:q}x {urn:r}y p:script script missing:z"/>'
        '<xsl:template name="xsl:initial-template">'
        '<p:script xmlns:p="urn:p">a</p:script>'
        "</xsl:template>"
    )

    result = transform(source, delivery="stylesheet")

    assert result.principal_entry.serialized == (
        '<p:script xmlns:p="urn:p" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema"><![CDATA[a]]></p:script>'
    )


def test_secondary_serialization_errors_are_marked_secondary() -> None:
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<principal/>"
        '<xsl:result-document href="secondary.xml" standalone="TRUE">'
        "<secondary/>"
        "</xsl:result-document>"
        "</xsl:template>"
    )

    with pytest.raises(XsltSerializationError) as error:
        transform(
            source,
            delivery="stylesheet",
            output_uri="http://example.com/principal.xml",
        )

    assert error.value.code == "SEPM0016"
    assert error.value.secondary is True


# ---------------------------------------------------------------------------
# Module-local output QNames and serializer capability


def _write_module(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def compile_imported_output_without_default_namespace(tmp_path: Path) -> Any:
    """Compile a principal whose imported module declares a bare output name.

    The importing module binds a default namespace, while the imported module
    declares ``cdata-section-elements="script"`` with no default namespace.
    """
    imported = _write_module(
        tmp_path,
        "imported-output.xsl",
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:output method="xml" omit-xml-declaration="yes" '
        'cdata-section-elements="script"/>'
        '<xsl:template name="xsl:initial-template"><script>text</script></xsl:template>'
        "</xsl:stylesheet>",
    )
    principal = _write_module(
        tmp_path,
        "principal-output.xsl",
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns="urn:principal" version="3.0">'
        f'<xsl:import href="{imported.name}"/>'
        "</xsl:stylesheet>",
    )
    return XsltProcessor().compile(principal)


def compile_result_document_with_instruction_local_cdata_prefix() -> Any:
    """Compile a result-document whose output QNames use an element-local prefix."""
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{SECONDARY_URI}" xmlns:p="urn:p" '
        'cdata-section-elements="p:script missing:z" suppress-indentation="p:block">'
        "<p:script>text</p:script>"
        "</xsl:result-document>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    return XsltProcessor().compile(io.BytesIO(source.encode()))


def test_imported_cdata_name_is_not_reexpanded_with_principal_namespaces(
    tmp_path: Path,
) -> None:
    result = compile_imported_output_without_default_namespace(tmp_path).transform()
    assert result.principal_entry.serialized == "<script><![CDATA[text]]></script>"


def test_result_document_cdata_uses_instruction_namespaces() -> None:
    result = compile_result_document_with_instruction_local_cdata_prefix().transform()
    entry = result.secondary[SECONDARY_URI]
    assert entry.serialized == '<p:script xmlns:p="urn:p"><![CDATA[text]]></p:script>'
    assert entry.serialization["cdata-section-elements"] == "Q{urn:p}script missing:z"
    assert entry.serialization["suppress-indentation"] == "Q{urn:p}block"


def compile_imported_format_prefix_output(tmp_path: Path) -> Any:
    """Compile a principal importing a module whose format prefix is module-local."""
    imported = _write_module(
        tmp_path,
        "imported-format.xsl",
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:f="urn:imported-format" version="3.0">'
        '<xsl:output name="f:foo" method="text"/>'
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{SECONDARY_URI}" format="f:foo">text</xsl:result-document>'
        "</xsl:template>"
        "</xsl:stylesheet>",
    )
    principal = _write_module(
        tmp_path,
        "principal-format.xsl",
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns="urn:principal" version="3.0">'
        f'<xsl:import href="{imported.name}"/>'
        "</xsl:stylesheet>",
    )
    return XsltProcessor().compile(principal)


def compile_instruction_local_format_prefix_output() -> Any:
    """Compile a result-document whose format prefix is bound only there.

    The output declaration binds its own prefix on the declaration, so neither
    prefix appears in the package root's namespace map.
    """
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:output xmlns:q="urn:local-format" name="q:foo" method="text"/>'
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{SECONDARY_URI}" xmlns:p="urn:local-format" '
        'format="p:foo">text</xsl:result-document>'
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    return XsltProcessor().compile(io.BytesIO(source.encode()))


def test_imported_format_prefix_resolves_with_declaring_module_namespaces(
    tmp_path: Path,
) -> None:
    result = compile_imported_format_prefix_output(tmp_path).transform()
    entry = result.secondary[SECONDARY_URI]
    assert entry.serialization["method"] == "text"
    assert entry.serialized == "text"


def test_result_document_format_uses_instruction_namespaces() -> None:
    result = compile_instruction_local_format_prefix_output().transform()
    entry = result.secondary[SECONDARY_URI]
    assert entry.serialization["method"] == "text"
    assert entry.serialized == "text"


def test_result_document_format_unbound_prefix_raises_xtse0280() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{SECONDARY_URI}" format="missing:foo">'
        "text</xsl:result-document>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(source.encode()))

    with pytest.raises(XsltStaticError) as error:
        compiled.transform()

    assert error.value.code == "XTSE0280"


def test_result_document_format_unknown_expanded_name_raises_xtde1460() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:output name="Q{urn:known}foo" method="text"/>'
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{SECONDARY_URI}" xmlns:p="urn:known" '
        'format="p:bar">text</xsl:result-document>'
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(source.encode()))

    with pytest.raises(XsltError) as error:
        compiled.transform()

    assert error.value.code == "XTDE1460"


def test_serialization_method_support_is_owned_by_the_serializer() -> None:
    assert Serializer.supports_method("xml")
    assert Serializer.supports_method("xhtml")
    assert Serializer.supports_method("html")
    assert Serializer.supports_method("text")
    assert Serializer.supports_method("Q{urn:xslt}text")
    assert Serializer.supports_method("xsl:html")
    assert Serializer.supports_method("json")
    assert Serializer.supports_method("adaptive")
    assert not Serializer.supports_method("yaml")


# ---------------------------------------------------------------------------
# Serialization 3.1 output-method and error taxonomy


def test_unsupported_encoding_is_a_serialization_error_not_a_parameter_error() -> None:
    with pytest.raises(XsltSerializationError) as error:
        SerializationParameters(encoding="not-an-encoding")
    assert error.value.code == "SESU0007"


def test_unsupported_normalization_form_is_sesu0011() -> None:
    with pytest.raises(XsltSerializationError) as error:
        SerializationParameters(normalization_form="ABC")
    assert error.value.code == "SESU0011"


def test_omit_xml_declaration_conflicts_are_sepm0009() -> None:
    for values in (
        {"omit_xml_declaration": True, "standalone": True},
        {"omit_xml_declaration": True, "version": "1.1", "doctype_system": "out.dtd"},
    ):
        with pytest.raises(XsltSerializationError) as error:
            SerializationParameters(**values)
        assert error.value.code == "SEPM0009"

    # A 1.0 doctype is permitted without an XML declaration.
    assert (
        SerializationParameters(
            omit_xml_declaration=True, version="1.0", doctype_system="out.dtd"
        ).doctype_system
        == "out.dtd"
    )


def test_standalone_accepts_only_the_serialization_lexical_space() -> None:
    assert SerializationParameters(standalone="yes").standalone is True
    assert SerializationParameters(standalone="0").standalone is False
    assert SerializationParameters(standalone="omit").standalone is None
    with pytest.raises(XsltSerializationError) as error:
        SerializationParameters(standalone="TRUE")
    assert error.value.code == "SEPM0016"


def test_output_version_is_a_version_alias() -> None:
    parameters = SerializationParameters.from_attributes({"output-version": "1.1"})
    assert parameters.version == "1.1"


def test_doctype_public_without_system_is_ignored() -> None:
    document = _document(b'<html xmlns="http://www.w3.org/1999/xhtml"/>')
    assert (
        _serialize(
            (document,),
            method="xhtml",
            omit_xml_declaration=True,
            doctype_public="-//BOAG//DTD Websites V1.3//EN",
        )
        == f'<html xmlns="{XHTML_NS}"></html>'
    )


def test_xhtml_non_void_empty_elements_use_explicit_end_tags() -> None:
    document = _document(
        f'<html xmlns="{XHTML_NS}"><head><title/></head><body><br/><p/></body></html>'.encode()
    )
    assert _serialize(
        (document,),
        method="xhtml",
        omit_xml_declaration=True,
        include_content_type=False,
    ) == (f'<html xmlns="{XHTML_NS}"><head><title></title></head><body><br /><p></p></body></html>')


def test_xhtml_text_escapes_greater_than_and_script_text() -> None:
    document = _document(
        f'<html xmlns="{XHTML_NS}"><body><script>a &lt; b &gt; c</script>'
        "<p>&gt;&gt;&gt;</p></body></html>".encode()
    )
    assert _serialize((document,), method="xhtml", omit_xml_declaration=True) == (
        f'<html xmlns="{XHTML_NS}"><body><script>a &lt; b &gt; c</script>'
        "<p>&gt;&gt;&gt;</p></body></html>"
    )


def test_xhtml_attribute_uses_numeric_quote_and_angle_bracket_references() -> None:
    document = _document(
        f'<a xmlns="{XHTML_NS}" href="x &quot; &lt; &gt; &amp;" accesskey="&#x96;"/>'.encode()
    )
    assert _serialize((document,), method="xhtml", omit_xml_declaration=True) == (
        f'<a xmlns="{XHTML_NS}" href="x &#34; &lt; &gt; &amp;" accesskey="&#x96;"></a>'
    )


def test_xhtml_c1_controls_in_text_become_numeric_references() -> None:
    document = _document(f'<p xmlns="{XHTML_NS}">&#x96;</p>'.encode())
    assert _serialize((document,), method="xhtml", omit_xml_declaration=True) == (
        f'<p xmlns="{XHTML_NS}">&#x96;</p>'
    )


def test_html_c1_control_is_sere0014() -> None:
    assert (
        _error_code(lambda: _serialize((_document(b"<doc>&#x9f;</doc>"),), method="html"))
        == "SERE0014"
    )


def test_html_processing_instruction_with_greater_than_is_sere0015() -> None:
    assert (
        _error_code(
            lambda: _serialize(
                (_document(b"<doc><?pi a > b?></doc>"),),
                method="html",
            )
        )
        == "SERE0015"
    )


def test_cdata_sections_split_around_unencodable_characters() -> None:
    document = _document(b"<r><x>foo \xc2\xaa bar</x></r>")
    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            encoding="US-ASCII",
            cdata_section_elements=("x",),
        )
        == "<r><x><![CDATA[foo ]]>&#xAA;<![CDATA[ bar]]></x></r>"
    )


def test_multiple_top_level_elements_need_a_delivered_result_tree() -> None:
    first = _document(b"<one/>").root_element
    second = _document(b"<two/>").root_element

    assert _error_code(lambda: Serializer.serialize((first, second))) == "SERE0003"
    assert (
        Serializer.serialize(
            (first, second),
            SerializationParameters.merge(explicit={"omit_xml_declaration": True}),
            result_tree=True,
        )
        == "<one/><two/>"
    )
    assert (
        _error_code(
            lambda: Serializer.serialize(
                (first, second),
                SerializationParameters.merge(explicit={"standalone": True}),
                result_tree=True,
            )
        )
        == "SEPM0004"
    )


def test_xhtml5_doctype_void_elements_and_empty_end_tags() -> None:
    document = _document(
        f'<html xmlns="{XHTML_NS}"><head><title/></head><body><br/><p/></body></html>'.encode()
    )
    serialized = _serialize(
        (document,),
        method="xhtml",
        html_version="5.0",
        omit_xml_declaration=True,
        include_content_type=False,
    )
    assert serialized == (
        "<!DOCTYPE html>"
        f'<html xmlns="{XHTML_NS}"><head><title></title></head>'
        "<body><br /><p></p></body></html>"
    )


def test_xhtml5_strips_xhtml_prefixes_and_defaults_foreign_namespaces() -> None:
    document = _document(
        (
            f'<h:html xmlns:h="{XHTML_NS}" xmlns:s="http://www.w3.org/2000/svg">'
            "<h:head><h:title>t</h:title></h:head>"
            "<h:body><s:svg><s:circle/></s:svg></h:body></h:html>"
        ).encode()
    )
    serialized = _serialize(
        (document,),
        method="xhtml",
        html_version="5.0",
        omit_xml_declaration=True,
        include_content_type=False,
    )
    assert serialized == (
        f'<!DOCTYPE html><html xmlns="{XHTML_NS}"><head><title>t</title></head>'
        '<body><svg xmlns="http://www.w3.org/2000/svg"><circle /></svg></body></html>'
    )


def test_xhtml5_treats_no_namespace_elements_as_html() -> None:
    document = _document(b"<html><head><title/></head><body><br/><p/></body></html>")
    serialized = _serialize(
        (document,),
        method="xhtml",
        html_version="5.0",
        omit_xml_declaration=True,
        include_content_type=False,
    )
    assert serialized == (
        "<!DOCTYPE html><html><head><title></title></head><body><br /><p></p></body></html>"
    )


def test_xhtml5_doctype_needs_an_html_root() -> None:
    document = _document(b'<html xmlns="urn:not-xhtml"/>')
    assert (
        _serialize(
            (document,),
            method="xhtml",
            html_version="5.0",
            omit_xml_declaration=True,
        )
        == '<html xmlns="urn:not-xhtml" />'
    )


def test_method_value_is_whitespace_trimmed() -> None:
    document = _document(f'<html xmlns="{XHTML_NS}"/>'.encode())
    assert (
        _serialize(
            (document,),
            method=" xhtml ",
            omit_xml_declaration=True,
            include_content_type=False,
        )
        == f'<html xmlns="{XHTML_NS}"></html>'
    )


def test_html_version_must_be_decimal_and_supported() -> None:
    with pytest.raises(XsltSerializationError) as invalid:
        SerializationParameters(method="xhtml", html_version="five")
    assert invalid.value.code == "SEPM0016"

    with pytest.raises(XsltSerializationError) as unsupported:
        SerializationParameters(method="html", version="0.0")
    assert unsupported.value.code == "SESU0013"


# ---------------------------------------------------------------------------
# Output declarations, secondary results, and the current output URI


def test_default_initial_template_uses_the_standard_name_not_a_prefix() -> None:
    source = (
        f'<t:transform xmlns:t="{XSLT_NS}" version="3.0">'
        '<t:template name="t:initial-template"><out/></t:template>'
        "</t:transform>"
    )
    result = transform(source.encode(), delivery="stylesheet")
    assert result.principal_entry.serialized == ('<?xml version="1.0" encoding="UTF-8"?><out/>')


def test_current_output_uri_tracks_principal_and_secondary_destinations() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        '<principal><xsl:value-of select="current-output-uri()"/></principal>'
        f'<xsl:result-document href="{SECONDARY_URI}" method="xml">'
        '<secondary><xsl:value-of select="current-output-uri()"/></secondary>'
        "</xsl:result-document>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    principal_uri = "http://example.com/principal.xml"
    result = transform(source.encode(), delivery="stylesheet", output_uri=principal_uri)
    assert principal_uri in (result.principal_entry.serialized or "")
    secondary = result.secondary[SECONDARY_URI]
    assert SECONDARY_URI in (secondary.serialized or "")


def test_current_output_uri_is_empty_without_a_base_output_uri() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        '<out><xsl:value-of select="empty(current-output-uri())"/></out>'
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    result = transform(source.encode(), delivery="stylesheet")
    assert "<out>true</out>" in (result.principal_entry.serialized or "")


def test_result_document_output_version_is_a_version_alias() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{SECONDARY_URI}" output-version="1.1">'
        "<secondary/>"
        "</xsl:result-document>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    result = transform(source.encode(), delivery="stylesheet")
    assert 'version="1.1"' in (result.secondary[SECONDARY_URI].serialized or "")


def test_result_document_absent_attribute_removes_the_named_format_value() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:output name="f" method="xml" item-separator="|" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{SECONDARY_URI}" format="f" item-separator="#absent">'
        "<one/><two/>"
        "</xsl:result-document>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    result = transform(source.encode(), delivery="stylesheet")
    entry = result.secondary[SECONDARY_URI]
    assert "item-separator" not in entry.serialization
    assert entry.serialized == "<one/><two/>"


def test_result_document_without_method_infers_xhtml_and_html() -> None:
    xhtml_uri = "http://example.com/xhtml.xml"
    html_uri = "http://example.com/html.xml"
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{xhtml_uri}">'
        f'<html xmlns="{XHTML_NS}"><head/><body/></html>'
        "</xsl:result-document>"
        f'<xsl:result-document href="{html_uri}">'
        "<html><head/><body/></html>"
        "</xsl:result-document>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    result = transform(source.encode(), delivery="stylesheet")
    xhtml_entry = result.secondary[xhtml_uri]
    html_entry = result.secondary[html_uri]
    assert "method" not in xhtml_entry.serialization
    assert (xhtml_entry.serialized or "").startswith('<?xml version="1.0"')
    assert '<meta http-equiv="Content-Type"' in (xhtml_entry.serialized or "")
    assert not (html_entry.serialized or "").startswith("<?xml")
    assert (html_entry.serialized or "").startswith("<html><head>")


def test_invalid_constant_html_version_is_a_static_error() -> None:
    output_source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:output method="xhtml" html-version="five"/>'
        "</xsl:stylesheet>"
    )
    with pytest.raises(XsltStaticError) as output_error:
        XsltProcessor().compile(io.BytesIO(output_source.encode()))
    assert output_error.value.code == "XTSE0020"

    document_source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:result-document href="{SECONDARY_URI}" html-version="five"/>'
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    with pytest.raises(XsltStaticError) as document_error:
        XsltProcessor().compile(io.BytesIO(document_source.encode()))
    assert document_error.value.code == "XTSE0020"


def test_principal_and_secondary_use_their_own_output_methods() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:output method="xml"/>'
        '<xsl:template name="xsl:initial-template">'
        "<principal>text</principal>"
        f'<xsl:result-document href="{SECONDARY_URI}" method="text">'
        "<secondary>text</secondary>"
        "</xsl:result-document>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    result = transform(source.encode(), delivery="stylesheet")
    assert (result.principal_entry.serialized or "").startswith('<?xml version="1.0"')
    assert result.secondary[SECONDARY_URI].serialized == "text"


def test_disable_output_escaping_serializes_marked_text_unescaped() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">'
        '<out><xsl:text disable-output-escaping="yes">&lt;raw&gt;</xsl:text>'
        "<xsl:text>&lt;escaped&gt;</xsl:text></out>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    result = transform(source.encode(), delivery="stylesheet")
    assert result.principal_entry.serialized == "<out><raw>&lt;escaped&gt;</out>"


def test_disable_output_escaping_is_lost_through_a_variable_binding() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">'
        '<out><xsl:variable name="x" as="text()">'
        '<xsl:apply-templates select="\'&lt;\'" mode="doe"/>'
        "</xsl:variable>"
        '<xsl:copy-of select="$x"/>'
        '<xsl:value-of select="\'&lt;\'" disable-output-escaping="yes"/>'
        "</out>"
        "</xsl:template>"
        '<xsl:template match="." mode="doe" as="text()">'
        '<xsl:value-of select="." disable-output-escaping="yes"/>'
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    result = transform(source.encode(), delivery="stylesheet")
    assert result.principal_entry.serialized == "<out>&lt;<</out>"


def test_invalid_disable_output_escaping_value_is_xtse0020() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        '<xsl:value-of select="1" disable-output-escaping="YES"/>'
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    with pytest.raises(XsltStaticError) as error:
        XsltProcessor().compile(io.BytesIO(source.encode()))
    assert error.value.code == "XTSE0020"


def test_html5_foreign_prefixes_and_default_namespaces() -> None:
    document = _document(
        b"<html><body>"
        b'<n:zzz xmlns:n="NamespaceN">foreign</n:zzz>'
        b'<p xmlns:m="NamespaceM" class="x" m:zzz="value">attribute</p>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="1"/>'
        b"<input/>"
        b"</body></html>"
    )
    serialized = _serialize(
        (document,),
        method="html",
        html_version="5.0",
        omit_xml_declaration=True,
        include_content_type=False,
    )
    assert serialized.startswith(
        '<!DOCTYPE html><html xmlns:n="NamespaceN" xmlns:m="NamespaceM"><body>'
    )
    assert "<n:zzz>foreign</n:zzz>" in serialized
    assert '<p class="x" m:zzz="value">attribute</p>' in serialized
    assert '<svg xmlns="http://www.w3.org/2000/svg" width="1">' in serialized
    assert "<input>" in serialized


def test_xhtml5_attribute_prefixes_are_declared_where_used() -> None:
    document = _document(
        (
            f'<html xmlns="{XHTML_NS}"><head/><body>'
            '<a xmlns:xlink="urn:link" xlink:href="target">text</a>'
            "</body></html>"
        ).encode()
    )
    assert _serialize(
        (document,),
        method="xhtml",
        html_version="5.0",
        omit_xml_declaration=True,
        include_content_type=False,
    ) == (
        f'<!DOCTYPE html><html xmlns="{XHTML_NS}"><head></head><body>'
        '<a xmlns:xlink="urn:link" xlink:href="target">text</a>'
        "</body></html>"
    )


def test_xhtml5_doctype_requires_the_html_local_name() -> None:
    document = _document(f'<div xmlns="{XHTML_NS}"/>'.encode())
    assert (
        _serialize(
            (document,),
            method="xhtml",
            html_version="5.0",
            omit_xml_declaration=True,
        )
        == f'<div xmlns="{XHTML_NS}"></div>'
    )


def test_fully_normalized_result_rejects_a_leading_combining_character() -> None:
    document = _document("<r>\u0301leading</r>".encode())
    assert (
        _error_code(
            lambda: _serialize(
                (document,),
                omit_xml_declaration=True,
                normalization_form="fully-normalized",
            )
        )
        == "SERE0012"
    )


def test_component_result_type_drops_disable_output_escaping() -> None:
    source = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">'
        '<out><xsl:call-template name="raw"/></out>'
        "</xsl:template>"
        '<xsl:template name="raw" as="text()">'
        '<xsl:value-of select="\'&lt;\'" disable-output-escaping="yes"/>'
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    result = transform(source.encode(), delivery="stylesheet")
    assert result.principal_entry.serialized == "<out>&lt;</out>"


def test_public_identifier_and_html_version_validation_branches() -> None:
    with pytest.raises(XsltSerializationError) as public_error:
        SerializationParameters(doctype_public="\u00a3")
    assert public_error.value.code == "SEPM0016"

    for value in (True, object(), "-1", "nan"):
        with pytest.raises(XsltSerializationError) as html_error:
            SerializationParameters(method="xhtml", html_version=value)  # type: ignore[arg-type]
        assert html_error.value.code == "SEPM0016"

    assert SerializationParameters(html_version="   ").html_version is None

    with pytest.raises(TypeError, match="legacy"):
        SerializationParameters(legacy="yes")  # type: ignore[arg-type]


def test_cdata_section_starting_with_an_unrepresentable_character() -> None:
    document = _document("<r><x>\u00aa tail</x></r>".encode())
    assert (
        _serialize(
            (document,),
            omit_xml_declaration=True,
            encoding="US-ASCII",
            cdata_section_elements=("x",),
        )
        == "<r><x>&#xAA;<![CDATA[ tail]]></x></r>"
    )


def test_html5_doctype_with_a_declared_system_identifier() -> None:
    document = _document(b'<html xmlns="http://www.w3.org/1999/xhtml"/>')
    assert _serialize(
        (document,),
        method="xhtml",
        html_version="5.0",
        omit_xml_declaration=True,
        doctype_system="x.dtd",
    ) == (f'<!DOCTYPE html SYSTEM "x.dtd"><html xmlns="{XHTML_NS}"></html>')


def test_a_top_level_text_node_marked_unescaped_is_not_reencoded() -> None:
    from pyxsd.xslt.runtime.builder import _NodeFactory

    text = _NodeFactory().text("<raw>", disable_output_escaping=True)
    assert _serialize((text,), omit_xml_declaration=True) == "<raw>"
