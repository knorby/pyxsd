"""Serialization parameter resolution and the XML-family output methods.

These tests operate directly on XDM values and nodes, so serialization is
exercised independently of stylesheet execution.  A separate section covers
the small integration seam that populates ``ResultEntry.serialized`` for
stylesheet delivery.
"""

from __future__ import annotations

import pytest
from tests.xslt.support import stylesheet, transform

from pyxsd.xdm import XdmAtomic, XdmFunction, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt.errors import XsltSerializationError
from pyxsd.xslt.serialization import SerializationParameters, Serializer

XHTML_NS = "http://www.w3.org/1999/xhtml"


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


def test_namespace_nodes_are_dropped_and_a_document_is_replaced_by_children() -> None:
    document = _document(b'<r xmlns="urn:default">text<!--note--><?pi data?></r>')

    namespace = document.root_element.namespace_nodes[0]
    assert _serialize((namespace,), omit_xml_declaration=True) == ""
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
    ) == (f'<!DOCTYPE html SYSTEM "xhtml1-strict.dtd"><html xmlns="{XHTML_NS}" />')


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
    assert SerializationParameters(standalone="YES").standalone is True

    base = SerializationParameters(method="text", encoding="ascii")
    layered = SerializationParameters.from_attributes(
        {"build-tree": "no", "indent": "yes"}, base=base
    )
    assert layered.method == "text"
    assert layered.encoding == "ascii"
    assert layered.indent is True


def test_invalid_parameters_raise_sepm0016() -> None:
    cases = (
        lambda: SerializationParameters(method="json"),
        lambda: SerializationParameters(method="XML"),
        lambda: SerializationParameters(method=1),  # type: ignore[arg-type]
        lambda: SerializationParameters(method="Q{broken"),
        lambda: SerializationParameters(encoding="not-a-real-encoding"),
        lambda: SerializationParameters(encoding=""),
        lambda: SerializationParameters(encoding=1),  # type: ignore[arg-type]
        lambda: SerializationParameters(version="-1"),
        lambda: SerializationParameters(version="latest"),
        lambda: SerializationParameters(version=""),
        lambda: SerializationParameters(version="nan"),
        lambda: SerializationParameters(version="inf"),
        lambda: SerializationParameters(normalization_form="NFX"),
        lambda: SerializationParameters(normalization_form=1),  # type: ignore[arg-type]
        lambda: SerializationParameters(doctype_public="pub"),
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
    )

    codes = [_error_code(case) for case in cases]

    assert codes == ["SEPM0016"] * len(cases)


def test_invalid_sequences_raise_senr0001() -> None:
    first = _document(b"<one/>")
    second = _document(b"<two/>")

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
        lambda: _serialize((first.root_element, second.root_element), omit_xml_declaration=True),
        lambda: _serialize((XdmAtomic.string("top-level text"),), omit_xml_declaration=True),
        lambda: _serialize(
            (XdmAtomic.string("a"), XdmAtomic.integer(2)), omit_xml_declaration=True
        ),
        lambda: _serialize(
            (first.root_element, XdmAtomic.string("text")), omit_xml_declaration=True
        ),
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


def test_stylesheet_delivery_defers_unsupported_methods_to_task_32() -> None:
    source = stylesheet(
        '<xsl:output method="json" build-tree="no"/>'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:sequence select=\"map{'k': 1}\"/>"
        "</xsl:template>"
    )

    result = transform(source, delivery="stylesheet")

    assert result.principal_entry.serialized is None


def test_stylesheet_delivery_leaves_a_text_only_xml_result_unserialized() -> None:
    source = stylesheet('<xsl:template name="xsl:initial-template">just text</xsl:template>')

    result = transform(source, delivery="stylesheet")

    assert result.principal_entry.serialized is None


def test_secondary_serialization_errors_are_marked_secondary() -> None:
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<principal/>"
        '<xsl:result-document href="secondary.xml" doctype-public="pub">'
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
