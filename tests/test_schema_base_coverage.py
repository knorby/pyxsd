"""Coverage-rounding tests for ``pyxsd.schema_base``.

The schema-driven tests pin observable parse behavior (bound children,
report codes, raw containers). A handful of guarded lookups and defensive
branches cannot be reached through ``Schema.compile``/``parse`` -- a
descriptor whose ``expandedInstanceName`` raises, a namespace context
whose ``prefix_for`` raises, a class built before the effective attribute
wildcard is stamped, a datatype without ``_unvalidated`` -- so those use
minimal ``SchemaBase`` subclasses or ``SimpleNamespace`` stubs, mirroring
``tests/test_identity_internals.py`` and ``tests/test_misc_coverage.py``.
"""

import logging
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from typing import ClassVar

import pytest

import pyxsd.schema  # noqa: F401  (first import loads the ER stack in its safe order)
from pyxsd.binding import ParseModes
from pyxsd.content_model import Particle
from pyxsd.schema import Schema
from pyxsd.schema_base import (
    SchemaBase,
    _defined_declaration_names,
    _global_declaration,
    _is_substitution_head,
    _model_is_empty,
    _requires_unsatisfiable_content,
)
from pyxsd.validation import ValidationReport
from pyxsd.wildcards import NAMESPACE_ANY, WildcardSpec
from pyxsd.xsd_data_types import AnySimpleType

XSD_HEAD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
XSD_TAIL = "</xs:schema>"
XSI_ATTR = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


def full_parse(body, xml, tmp_path, mode=ParseModes.NAMESPACED):
    """Run the complete pipeline (schema build plus instance binding)."""
    schema_path = tmp_path / "schema.xsd"
    instance_path = tmp_path / "instance.xml"
    schema_path.write_text(XSD_HEAD + body + XSD_TAIL, encoding="utf-8")
    instance_path.write_text(xml, encoding="utf-8")
    return Schema.compile(str(schema_path), mode=mode).parse(str(instance_path))


def codes(doc):
    return [issue.code for issue in doc.report.issues]


def report_host():
    """A fresh ``SchemaBase`` subclass that records issues on its report."""

    class _Host(SchemaBase):
        hasWildcardAttributes_ = True
        schema = SimpleNamespace(report=ValidationReport())

    return _Host


def _component(kind_name, namespace, instance_name):
    """A fake component whose ``type(...).__name__`` fixes its XSD kind."""

    entry = type(kind_name, (), {})()
    entry.isGlobalDeclaration = lambda: True
    entry.getNamespace = lambda: namespace
    entry.instanceName = instance_name
    return entry


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def test_unsatisfiable_content_and_empty_model_handle_none():
    assert _requires_unsatisfiable_content(None) is False
    assert _model_is_empty(None) is False


def test_is_substitution_head_rejects_none():
    assert _is_substitution_head(None) is False


def test_global_declaration_filters_by_kind_and_namespace():
    # No component table at all.
    assert _global_declaration(None, "a", "element", None) is None

    attribute = _component("Attribute", "urn:x", lambda **_kw: "a")
    element = _component("Element", "urn:x", lambda **_kw: "a")
    # The attribute entry is skipped by kind; the element is returned.
    assert _global_declaration({"a": [attribute, element]}, "a", "element", "urn:x") is element
    # An entry declared in another namespace is skipped.
    assert _global_declaration({"a": [element]}, "a", "element", "urn:other") is None


def test_defined_declaration_names_skips_raising_and_nameless_entries():
    def _boom(**_kwargs):
        raise RuntimeError("boom")

    parser = SimpleNamespace(
        components={
            "a": [
                _component("Element", "urn:x", _boom),
                _component("Element", "urn:y", lambda **_kw: ""),
                _component("Element", "urn:z", lambda **_kw: "good"),
            ]
        }
    )
    assert _defined_declaration_names(parser, "element") == frozenset({"good"})
    # The collected names are cached on the parser.
    assert _defined_declaration_names(parser, "attribute") == frozenset()
    assert _defined_declaration_names(None, "element") == frozenset()


# ---------------------------------------------------------------------------
# SchemaBase: attribute fallbacks and descriptor tables
# ---------------------------------------------------------------------------


def test_getattr_rejects_dunder_probes():
    with pytest.raises(AttributeError) as excinfo:
        SchemaBase().__wibble__  # noqa: B018
    assert excinfo.value.args == ("__wibble__",)


def test_getattr_unmasks_a_broken_descriptor_lookup():
    class _Broken(SchemaBase):
        def _getElements(self):
            raise RuntimeError("broken descriptor table")

    with pytest.raises(AttributeError) as excinfo:
        _Broken().missing  # noqa: B018
    assert excinfo.value.args == ("missing",)
    # ``from None`` keeps the original failure out of the raised error.
    assert excinfo.value.__cause__ is None


def test_report_issue_logs_without_any_report(caplog):
    with caplog.at_level(logging.ERROR, logger="pyxsd"):
        SchemaBase._report_error("unattached problem", code="c")
    assert "unattached problem" in caplog.text
    record = next(r for r in caplog.records if "unattached problem" in r.getMessage())
    assert record.levelno == logging.ERROR


def test_qname_bindings_disabled_without_a_namespace_context():
    class _Strict(SchemaBase):
        _parseMode_ = ParseModes.NAMESPACED

    assert _Strict._qname_bindings(ET.fromstring("<r/>")) is None


def test_resolve_xsi_type_name_without_a_namespace_context():
    host = SimpleNamespace(mode=ParseModes.NAMESPACED, namespaceContext=None)
    assert SchemaBase._resolveXsiTypeName(ET.fromstring("<r/>"), "t:A", host) == "t:A"


def test_wildcard_attribute_specs_collects_over_the_mro():
    spec = WildcardSpec(namespace=NAMESPACE_ANY)

    class _Base(SchemaBase):
        wildcardAttributeSpecs_ = (spec,)

    class _Derived(_Base):
        pass

    assert SchemaBase._wildcard_attribute_specs(_Derived()) == [spec]
    assert SchemaBase._wildcard_attribute_specs(SchemaBase()) == []


def test_attribute_wildcard_fallback_without_an_effective_spec():
    class _Type(SchemaBase):
        _parseMode_ = ParseModes.NAMESPACED
        hasWildcardAttributes_ = True
        wildcardAttributeSpecs_ = ()

    instance = _Type()
    element = ET.fromstring('<r xmlns:x="urn:x" x:foo="1"/>')
    # No stamped effective wildcard and no collected specs: the undeclared
    # attribute is not bound and nothing is recorded.
    assert instance.getAttributesFromTag(element) == []
    assert instance._attribs_ == {}


def _stub_attribute(name, *, expander=None):
    class _Stub:
        _aliased_ = False

        def __init__(self):
            self.name = name
            if expander is not None:
                self.expandedInstanceName = expander

        def instanceName(self, *, is_attribute=False, parser=None):
            return name

        def __set__(self, instance, value):
            instance.__dict__[name] = value

    return _Stub()


def test_get_attributes_from_tag_without_an_expander():
    class _Type(SchemaBase):
        pass

    _Type._attributeNames_ = ["a"]
    _Type.a = _stub_attribute("a")
    instance = _Type()
    assert instance.getAttributesFromTag(ET.fromstring('<r a="v"/>')) == ["a"]
    assert instance.a == "v"
    assert instance._attribs_ == {"a": "v"}


def test_get_attributes_from_tag_survives_a_failing_expander():
    def _boom(*, is_attribute=False, parser=None):
        raise RuntimeError("no namespace for expansion")

    class _Type(SchemaBase):
        pass

    _Type._attributeNames_ = ["a"]
    _Type.a = _stub_attribute("a", expander=_boom)
    instance = _Type()
    assert instance.getAttributesFromTag(ET.fromstring('<r a="v"/>')) == ["a"]
    assert instance.a == "v"


def test_attribute_wildcard_checks_skip_and_invalid_values(caplog):
    assert (
        SchemaBase._checkWildcardAttribute("a", "v", WildcardSpec(process_contents="skip"), None)
        is True
    )

    class _BadValue:
        def __call__(self, value):
            raise ValueError("bad value")

    declaration = type("Attribute", (), {})()
    declaration.getType = lambda: _BadValue()
    declaration.isGlobalDeclaration = lambda: True
    parser = SimpleNamespace(components={"a": [declaration]})
    spec = WildcardSpec(namespace=NAMESPACE_ANY, process_contents="lax")
    with caplog.at_level(logging.ERROR, logger="pyxsd"):
        outcome = SchemaBase._checkWildcardAttribute("a", "v", spec, parser)
    assert outcome is False
    assert "has an invalid value" in caplog.text


# ---------------------------------------------------------------------------
# Instance tree construction
# ---------------------------------------------------------------------------


def test_make_instance_from_tag_without_a_parser():
    instance = SchemaBase.makeInstanceFromTag(ET.fromstring("<r>x</r>"))
    assert instance._name_ == "r"
    assert instance._value_ == ["x"]


def test_anytype_child_with_xsi_type_uses_the_declaration_or_falls_back(tmp_path):
    body = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:t" xmlns:t="urn:t" elementFormDefault="qualified">'
        '<xs:complexType name="A"><xs:sequence>'
        '<xs:element name="x" type="xs:string"/></xs:sequence></xs:complexType>'
        '<xs:element name="child" type="t:A"/>'
        '<xs:element name="root"><xs:complexType><xs:sequence>'
        '<xs:element name="a" type="xs:anyType"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(body, encoding="utf-8")
    schema = Schema.compile(str(schema_path), mode=ParseModes.NAMESPACED)

    declared = tmp_path / "declared.xml"
    declared.write_text(
        f'<t:root xmlns:t="urn:t" {XSI_ATTR}><t:a>'
        '<t:child xsi:type="t:A"><t:x>v</t:x></t:child></t:a></t:root>',
        encoding="utf-8",
    )
    doc = schema.parse(str(declared))
    assert doc.report.errors == []
    assert len(doc.root._children_) == 1
    assert len(doc.root._children_[0]._children_) == 1

    undeclared = tmp_path / "undeclared.xml"
    undeclared.write_text(
        f'<t:root xmlns:t="urn:t" {XSI_ATTR} '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema"><t:a>'
        '<t:undeclared xsi:type="xs:int">5</t:undeclared></t:a></t:root>',
        encoding="utf-8",
    )
    doc = schema.parse(str(undeclared))
    assert doc.report.errors == []


def test_simple_content_raw_default_keeps_the_forced_text(tmp_path):
    body = (
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="v" default="notint"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="xs:int"/></xs:simpleContent></xs:complexType></xs:element>'
        "</xs:sequence></xs:complexType></xs:element>"
    )
    doc = full_parse(body, "<r><v/></r>", tmp_path, mode=ParseModes.LAX)
    assert "value" in codes(doc)
    (child,) = doc.root._children_
    assert child._value_ == ["notint"]


def test_simple_content_invalid_value_leaves_no_value_to_check(tmp_path):
    body = (
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="v"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="xs:int"/></xs:simpleContent></xs:complexType></xs:element>'
        "</xs:sequence></xs:complexType></xs:element>"
    )
    doc = full_parse(body, "<r><v>notint</v></r>", tmp_path)
    assert "value" in codes(doc)
    (child,) = doc.root._children_
    assert child._value_ is None


def test_element_only_fixed_value_is_not_constrained(tmp_path):
    body = (
        '<xs:complexType name="T"><xs:sequence>'
        '<xs:element name="a" type="xs:string" minOccurs="0"/></xs:sequence></xs:complexType>'
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="v" type="T" fixed="zzz"/>'
        "</xs:sequence></xs:complexType></xs:element>"
    )
    doc = full_parse(body, "<r><v><a>x</a></v></r>", tmp_path)
    assert "fixed-element" not in codes(doc)


def test_invalid_fixed_primitive_value_is_reported(tmp_path):
    body = (
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="v" type="xs:int" fixed="abc"/>'
        "</xs:sequence></xs:complexType></xs:element>"
    )
    doc = full_parse(body, "<r><v>5</v></r>", tmp_path)
    assert "fixed-element" in codes(doc)


def test_child_alternative_selecting_xs_error_is_reported(tmp_path):
    body = (
        '<xs:complexType name="Base"><xs:sequence>'
        '<xs:element name="a" type="xs:string" minOccurs="0"/></xs:sequence></xs:complexType>'
        '<xs:element name="child" type="Base">'
        '<xs:alternative test="@kind = \'x\'" type="xs:error"/></xs:element>'
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element ref="child"/></xs:sequence></xs:complexType></xs:element>'
    )
    doc = full_parse(body, '<r><child kind="x"/></r>', tmp_path)
    assert "alternative-error" in codes(doc)


def test_primitive_element_attributes_skip_namespace_declarations():
    host = report_host()
    element = SimpleNamespace(attrib={"xmlns:x": "urn:x", "bad": "v"})
    host._checkPrimitiveElementAttributes(element, "el")
    assert [issue.code for issue in host.schema.report.errors] == ["unexpected-attribute"]
    assert "bad" in host.schema.report.errors[0].message


def test_nil_primitive_without_an_unvalidated_factory_is_reported():
    class _NoUnvalidated:
        pass

    host = report_host()
    assert host._nilPrimitive(_NoUnvalidated, ET.fromstring("<v/>"), "v") is None
    assert [issue.code for issue in host.schema.report.errors] == ["value"]
    assert "could not build a nil instance" in host.schema.report.errors[0].message


def test_declaration_type_falls_back_for_unusable_declarations():
    class _Raising:
        def getType(self):
            raise RuntimeError("no type")

    assert SchemaBase._declarationType(None) is None
    assert SchemaBase._declarationType(SimpleNamespace()) is None
    assert SchemaBase._declarationType(_Raising()) is None


def test_locally_declared_type_of_a_substitution_member():
    class _Declared:
        def getType(self):
            return AnySimpleType

    class _ModelHolder(SchemaBase):
        _contentModel_ = Particle(
            "sequence",
            1,
            1,
            [Particle("element", 1, 1, [], "head", descriptor=_Declared())],
        )

    instance = object.__new__(_ModelHolder)
    sub_element = ET.fromstring("<member/>")
    result = SchemaBase._locallyDeclaredElementType(
        instance, sub_element, descriptor=_Declared(), memberHeadMap={"member": "head"}
    )
    assert result is AnySimpleType


def _substitution_member(name=None, expanded=None, *, abstract=False, type_name=None):
    class _Member:
        tagAttributes = {"type": type_name} if type_name else {}

        def isAbstract(self):
            return abstract

        def instanceName(self, *, is_attribute=False, parser=None):
            return name

    member = _Member()
    member.name = name
    member.expandedName = expanded
    return member


class _SubstitutionHead:
    name = "h"
    expandedName = "h"
    isElementRef = True

    def __init__(self, declared_type=None):
        self._type = declared_type

    def getType(self):
        return self._type

    def getBlock(self):
        return None


def test_add_substitution_member_without_any_groups():
    assert SchemaBase._addSubstitutionMember(SchemaBase(), ET.fromstring("<x/>"), []) is False


def test_add_substitution_member_walks_the_closure_without_a_match():
    class _Stub(SchemaBase):
        _groups: ClassVar[dict] = {}

        @classmethod
        def _schemaSubstitutionGroups(cls, elemDescriptors):
            return cls._groups

    head = _SubstitutionHead()
    _Stub._groups = {
        "h": [
            _substitution_member("p", "p2"),
            _substitution_member(None, None),
        ]
    }
    assert _Stub._addSubstitutionMember(_Stub(), ET.fromstring("<x/>"), [head]) is False


def test_add_substitution_member_without_a_resolvable_type():
    class _Stub(SchemaBase):
        _groups: ClassVar[dict] = {}

        @classmethod
        def _schemaSubstitutionGroups(cls, elemDescriptors):
            return cls._groups

    _Stub._groups = {"h": [_substitution_member("x", "x2")]}
    head = _SubstitutionHead(declared_type=None)
    assert _Stub._addSubstitutionMember(_Stub(), ET.fromstring("<x/>"), [head]) is False


def test_schema_substitution_groups_survives_a_broken_schema_link():
    class _ExplodingSchema:
        def getSchema(self):
            raise RuntimeError("detached")

    assert SchemaBase._schemaSubstitutionGroups([_ExplodingSchema()]) == {}


def test_substitution_member_xsi_type_override_paths(tmp_path):
    body = (
        '<xs:complexType name="Base"><xs:sequence>'
        '<xs:element name="x" type="xs:string" minOccurs="0"/></xs:sequence></xs:complexType>'
        '<xs:complexType name="Derived"><xs:complexContent>'
        '<xs:extension base="Base"><xs:sequence>'
        '<xs:element name="y" type="xs:string" minOccurs="0"/>'
        "</xs:sequence></xs:extension></xs:complexContent></xs:complexType>"
        '<xs:complexType name="Other"><xs:sequence>'
        '<xs:element name="x" type="xs:string" minOccurs="0"/></xs:sequence></xs:complexType>'
        '<xs:element name="h" type="Base"/>'
        '<xs:element name="m" substitutionGroup="h" type="Derived"/>'
        '<xs:element name="root"><xs:complexType><xs:sequence>'
        '<xs:element ref="h"/></xs:sequence></xs:complexType></xs:element>'
    )
    valid = full_parse(body, f'<root {XSI_ATTR}><m xsi:type="Derived"/></root>', tmp_path)
    assert codes(valid) == []
    assert type(valid.root._children_[0]).__name__ == "Derived"

    invalid = full_parse(body, f'<root {XSI_ATTR}><m xsi:type="Other"/></root>', tmp_path)
    assert "xsi-type" in codes(invalid)

    unknown = full_parse(body, f'<root {XSI_ATTR}><m xsi:type="noSuch"/></root>', tmp_path)
    assert "xsi-type" in codes(unknown)


# ---------------------------------------------------------------------------
# Wildcard children
# ---------------------------------------------------------------------------


def test_abstract_wildcard_child_is_reported(tmp_path):
    body = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:t" xmlns:t="urn:t" elementFormDefault="qualified">'
        '<xs:element name="ab" abstract="true" type="xs:string"/>'
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:any namespace="##any" processContents="lax"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(body, encoding="utf-8")
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text('<t:r xmlns:t="urn:t"><t:ab>x</t:ab></t:r>', encoding="utf-8")
    doc = Schema.compile(str(schema_path), mode=ParseModes.NAMESPACED).parse(str(instance_path))
    assert "abstract-element" in codes(doc)
    assert doc.root._children_ == []


def test_strict_wildcard_binds_an_xsi_typed_undeclared_child(tmp_path):
    body = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:t" xmlns:t="urn:t" elementFormDefault="qualified">'
        '<xs:complexType name="A"><xs:sequence>'
        '<xs:element name="x" type="xs:string"/></xs:sequence></xs:complexType>'
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:any namespace="##any" processContents="strict"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(body, encoding="utf-8")
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(
        f'<t:r xmlns:t="urn:t" {XSI_ATTR}><t:undeclared xsi:type="t:A"><t:x/></t:undeclared></t:r>',
        encoding="utf-8",
    )
    doc = Schema.compile(str(schema_path), mode=ParseModes.NAMESPACED).parse(str(instance_path))
    assert doc.report.errors == []
    assert type(doc.root._children_[0]).__name__ == "A"


def test_strict_wildcard_reports_an_unresolvable_xsi_type(tmp_path):
    body = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:t" xmlns:t="urn:t" elementFormDefault="qualified">'
        '<xs:complexType name="A"><xs:sequence/></xs:complexType>'
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:any namespace="##any" processContents="strict"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(body, encoding="utf-8")
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(
        f'<t:r xmlns:t="urn:t" {XSI_ATTR}><t:undeclared xsi:type="t:noSuch"/></t:r>',
        encoding="utf-8",
    )
    doc = Schema.compile(str(schema_path), mode=ParseModes.NAMESPACED).parse(str(instance_path))
    assert "xsi-type" in codes(doc)
    assert "wildcard-no-declaration" in codes(doc)


def test_strict_wildcard_drops_an_invalid_primitive_xsi_type(tmp_path):
    body = (
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:any processContents="strict"/>'
        "</xs:sequence></xs:complexType></xs:element>"
    )
    doc = full_parse(
        body,
        f'<r xmlns:xs="http://www.w3.org/2001/XMLSchema" {XSI_ATTR}>'
        '<und xsi:type="xs:int">abc</und></r>',
        tmp_path,
    )
    assert "value" in codes(doc)
    assert doc.root._children_ == []


def test_declared_but_unresolvable_type_binds_through_a_lax_wildcard(tmp_path):
    body = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:t" xmlns:t="urn:t" elementFormDefault="qualified">'
        '<xs:element name="child" type="NoSuchType"/>'
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:any namespace="##any" processContents="lax"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(body, encoding="utf-8")
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text('<t:r xmlns:t="urn:t"><t:child/></t:r>', encoding="utf-8")
    doc = Schema.compile(str(schema_path), mode=ParseModes.NAMESPACED).parse(str(instance_path))
    # The unresolvable declaration fell through to the generic wildcard path.
    assert len(doc.root._children_) == 1
    assert type(doc.root._children_[0]) is SchemaBase


def test_model_less_wildcard_prefilter_declares_and_admits_children():
    class _Stub(SchemaBase):
        _parseMode_ = ParseModes.NAMESPACED
        hasWildcardElements_ = True
        wildcardElementSpecs_ = (WildcardSpec(namespace="urn:only"),)
        _targetNamespace_ = None

        @classmethod
        def _schemaSubstitutionGroups(cls, elemDescriptors):
            return {
                "e": [
                    _substitution_member(None, None),
                    _substitution_member("e", "e"),
                ]
            }

    instance = _Stub()
    element = ET.fromstring('<r xmlns:o="urn:only"><e/><o:y>t</o:y><z/></r>')
    assert _Stub.addElementsTo(instance, element) is instance
    # Only the wildcard-admitted child was bound (the others are
    # undeclared with strict binding).
    assert [child._name_ for child in instance._children_] == ["{urn:only}y"]


# ---------------------------------------------------------------------------
# Child accessors
# ---------------------------------------------------------------------------


class _NamespaceContext:
    def __init__(self, prefix="i", *, raises=False):
        self._prefix = prefix
        self._raises = raises

    def bindings_for(self, element):
        return {}

    def prefix_for(self, element, uri):
        if self._raises:
            raise RuntimeError("no prefix")
        return self._prefix


def _accessor_descriptor(name, namespace):
    class _Descriptor:
        _aliased_ = False

        def getNamespace(self):
            return namespace

        def isNillable(self):
            return False

        def getFixed(self):
            return None

        def getDefault(self):
            return None

        def getType(self):
            return AnySimpleType

    descriptor = _Descriptor()
    descriptor.name = name
    return descriptor


def _accessor_host(context):
    class _Host(SchemaBase):
        _parseMode_ = ParseModes.NAMESPACED
        schema = SimpleNamespace(_host=SimpleNamespace(namespaceContext=context))

    return _Host


def test_child_accessor_uses_an_in_scope_prefix_for_a_collision():
    host = _accessor_host(_NamespaceContext("i"))
    instance = host()
    element = ET.fromstring('<a xmlns:i="urn:i">1</a>')
    host._addChildInstance(instance, element, AnySimpleType, _accessor_descriptor("a", "urn:t"))
    host._addChildInstance(instance, element, AnySimpleType, _accessor_descriptor("a", "urn:i"))
    assert "a" in instance.__dict__
    assert isinstance(instance.__dict__["a_i"], AnySimpleType)


def test_child_accessor_falls_back_to_a_numeric_suffix():
    host = _accessor_host(_NamespaceContext(raises=True))
    instance = host()
    element = ET.fromstring('<a xmlns:i="urn:i">2</a>')
    host._addChildInstance(instance, element, AnySimpleType, _accessor_descriptor("a", "urn:t"))
    host._addChildInstance(instance, element, AnySimpleType, _accessor_descriptor("a", "urn:i"))
    assert isinstance(instance.__dict__["a_1"], AnySimpleType)


def test_child_accessor_without_a_namespace_context_uses_a_suffix():
    host = _accessor_host(None)
    instance = host()
    element = ET.fromstring('<a xmlns:i="urn:i">3</a>')
    host._addChildInstance(instance, element, AnySimpleType, _accessor_descriptor("a", "urn:t"))
    host._addChildInstance(instance, element, AnySimpleType, _accessor_descriptor("a", "urn:i"))
    assert isinstance(instance.__dict__["a_1"], AnySimpleType)


def test_child_accessor_disambiguates_a_real_strict_namespace_collision(tmp_path):
    """Two same-local-name global declarations, both admitted on one parent.

    The strict ``##any`` wildcard binds each child through its resolved
    global declaration, so the two ``foo`` declarations reach
    ``_childAccessor`` with equal ``name`` but different
    ``getNamespace()`` -- the real-pipeline shape the collision branch
    handles (rather than a hand-built namespace-context stub). The second
    declaration must not overwrite the first's plain accessor.
    """
    (tmp_path / "other.xsd").write_text(
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
        "targetNamespace='urn:other' xmlns:o='urn:other' "
        "elementFormDefault='qualified'>"
        "<xs:element name='foo' type='xs:string'/>"
        "</xs:schema>",
        encoding="utf-8",
    )
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
        "targetNamespace='urn:main' xmlns:m='urn:main' xmlns:o='urn:other' "
        "elementFormDefault='qualified'>"
        "<xs:import namespace='urn:other' schemaLocation='other.xsd'/>"
        "<xs:element name='foo' type='xs:string'/>"
        "<xs:element name='root'><xs:complexType><xs:sequence>"
        "<xs:any namespace='##any' processContents='strict' maxOccurs='unbounded'/>"
        "</xs:sequence></xs:complexType></xs:element>"
        "</xs:schema>",
        encoding="utf-8",
    )
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(
        '<m:root xmlns:m="urn:main" xmlns:o="urn:other">'
        "<m:foo>local</m:foo><o:foo>foreign</o:foo></m:root>",
        encoding="utf-8",
    )
    doc = Schema.compile(str(schema_path), mode=ParseModes.NAMESPACED).parse(str(instance_path))
    assert doc.report.errors == []
    assert [child._name_ for child in doc.root._children_] == [
        "{urn:main}foo",
        "{urn:other}foo",
    ]
    # The imported declaration is exposed under its in-scope instance prefix.
    assert doc.root.foo == "local"
    assert doc.root.foo_o == "foreign"
    assert doc.root.__dict__["_childAccessors_"] == {"foo": "urn:main", "foo_o": "urn:other"}


def test_inheritable_attributes_feed_a_conditional_type_test(tmp_path):
    body = (
        '<xs:complexType name="Doc"><xs:sequence>'
        '<xs:element ref="chap" maxOccurs="unbounded"/></xs:sequence>'
        '<xs:attribute name="kind" type="xs:string" inheritable="true"/>'
        '<xs:attribute name="note" type="xs:string"/>'
        "</xs:complexType>"
        '<xs:complexType name="ChapA"><xs:sequence>'
        '<xs:element name="a"/></xs:sequence></xs:complexType>'
        '<xs:complexType name="ChapB"><xs:sequence>'
        '<xs:element name="b"/></xs:sequence></xs:complexType>'
        '<xs:element name="doc" type="Doc"/>'
        '<xs:element name="chap">'
        '<xs:alternative test="@kind = \'a\'" type="ChapA"/>'
        '<xs:alternative test="@kind = \'b\'" type="ChapB"/>'
        "</xs:element>"
    )
    doc = full_parse(body, '<doc kind="a" note="n"><chap><a/></chap></doc>', tmp_path)
    assert doc.report.errors == []
    # The inherited kind selected ChapA, so a b child is rejected.
    doc = full_parse(body, '<doc kind="a" note="n"><chap><b/></chap></doc>', tmp_path)
    assert "order" in codes(doc)


def test_cta_context_node_without_ancestors_is_the_element():
    element = ET.fromstring("<r/>")
    assert SchemaBase._ctaContextNode(element) is element


# ---------------------------------------------------------------------------
# Values and order checkers
# ---------------------------------------------------------------------------


def test_add_value_to_honors_xml_space_and_line_splitting():
    preserve = SchemaBase()
    SchemaBase.addValueTo(preserve, ET.fromstring('<r xml:space="preserve"> x </r>'))
    assert preserve._value_ == [" x "]

    multiline = SchemaBase()
    SchemaBase.addValueTo(multiline, ET.fromstring("<r>a\nb</r>"))
    assert multiline._value_ == ["a", "b"]

    blank = SchemaBase()
    SchemaBase.addValueTo(blank, ET.fromstring("<r>   </r>"))
    assert blank._value_ is None


class Choice:
    def __init__(self, min_occurs=0, max_occurs=5):
        self._min = min_occurs
        self._max = max_occurs

    def getMinOccurs(self):
        return self._min

    def getMaxOccurs(self):
        return self._max


class _OrderDescriptor:
    def __init__(self, name, *, min_occurs=1, max_occurs=1, parent=None):
        self.name = name
        self.parent = parent
        self._min = min_occurs
        self._max = max_occurs

    def getMinOccurs(self):
        return self._min

    def getMaxOccurs(self):
        return self._max

    def instanceName(self, *, is_attribute=False, parser=None):
        return self.name


def test_choice_order_falls_back_and_warns_on_negative_occurrences():
    host = report_host()
    descriptor = _OrderDescriptor("a", min_occurs=-1, max_occurs=-1)
    element = ET.fromstring("<r><a/><a/></r>")
    assert host.checkElementOrderInChoice([descriptor], list(element), {}) is None
    issues = host.schema.report.issues
    assert [issue.code for issue in issues] == ["schema", "schema", "occurrence-max"]
    assert "minOccurs" in issues[0].message
    assert "maxOccurs" in issues[1].message
    assert "too many elements for the choice" in issues[2].message


def test_choice_order_checks_each_branch_max_occurs():
    host = report_host()
    descriptor = _OrderDescriptor("a", min_occurs=0, max_occurs=1, parent=Choice())
    element = ET.fromstring("<r><a/><a/></r>")
    assert host.checkElementOrderInChoice([descriptor], list(element), {}) is None
    issues = host.schema.report.issues
    assert [issue.code for issue in issues] == ["occurrence-max"]
    assert "within the choice" in issues[0].message


def test_attribute_wildcard_admits_short_circuits():
    class _Legacy(SchemaBase):
        hasWildcardAttributes_ = True

    assert _Legacy()._attributeWildcardAdmits("a") is False

    class _Strict(SchemaBase):
        _parseMode_ = ParseModes.NAMESPACED
        hasWildcardAttributes_ = True

    assert _Strict()._attributeWildcardAdmits("a") is False


def test_prohibited_attribute_with_an_any_attribute_is_reported(tmp_path):
    body = (
        '<xs:element name="r"><xs:complexType>'
        '<xs:attribute name="a" type="xs:string" use="prohibited"/>'
        "<xs:anyAttribute/></xs:complexType></xs:element>"
    )
    doc = full_parse(body, '<r a="x"/>', tmp_path, mode=ParseModes.STRICT)
    assert "prohibited-attribute" in codes(doc)


def test_undeclared_attributes_skip_builtins_and_admitted_namespaces():
    first = report_host()
    instance = first()
    instance._name_ = "r"
    element = ET.fromstring(f'<r {XSI_ATTR} xsi:nil="true" xml:lang="en" bad="1"/>')
    instance.checkAttributes([], element)
    issues = first.schema.report.issues
    assert [issue.code for issue in issues] == ["unexpected-attribute"]
    assert "bad" in issues[0].message

    second = report_host()
    raw = second()
    raw._name_ = "r"
    raw_element = SimpleNamespace(attrib={"xml:lang": "en", "xmlns:foo": "urn:x", "bad": "1"})
    raw.checkAttributes([], raw_element)
    raw_issues = second.schema.report.issues
    assert [issue.code for issue in raw_issues] == ["unexpected-attribute"]
    assert "bad" in raw_issues[0].message


def test_fixed_attribute_check_ignores_complex_typed_attributes():
    host = report_host()

    class _PlainAttributeType:
        pass

    descriptor = SimpleNamespace(
        name="a", getFixed=lambda: "x", getType=lambda: _PlainAttributeType
    )
    holder = host()
    assert holder._checkFixedAttribute(descriptor, holder, "r", ET.fromstring("<r/>")) is None
    assert host.schema.report.issues == []


def test_dump_cls_logs_the_class_body(caplog):
    with caplog.at_level(logging.DEBUG, logger="pyxsd.schema_base"):
        SchemaBase.dumpCls(SchemaBase)
    assert "In dumpCls" in caplog.text
