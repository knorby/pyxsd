"""Occurrence-level schema annotations on grounded XDM nodes.

The annotation layer keeps the Task 8 source evidence (unvalidated
``xsi:type``/``xsi:nil`` spellings) while adding the schema-aware PSVI:
governing declarations, actual types, validity/attempted state, nilled
state, typed values, defaults, and ID/IDREF classification.  Every claim
is keyed by the source occurrence, so repeated siblings and local
declarations never collide by name.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar

from pyxsd import xsd_data_types as xsd
from pyxsd.binding import ParseModes
from pyxsd.locations import IssueLocation, ParseProvenance, PathStep
from pyxsd.schema_context import SchemaContext, active_context
from pyxsd.validation import ValidationReport
from pyxsd.xdm import annotations as annotations_module
from pyxsd.xdm.annotations import (
    OccurrenceAnnotation,
    OccurrenceCapture,
    OccurrenceKey,
    capture_attribute_occurrence,
    capture_element_occurrence,
    capture_remaining_attributes,
    capture_schema_annotations,
    capturing_occurrences,
    occurrence_capture_active,
)
from pyxsd.xdm.nodes import XdmAnnotation, XdmDocument
from pyxsd.xdm.values import XdmAtomic

XSD_NS = "http://www.w3.org/2001/XMLSchema"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
SHOP = "urn:shop"
OTHER = "urn:other"

STRICT = ParseModes.LAX.replace(namespaces="strict")


def _compile(schema_source, **kwargs):
    from pyxsd.schema import Schema

    source = io.StringIO(schema_source) if isinstance(schema_source, str) else schema_source
    schema = Schema.compile(source, mode=kwargs.pop("mode", STRICT), **kwargs)
    assert not schema.report.has_errors, schema.report
    return schema


def _annotated(schema, xml_text: str, *, pool=None):
    capture = capture_schema_annotations(schema, io.BytesIO(xml_text.encode("utf-8")))
    document = XdmDocument.parse(xml_text.encode("utf-8"), schema_pool=pool)
    capture.apply(document)
    return document, capture


def _element_children(node):
    return tuple(child for child in node.children if child.kind == "element")


def _type_name(local: str, namespace: str = XSD_NS) -> str:
    return f"{{{namespace}}}{local}"


def test_local_anonymous_and_global_annotations():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{SHOP}"
            xmlns:s="{SHOP}" elementFormDefault="qualified">
          <xs:element name="catalog">
            <xs:complexType><xs:sequence>
              <xs:element name="item">
                <xs:complexType><xs:sequence>
                  <xs:element name="name" type="xs:string"/>
                </xs:sequence></xs:complexType>
              </xs:element>
              <xs:element name="code">
                <xs:complexType><xs:sequence>
                  <xs:element name="value" type="xs:int"/>
                </xs:sequence></xs:complexType>
              </xs:element>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    xml = (
        f'<s:catalog xmlns:s="{SHOP}">'
        "<s:item><s:name>a</s:name></s:item>"
        "<s:code><s:value>1</s:value></s:code>"
        "</s:catalog>"
    )
    document, capture = _annotated(schema, xml)

    root = document.root_element
    first, second = _element_children(root)
    assert first.annotation.governing_declaration.name == "item"
    assert first.annotation.local_declaration
    assert first.annotation.anonymous_type
    assert first.annotation.type_name is None
    assert first.annotation.actual_type_name is None
    assert first.annotation.actual_type is not second.annotation.actual_type
    assert first.annotation.validity == "valid"
    assert first.annotation.validation_attempted == "full"
    assert second.annotation.validity == "valid"
    assert second.annotation.governing_declaration.name == "code"
    assert second.annotation.type_name is None

    assert root.annotation.governing_declaration.name == "catalog"
    assert not root.annotation.local_declaration
    assert root.annotation.anonymous_type
    assert root.annotation.type_name is None
    assert root.annotation.actual_type is not None
    assert root.annotation.validity == "valid"
    assert root.annotation.validation_attempted == "full"

    first_path = first.source_location().path
    assert capture.annotation(OccurrenceKey(first_path)).governing_declaration.name == "item"
    assert capture.annotation(first_path, "missing") is None


def test_repeated_siblings_distinguish_xsi_type_by_occurrence():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{SHOP}"
            xmlns:s="{SHOP}" elementFormDefault="qualified">
          <xs:complexType name="Base">
            <xs:sequence><xs:element name="name" type="xs:string"/></xs:sequence>
          </xs:complexType>
          <xs:complexType name="Wide">
            <xs:complexContent><xs:extension base="s:Base">
              <xs:sequence><xs:element name="extra" type="xs:string" minOccurs="0"/></xs:sequence>
            </xs:extension></xs:complexContent>
          </xs:complexType>
          <xs:element name="catalog">
            <xs:complexType><xs:sequence>
              <xs:element name="item" type="s:Base" maxOccurs="unbounded"/>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    xml = (
        f'<s:catalog xmlns:s="{SHOP}" xmlns:xsi="{XSI_NS}">'
        "<s:item><s:name>a</s:name></s:item>"
        '<s:item xsi:type="s:Wide"><s:name>b</s:name><s:extra>c</s:extra></s:item>'
        "</s:catalog>"
    )
    document, _ = _annotated(schema, xml)
    first, second = _element_children(document.root_element)

    assert first.annotation.governing_declaration.name == "item"
    assert second.annotation.governing_declaration is first.annotation.governing_declaration
    assert first.annotation.actual_type is not second.annotation.actual_type
    assert first.annotation.type_name == _type_name("Base", SHOP)
    assert second.annotation.type_name == _type_name("Wide", SHOP)
    assert first.annotation.xsi_type is None
    assert second.annotation.xsi_type == "s:Wide"
    assert first.annotation.validity == "valid"
    assert second.annotation.validity == "valid"
    assert first.source_location().path != second.source_location().path


def test_list_union_and_id_typed_values():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{SHOP}"
            xmlns:s="{SHOP}" elementFormDefault="qualified">
          <xs:simpleType name="Sku">
            <xs:restriction base="xs:string">
              <xs:pattern value="[A-Z]{{2}}[0-9]+"/>
            </xs:restriction>
          </xs:simpleType>
          <xs:simpleType name="Tags"><xs:list itemType="xs:string"/></xs:simpleType>
          <xs:simpleType name="Code"><xs:union memberTypes="xs:int s:Sku"/></xs:simpleType>
          <xs:simpleType name="Identifier">
            <xs:restriction base="xs:ID"/>
          </xs:simpleType>
          <xs:element name="catalog">
            <xs:complexType><xs:sequence>
              <xs:element name="tags" type="s:Tags" minOccurs="0"/>
              <xs:element name="code" type="s:Code" maxOccurs="unbounded"/>
              <xs:element name="id" type="s:Identifier" minOccurs="0"/>
              <xs:element name="ref" type="xs:IDREF" minOccurs="0"/>
              <xs:element name="refs" type="xs:IDREFS" minOccurs="0"/>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    xml = (
        f'<s:catalog xmlns:s="{SHOP}">'
        "<s:tags>a b</s:tags><s:code>42</s:code><s:code>AB1</s:code>"
        "<s:id>ID1</s:id><s:ref>ID1</s:ref><s:refs>ID1 ID2</s:refs>"
        "</s:catalog>"
    )
    document, _ = _annotated(schema, xml)
    tags, first_code, second_code, identifier, ref, refs = _element_children(document.root_element)

    assert tags.annotation.type_name == _type_name("Tags", SHOP)
    assert [item.lexical for item in tags.annotation.typed_values] == ["a", "b"]
    assert all(item.type_name == _type_name("string") for item in tags.annotation.typed_values)
    assert tags.typed_value == tags.annotation.typed_values

    assert first_code.annotation.type_name == _type_name("Code", SHOP)
    assert first_code.annotation.typed_values == (XdmAtomic.integer(42),)
    assert [item.type_name for item in second_code.annotation.typed_values] == [
        _type_name("Sku", SHOP)
    ]
    assert identifier.annotation.id_kind == "ID"
    assert ref.annotation.id_kind == "IDREF"
    assert refs.annotation.id_kind == "IDREFS"
    assert refs.annotation.type_name == _type_name("IDREFS")


def test_defaults_nilled_and_invalid_nil():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:element name="root">
            <xs:complexType><xs:sequence>
              <xs:element name="nilled" type="xs:string" nillable="true"/>
              <xs:element name="badnil" type="xs:string"/>
              <xs:element name="defaulted" type="xs:string" default="dflt"/>
              <xs:element name="fixed" type="xs:string" fixed="F"/>
              <xs:element name="overridden" type="xs:string" default="d"/>
            </xs:sequence>
            <xs:attribute name="currency" default="USD"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    xml = (
        f'<root xmlns:xsi="{XSI_NS}" currency="EUR">'
        '<nilled xsi:nil="true"/><badnil xsi:nil="true"/><defaulted/>'
        "<fixed/><overridden>o</overridden>"
        "</root>"
    )
    document, _ = _annotated(schema, xml)
    nilled, badnil, defaulted, fixed, overridden = _element_children(document.root_element)

    assert nilled.annotation.nilled is True
    assert nilled.annotation.nil_declared
    assert nilled.annotation.nil_lexical == "true"
    assert nilled.annotation.typed_values == ()
    assert nilled.typed_value == ()
    assert nilled.annotation.validity == "valid"

    assert badnil.annotation.nilled is False
    assert badnil.annotation.nil_declared
    assert badnil.annotation.validity == "invalid"

    assert defaulted.annotation.type_name == _type_name("string")
    assert defaulted.annotation.default_value == "dflt"
    assert defaulted.annotation.value_source == "default"
    assert defaulted.annotation.typed_values == (XdmAtomic.string("dflt"),)

    assert fixed.annotation.fixed_value == "F"
    assert fixed.annotation.value_source == "fixed"
    assert fixed.annotation.typed_values == (XdmAtomic.string("F"),)

    assert overridden.annotation.default_value == "d"
    assert overridden.annotation.value_source == "instance"
    assert overridden.annotation.typed_values == (XdmAtomic.string("o"),)

    currency = document.root_element.attribute("currency")
    assert currency is not None
    assert currency.annotation.is_attribute
    assert currency.annotation.default_value == "USD"
    assert currency.annotation.value_source == "instance"
    assert currency.annotation.validity == "valid"
    assert [item.lexical for item in currency.annotation.typed_values] == ["EUR"]


def test_substitution_groups_and_lax_wildcards():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{SHOP}"
            xmlns:s="{SHOP}" elementFormDefault="qualified">
          <xs:simpleType name="Sku">
            <xs:restriction base="xs:string">
              <xs:pattern value="[A-Z]{{2}}[0-9]+"/>
            </xs:restriction>
          </xs:simpleType>
          <xs:element name="head" type="xs:string" abstract="true"/>
          <xs:element name="special" type="s:Sku" substitutionGroup="s:head"/>
          <xs:element name="root">
            <xs:complexType>
              <xs:sequence>
                <xs:element ref="s:head" minOccurs="0"/>
                <xs:any namespace="##other" processContents="lax"
                        minOccurs="0" maxOccurs="unbounded"/>
              </xs:sequence>
              <xs:anyAttribute namespace="##other" processContents="lax"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    xml = (
        f'<s:root xmlns:s="{SHOP}" xmlns:o="{OTHER}" o:extra="w">'
        "<s:special>AB1</s:special><o:loose>v</o:loose>"
        "</s:root>"
    )
    schema.parse(io.BytesIO(xml.encode("utf-8")))
    document, _ = _annotated(schema, xml)
    special, loose = _element_children(document.root_element)

    assert special.annotation.governing_declaration.name == "special"
    assert special.annotation.substitution_group_head == f"{{{SHOP}}}head"
    assert special.annotation.actual_type is not None
    assert special.annotation.validity == "valid"

    assert loose.annotation.governing_declaration is None
    assert loose.annotation.actual_type is None
    assert loose.annotation.wildcard.process_contents == "lax"
    assert loose.annotation.validation_attempted == "partial"
    assert loose.annotation.validity == "notKnown"

    extra = document.root_element.attribute(f"{{{OTHER}}}extra")
    assert extra is not None
    assert extra.annotation.governing_declaration is None
    assert extra.annotation.wildcard.process_contents == "lax"
    assert extra.annotation.validation_attempted == "partial"
    assert extra.annotation.validity == "notKnown"


def test_skip_and_strict_wildcard_states():
    skip_schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:element name="root">
            <xs:complexType><xs:sequence>
              <xs:any namespace="##other" processContents="skip"/>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    skip_xml = f'<root xmlns:o="{OTHER}"><o:any/></root>'
    skip_schema.parse(io.BytesIO(skip_xml.encode("utf-8")))
    skip_document, _ = _annotated(skip_schema, skip_xml)
    skipped = _element_children(skip_document.root_element)[0]
    assert skipped.annotation.governing_declaration is None
    assert skipped.annotation.wildcard.process_contents == "skip"
    assert skipped.annotation.validation_attempted == "none"
    assert skipped.annotation.validity == "notKnown"

    strict_schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:element name="root">
            <xs:complexType><xs:sequence>
              <xs:any namespace="##other" processContents="strict"/>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    missing_xml = f'<root xmlns:o="{OTHER}"><o:missing/></root>'
    strict_schema.parse(io.BytesIO(missing_xml.encode("utf-8")))
    missing_document, _ = _annotated(strict_schema, missing_xml)
    missing = _element_children(missing_document.root_element)[0]
    assert missing.annotation.governing_declaration is None
    assert missing.annotation.wildcard.process_contents == "strict"
    assert missing.annotation.validation_attempted == "none"
    assert missing.annotation.validity == "invalid"

    typed_xml = (
        f'<root xmlns:o="{OTHER}" xmlns:xsi="{XSI_NS}" '
        f'xmlns:xsd="{XSD_NS}"><o:dynamic xsi:type="xsd:string">x</o:dynamic></root>'
    )
    strict_schema.parse(io.BytesIO(typed_xml.encode("utf-8")))
    typed_document, _ = _annotated(strict_schema, typed_xml)
    dynamic = _element_children(typed_document.root_element)[0]
    assert dynamic.annotation.governing_declaration is None
    assert dynamic.annotation.wildcard.process_contents == "strict"
    assert dynamic.annotation.validation_attempted == "full"
    assert dynamic.annotation.validity == "valid"
    assert dynamic.annotation.xsi_type == "xsd:string"
    assert dynamic.annotation.typed_values == (XdmAtomic.string("x"),)


def test_strict_wildcard_matches_global_declaration(tmp_path: Path):
    other = tmp_path / "other.xsd"
    other.write_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{OTHER}"
            xmlns:o="{OTHER}" elementFormDefault="qualified">
          <xs:element name="declared" type="xs:string"/>
        </xs:schema>""",
        encoding="utf-8",
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:import namespace="{OTHER}" schemaLocation="other.xsd"/>
          <xs:element name="root">
            <xs:complexType><xs:sequence>
              <xs:any namespace="##other" processContents="strict"/>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>""",
        encoding="utf-8",
    )
    schema = _compile(main)
    document, _ = _annotated(schema, f'<root xmlns:o="{OTHER}"><o:declared>x</o:declared></root>')
    declared = _element_children(document.root_element)[0]
    assert declared.annotation.governing_declaration.name == "declared"
    assert declared.annotation.wildcard.process_contents == "strict"
    assert declared.annotation.validation_attempted == "full"
    assert declared.annotation.validity == "valid"
    assert declared.annotation.typed_values == (XdmAtomic.string("x"),)


def test_ordinary_parse_and_mismatched_capture_stay_untyped():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:element name="root" type="xs:string"/>
        </xs:schema>"""
    )
    xml = "<root>text</root>"

    with capturing_occurrences():
        assert occurrence_capture_active()
    assert not occurrence_capture_active()

    plain = schema.parse(io.BytesIO(xml.encode("utf-8")))
    assert not plain.report.has_errors
    untyped = XdmDocument.parse(xml.encode("utf-8"))
    assert isinstance(untyped.root_element.annotation, XdmAnnotation)
    assert untyped.root_element.annotation.validity == "not-validated"
    assert untyped.root_element.typed_value == (XdmAtomic.untyped_atomic("text"),)

    capture = capture_schema_annotations(schema, io.BytesIO(xml.encode("utf-8")))
    different_occurrence = XdmDocument.parse(b"<different>other</different>")
    capture.apply(different_occurrence)
    assert isinstance(different_occurrence.root_element.annotation, XdmAnnotation)
    assert different_occurrence.root_element.annotation.validity == "not-validated"

    annotated = XdmDocument.parse(xml.encode("utf-8"))
    capture.apply(annotated)
    assert isinstance(annotated.root_element.annotation, OccurrenceAnnotation)
    assert annotated.root_element.annotation.validity == "valid"


def test_capture_maps_keys_by_occurrence_and_ignores_namespace_nodes():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:element name="root">
            <xs:complexType><xs:sequence>
              <xs:element name="item" type="xs:int" maxOccurs="unbounded"/>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    xml = '<root xmlns:p="urn:p"><item>1</item><item>2</item></root>'
    document, capture = _annotated(schema, xml)
    first, second = _element_children(document.root_element)

    assert len({key for key in capture.annotations if key.attribute is None}) == 3
    assert capture.annotation(OccurrenceKey(first.source_location().path)).validity == "valid"
    assert capture.annotation(OccurrenceKey(second.source_location().path)).validity == "valid"
    assert first.annotation is not second.annotation

    namespace = document.root_element.namespace_node("p")
    assert namespace is not None
    assert isinstance(namespace.annotation, XdmAnnotation)
    assert not isinstance(namespace.annotation, OccurrenceAnnotation)


# ---------------------------------------------------------------------------
# Helper edge cases and the remaining PSVI shapes


def test_capture_helpers_are_inert_without_capture_or_provenance():
    element = ET.fromstring('<r a="1"/>')
    capture_element_occurrence(element, actual_type=str)
    capture_attribute_occurrence(element, "a")
    capture_remaining_attributes(element)

    with capturing_occurrences() as capture:
        capture_element_occurrence(element, actual_type=str)
        capture_attribute_occurrence(element, "a")
        capture_remaining_attributes(element)
        assert capture.annotations == {}

    root = ET.fromstring('<r a="1"><c/></r>')
    child = ET.Element("x", {"xmlns:p": "urn:p", "b": "2"})
    root.append(child)
    provenance = ParseProvenance(root, None)
    with active_context(SchemaContext(provenance=provenance)), capturing_occurrences() as capture:
        outside = ET.fromstring("<outside/>")
        capture_element_occurrence(outside, actual_type=str)
        capture_attribute_occurrence(outside, "a")
        capture_remaining_attributes(outside)
        assert capture.annotations == {}

        capture_element_occurrence(root, actual_type=str, bound="x")
        capture_attribute_occurrence(
            root, "a", actual_type=xsd.String, value="1", default_value="d", fixed_value="f"
        )
        capture_remaining_attributes(child)
        keys = set(capture.annotations)
        assert OccurrenceKey((PathStep("r", 1),)) in keys
        assert OccurrenceKey((PathStep("r", 1),), "a") in keys
        assert OccurrenceKey((PathStep("r", 1), PathStep("x", 1)), "b") in keys
        assert OccurrenceKey((PathStep("r", 1), PathStep("x", 1)), "xmlns:p") not in keys
        attribute = capture.annotation((PathStep("r", 1),), "a")
        assert attribute is not None
        assert attribute.default_value == "d"
        assert attribute.fixed_value == "f"


def test_finish_and_apply_edge_cases():
    path = (PathStep("r", 1),)
    report = ValidationReport()
    report.add_error("no address", code="unaddressed")
    report.add_error(
        "attribute error",
        code="invalid-attribute",
        location=IssueLocation(None, path, "a"),
    )
    report.add_error("element error", code="value", location=IssueLocation(None, path))

    capture = OccurrenceCapture()
    capture.record(
        OccurrenceKey(path),
        OccurrenceAnnotation(validation_attempted="full"),
    )
    capture.record(
        OccurrenceKey(path, "a"),
        OccurrenceAnnotation(validation_attempted="full", is_attribute=True),
    )
    capture.record(
        OccurrenceKey((PathStep("other", 1),)),
        OccurrenceAnnotation(validation_attempted="partial"),
    )
    capture.finish(report)
    assert capture.annotation(path).validity == "invalid"
    assert capture.annotation(path, "a").validity == "invalid"
    assert capture.annotation((PathStep("other", 1),)).validity == "notKnown"

    class NoRoot:
        root_element = None

    capture.apply(NoRoot())

    class FakeAttribute:
        def source_location(self):
            return None

    class FakeNode:
        kind = "element"
        descendants = ()
        attributes = (FakeAttribute(),)

        def source_location(self):
            return None

    class FakeDocument:
        root_element = FakeNode()

    capture.apply(FakeDocument())

    class TextNode:
        kind = "text"
        descendants = ()
        attributes = ()

        def source_location(self):
            return None

    class TextDocument:
        root_element = TextNode()

    capture.apply(TextDocument())


def test_declaration_and_value_helpers_handle_defensive_shapes():
    class NoMethods:
        pass

    class Raising:
        def getDefault(self):
            raise RuntimeError("broken")

        def isList(self):
            raise RuntimeError("broken")

        def isGlobalDeclaration(self):
            raise RuntimeError("broken")

    class NonString:
        def getDefault(self):
            return 42

    assert annotations_module._declaration_constraint(NoMethods(), "getDefault") is None
    assert annotations_module._declaration_constraint(Raising(), "getDefault") is None
    assert annotations_module._declaration_constraint(NonString(), "getDefault") is None
    assert not annotations_module._declaration_is_list(NoMethods())
    assert not annotations_module._declaration_is_list(Raising())
    assert not annotations_module._is_local_declaration(NoMethods())
    assert not annotations_module._is_local_declaration(Raising())

    assert annotations_module._value_source(None, True, None, "f") == "fixed"
    assert annotations_module._value_source(None, True, "d", None) == "default"
    assert annotations_module._value_source(None, True, "d", "f") == "fixed"
    assert annotations_module._value_source(None, False, "d", None) == "instance"
    assert annotations_module._value_source(None, False, None, None) is None

    class TextOnly:
        text = "value"

    assert annotations_module._string_value(TextOnly()) == "value"

    assert annotations_module._atomize(None) == ()
    atomic = XdmAtomic.string("x")
    assert annotations_module._atomize(atomic) == (atomic,)
    fallback = annotations_module._atomize(object())
    assert isinstance(fallback[0], XdmAtomic)
    assert fallback[0].type_name == f"{{{XSD_NS}}}untypedAtomic"

    class FakeUnion(xsd.XsdDataType):
        name = "FakeUnion"
        _unionMembers: ClassVar[list[type]] = [xsd.String]

    fake_union = FakeUnion()
    assert annotations_module._atomize(fake_union)[0].type_name == "FakeUnion"

    broken = type(
        "tag|simpleType",
        (xsd.Decimal,),
        {"__str__": lambda self: "not-a-decimal"},
    )("1")
    assert annotations_module._atomize(broken) == (XdmAtomic.untyped_atomic("not-a-decimal"),)

    class MidType(xsd.XsdDataType):
        pass

    no_base = type("tag|simpleType", (MidType,), {})()
    assert annotations_module._atomize(no_base) == (XdmAtomic.untyped_atomic(str(no_base)),)

    class BrokenContent:
        @classmethod
        def __new__(cls, value):
            raise ValueError("cannot rebuild")

    class SimpleContentType:
        _contentKind_ = "complex"
        _simpleContentType_ = BrokenContent
        _elementOnly_ = False

    element = ET.fromstring("<r>bad</r>")
    assert annotations_module._element_typed_values(
        None, element, False, SimpleContentType, "full"
    ) == (XdmAtomic.untyped_atomic("bad"),)

    assert annotations_module._attribute_typed_values(None, xsd.String) == ()

    with annotations_module._qname_context_for(object(), ET.fromstring("<r/>")):
        pass

    class BrokenBindings:
        def _qname_bindings(self, element):
            raise RuntimeError("no bindings")

    with annotations_module._qname_context_for(BrokenBindings(), ET.fromstring("<r/>")):
        pass

    bare_class = type("Bare", (), {})
    assert annotations_module.schema_type_name(bare_class) == "Bare"
    assert not annotations_module.is_anonymous_type(bare_class)
    assert annotations_module.schema_type_name(None) is None


def test_any_type_simple_content_mixed_and_invalid_values():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:element name="root">
            <xs:complexType mixed="true">
              <xs:sequence>
                <xs:element name="any" type="xs:anyType" minOccurs="0"/>
                <xs:element name="amount" minOccurs="0">
                  <xs:complexType><xs:simpleContent>
                    <xs:extension base="xs:decimal"/>
                  </xs:simpleContent></xs:complexType>
                </xs:element>
                <xs:element name="bad" type="xs:decimal" minOccurs="0"/>
              </xs:sequence>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    xml = "<root>lead<any><inner>x</inner></any><amount>12.5</amount><bad>oops</bad></root>"
    document, _ = _annotated(schema, xml)
    any_element, amount, bad = _element_children(document.root_element)

    assert document.root_element.annotation.typed_values == (
        XdmAtomic.untyped_atomic("leadx12.5oops"),
    )
    assert any_element.annotation.actual_type is xsd.AnyType
    assert any_element.annotation.typed_values == (XdmAtomic.untyped_atomic("x"),)
    assert amount.annotation.typed_values == (XdmAtomic.decimal("12.5"),)
    assert amount.annotation.validity == "valid"
    assert bad.annotation.typed_values[0].lexical == "oops"
    assert bad.annotation.validity == "invalid"


def test_dropped_invalid_value_uses_untyped_atomic():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:element name="root">
            <xs:complexType><xs:sequence>
              <xs:element name="bad" type="xs:decimal"/>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>""",
        mode=ParseModes.NAMESPACED,
    )
    document, _ = _annotated(schema, "<root><bad>oops</bad></root>")
    bad = _element_children(document.root_element)[0]
    assert bad.annotation.typed_values == (XdmAtomic.untyped_atomic("oops"),)
    assert bad.annotation.validity == "invalid"


def test_anonymous_simple_type_typed_value():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:element name="root">
            <xs:complexType><xs:sequence>
              <xs:element name="tag">
                <xs:simpleType><xs:restriction base="xs:string"/></xs:simpleType>
              </xs:element>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    document, _ = _annotated(schema, "<root><tag>v</tag></root>")
    tag = _element_children(document.root_element)[0]
    assert tag.annotation.anonymous_type
    assert tag.annotation.type_name is None
    assert tag.annotation.typed_values == (XdmAtomic.string("v"),)


def test_invalid_declared_attribute_keeps_lexical_typed_value():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:element name="root">
            <xs:complexType><xs:attribute name="count" type="xs:int"/></xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    document, _ = _annotated(schema, '<root count="oops"/>')
    attribute = document.root_element.attribute("count")
    assert attribute is not None
    assert attribute.annotation.validity == "invalid"
    assert attribute.annotation.typed_values == (XdmAtomic.untyped_atomic("oops"),)
    assert document.root_element.annotation.validity == "invalid"


def test_wildcard_attribute_declaration_skip_and_legacy_states(tmp_path: Path):
    other = tmp_path / "other.xsd"
    other.write_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{OTHER}"
            xmlns:o="{OTHER}">
          <xs:attribute name="tag" type="xs:int"/>
        </xs:schema>""",
        encoding="utf-8",
    )
    strict_main = tmp_path / "strict.xsd"
    strict_main.write_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:import namespace="{OTHER}" schemaLocation="other.xsd"/>
          <xs:element name="root">
            <xs:complexType>
              <xs:anyAttribute namespace="##other" processContents="strict"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>""",
        encoding="utf-8",
    )
    schema = _compile(strict_main)

    schema.parse(io.BytesIO(f'<root xmlns:o="{OTHER}" o:tag="5"/>'.encode()))
    document, _ = _annotated(schema, f'<root xmlns:o="{OTHER}" o:tag="5"/>')
    tag = document.root_element.attribute(f"{{{OTHER}}}tag")
    assert tag is not None
    assert tag.annotation.governing_declaration.name == "tag"
    assert tag.annotation.typed_values == (XdmAtomic.integer(5),)
    assert tag.annotation.validity == "valid"

    schema.parse(io.BytesIO(f'<root xmlns:o="{OTHER}" o:tag="bad"/>'.encode()))
    invalid_document, _ = _annotated(schema, f'<root xmlns:o="{OTHER}" o:tag="bad"/>')
    invalid_tag = invalid_document.root_element.attribute(f"{{{OTHER}}}tag")
    assert invalid_tag is not None
    assert invalid_tag.annotation.validity == "invalid"

    schema.parse(io.BytesIO(f'<root xmlns:o="{OTHER}" o:missing="x"/>'.encode()))
    missing_document, _ = _annotated(schema, f'<root xmlns:o="{OTHER}" o:missing="x"/>')
    missing = missing_document.root_element.attribute(f"{{{OTHER}}}missing")
    assert missing is not None
    assert missing.annotation.governing_declaration is None
    assert missing.annotation.validation_attempted == "none"
    assert missing.annotation.validity == "invalid"

    skip_schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
          <xs:element name="root">
            <xs:complexType>
              <xs:anyAttribute namespace="##other" processContents="skip"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    skip_schema.parse(io.BytesIO(f'<root xmlns:o="{OTHER}" o:tag="x"/>'.encode()))
    skip_document, _ = _annotated(skip_schema, f'<root xmlns:o="{OTHER}" o:tag="x"/>')
    skipped = skip_document.root_element.attribute(f"{{{OTHER}}}tag")
    assert skipped is not None
    assert skipped.annotation.wildcard.process_contents == "skip"
    assert skipped.annotation.validation_attempted == "none"
    assert skipped.annotation.validity == "notKnown"

    legacy_schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}">
          <xs:element name="root">
            <xs:complexType><xs:anyAttribute/></xs:complexType>
          </xs:element>
        </xs:schema>""",
        mode=ParseModes.LAX,
    )
    legacy_xml = f'<root xmlns:o="{OTHER}" o:tag="x"/>'
    legacy_schema.parse(io.BytesIO(legacy_xml.encode("utf-8")))
    legacy_document, _ = _annotated(legacy_schema, legacy_xml)
    legacy = legacy_document.root_element.attribute(f"{{{OTHER}}}tag")
    assert legacy is not None
    assert legacy.annotation.governing_declaration is None
    assert legacy.annotation.validation_attempted == "none"
    assert legacy.annotation.validity == "notKnown"

    rejected_schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{SHOP}"
            xmlns:s="{SHOP}" elementFormDefault="qualified">
          <xs:element name="root">
            <xs:complexType>
              <xs:anyAttribute namespace="##other" processContents="lax"/>
            </xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    rejected_xml = f'<s:root xmlns:s="{SHOP}" s:bad="x"/>'
    rejected_schema.parse(io.BytesIO(rejected_xml.encode("utf-8")))
    rejected_document, _ = _annotated(rejected_schema, rejected_xml)
    rejected = rejected_document.root_element.attribute(f"{{{SHOP}}}bad")
    assert rejected is not None
    assert rejected.annotation.governing_declaration is None
    assert rejected.annotation.validation_attempted == "none"
    assert rejected.annotation.validity == "invalid"


def test_nilled_complex_and_any_type_xsi_type_children():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{SHOP}"
            xmlns:s="{SHOP}" elementFormDefault="qualified">
          <xs:complexType name="Dyn">
            <xs:sequence><xs:element name="v" type="xs:string"/></xs:sequence>
          </xs:complexType>
          <xs:element name="root">
            <xs:complexType><xs:sequence>
              <xs:element name="nilledComplex" nillable="true">
                <xs:complexType><xs:sequence>
                  <xs:element name="v" type="xs:string"/>
                </xs:sequence></xs:complexType>
              </xs:element>
              <xs:element name="any" type="xs:anyType"/>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>"""
    )
    xml = (
        f'<s:root xmlns:s="{SHOP}" xmlns:xsi="{XSI_NS}">'
        '<s:nilledComplex xsi:nil="true"/>'
        '<s:any><s:dynamic xsi:type="s:Dyn"><s:v>x</s:v></s:dynamic></s:any>'
        "</s:root>"
    )
    schema.parse(io.BytesIO(xml.encode("utf-8")))
    document, _ = _annotated(schema, xml)
    nilled, any_element = _element_children(document.root_element)
    dynamic = _element_children(any_element)[0]

    assert nilled.annotation.nilled is True
    assert nilled.annotation.typed_values == ()
    assert nilled.annotation.validity == "valid"

    assert dynamic.annotation.governing_declaration is None
    assert dynamic.annotation.actual_type is not None
    assert dynamic.annotation.type_name == f"{{{SHOP}}}Dyn"
    assert dynamic.annotation.validation_attempted == "full"
    assert dynamic.annotation.typed_values == ()
    assert dynamic.annotation.validity == "valid"


def test_legacy_generic_wildcard_and_ordinary_parse():
    schema = _compile(
        f"""<xs:schema xmlns:xs="{XSD_NS}">
          <xs:element name="root">
            <xs:complexType><xs:sequence>
              <xs:any namespace="##any" processContents="lax" minOccurs="0"/>
            </xs:sequence></xs:complexType>
          </xs:element>
        </xs:schema>""",
        mode=ParseModes.LAX,
    )
    xml = "<root><anything><nested/></anything></root>"
    document, _ = _annotated(schema, xml)
    anything = _element_children(document.root_element)[0]
    nested = _element_children(anything)[0]

    assert anything.annotation.governing_declaration is None
    assert anything.annotation.wildcard is None
    assert anything.annotation.validation_attempted == "none"
    assert anything.annotation.validity == "notKnown"
    assert nested.annotation.validation_attempted == "none"

    ordinary = schema.parse(io.BytesIO(xml.encode("utf-8")))
    assert ordinary.root is not None
