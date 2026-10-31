"""Coverage tests for type resolution, element occurrence/legality and
complex-type content.

Schema-driven tests pin the observable error codes and generated-class
shapes; small stand-ins cover the detached/detached-schema fallbacks a
real compile never reaches.
"""

import xml.etree.ElementTree as ET
from types import SimpleNamespace
from typing import ClassVar

import pytest

import pyxsd.schema  # noqa: F401
from pyxsd.binding import ParseModes
from pyxsd.element_representatives import xsd_type as xsd_type_module
from pyxsd.element_representatives.element import Element, _xsd_derived
from pyxsd.element_representatives.element_representative import (
    ComponentTable,
    ElementRepresentative,
)
from pyxsd.element_representatives.xsd_type import XsdType
from pyxsd.schema import Schema
from pyxsd.schema_context import SchemaContext, active_context
from pyxsd.validation import ValidationReport
from pyxsd.wildcards import WildcardSpec
from pyxsd.xsd_data_types import AnySimpleType, XsdDataType

XSD_HEAD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
XSD_TAIL = "</xs:schema>"


def schema_tree(body):
    root = ET.fromstring(XSD_HEAD + body + XSD_TAIL)
    return ElementRepresentative.factory(root, None)


def find_all(er, class_name, found=None):
    if found is None:
        found = []
    if type(er).__name__ == class_name:
        found.append(er)
    for child in getattr(er, "processedChildren", None) or []:
        if child is not None:
            find_all(child, class_name, found)
    return found


def compile_schema(body, tmp_path, mode=ParseModes.STRICT):
    path = tmp_path / "schema.xsd"
    path.write_text(XSD_HEAD + body + XSD_TAIL, encoding="utf-8")
    return Schema.compile(str(path), mode=mode)


def compile_raw(text, tmp_path, mode=ParseModes.STRICT):
    path = tmp_path / "schema.xsd"
    path.write_text(text, encoding="utf-8")
    return Schema.compile(str(path), mode=mode)


def entries_of(compiled, class_name):
    return [
        entry
        for entries in compiled.components.values()
        for entry in entries
        if type(entry).__name__ == class_name
    ]


def error_codes(report):
    return {issue.code for issue in report.errors}


# ---------------------------------------------------------------------------
# XsdType base helpers
# ---------------------------------------------------------------------------


def test_get_containing_type_name_repairs_a_missing_name():
    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence><xs:element name='a'>"
        "<xs:simpleType><xs:restriction base='xs:string'/></xs:simpleType>"
        "</xs:element></xs:sequence></xs:complexType>"
    )
    inline = find_all(tree, "SimpleType")[0]
    inline.name = None
    assert inline.getContainingTypeName() is not None
    assert inline.name is not None


def test_get_name_falls_back_to_the_registry_without_a_table():
    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence><xs:element name='a'>"
        "<xs:simpleType><xs:restriction base='xs:string'/></xs:simpleType>"
        "</xs:element></xs:sequence></xs:complexType>"
    )
    inline = find_all(tree, "SimpleType")[0]
    inline.name = None
    tree.components = None
    generated = inline.getName()
    assert generated is not None and "|" in generated


def test_primary_base_returns_none_when_all_bases_are_mixins():
    class Mixin:
        pass

    stub = SimpleNamespace(_mixinBases=lambda host: [Mixin])
    assert XsdType._primaryBase(stub, None, [Mixin]) is None


def test_list_item_class_inline_name_and_unresolved():
    tree = schema_tree(
        "<xs:simpleType name='s'><xs:list itemType='parent|SimpleType'/></xs:simpleType>"
    )
    simple = find_all(tree, "SimpleType")[0]
    resolved = simple._listItemClass(SimpleNamespace(components=ComponentTable()))
    assert resolved is AnySimpleType


def test_check_final_ignores_a_missing_base():
    tree = schema_tree("<xs:complexType name='t'><xs:sequence/></xs:complexType>")
    complex_type = find_all(tree, "ComplexType")[0]
    assert complex_type._checkFinal(None, "Base") is None


def test_check_final_reads_the_schema_final_default(tmp_path):
    compiled = compile_raw(
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' finalDefault='restriction'>"
        "<xs:complexType name='Base'><xs:sequence/></xs:complexType>"
        "<xs:complexType name='Derived'><xs:complexContent>"
        "<xs:extension base='Base'><xs:sequence/></xs:extension>"
        "</xs:complexContent></xs:complexType></xs:schema>",
        tmp_path,
    )
    # The final default forbids restrictions, not the extension used here.
    assert "final" not in error_codes(compiled.report)


def test_check_final_reports_a_blocked_derivation(tmp_path):
    compiled = compile_schema(
        "<xs:complexType name='Base' final='extension'><xs:sequence/></xs:complexType>"
        "<xs:complexType name='Derived'><xs:complexContent>"
        "<xs:extension base='Base'><xs:sequence/></xs:extension>"
        "</xs:complexContent></xs:complexType>",
        tmp_path,
    )
    assert "final" in error_codes(compiled.report)


def test_attribute_collides_returns_false_without_a_key(tmp_path):
    compiled = compile_schema("<xs:complexType name='t'><xs:sequence/></xs:complexType>", tmp_path)
    complex_type = entries_of(compiled, "ComplexType")[0]
    candidate = SimpleNamespace(
        expandedInstanceName=lambda is_attribute=True, parser=None: None,
        name=None,
    )
    assert complex_type._attributeCollides([], candidate, None) is False


def test_resolve_attribute_refs_dedupes_by_expanded_name(tmp_path):
    compiled = compile_schema(
        "<xs:attribute name='g' type='xs:string'/>"
        "<xs:complexType name='t'>"
        "<xs:attribute name='g' type='xs:string'/>"
        "<xs:attribute ref='g'/>"
        "</xs:complexType>",
        tmp_path,
    )
    assert "duplicate-attribute" in error_codes(compiled.report)


def test_resolve_attribute_refs_suffixes_a_colliding_local_name(tmp_path):
    compiled = compile_raw(
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' xmlns:t='urn:t' "
        "targetNamespace='urn:t' elementFormDefault='qualified' "
        "attributeFormDefault='unqualified'>"
        "<xs:attribute name='a' type='xs:string'/>"
        "<xs:complexType name='t'>"
        "<xs:attribute ref='t:a'/>"
        "<xs:attribute name='a' type='xs:string'/>"
        "</xs:complexType></xs:schema>",
        tmp_path,
        mode=ParseModes.NAMESPACED,
    )
    type_er = next(entry for entry in entries_of(compiled, "ComplexType") if entry.name == "t")
    # The unqualified local use and the namespaced reference share a local
    # name but not an expanded name; the later one is re-keyed with a suffix.
    assert "a|2" in type_er.attributes


def test_global_attribute_candidates_without_a_table():
    schema = SimpleNamespace(
        attributes={"a": "attr-a"},
        components=None,
        processedChildren=[],
        host=None,
    )
    stub = SimpleNamespace(getSchema=lambda: schema)
    candidates = XsdType._globalAttributeCandidates(
        stub, SimpleNamespace(mode=ParseModes.NAMESPACED)
    )
    assert list(candidates) == ["attr-a"]


def test_declared_notations_without_a_component_table():
    stub = SimpleNamespace(getSchema=lambda: SimpleNamespace(components=None))
    assert XsdType._declaredNotations(stub) == set()


def test_effective_attribute_wildcard_skips_non_type_bases():
    tree = schema_tree("<xs:complexType name='t'><xs:sequence/></xs:complexType>")
    complex_type = find_all(tree, "ComplexType")[0]
    assert complex_type._effectiveAttributeWildcard(["not-a-type"]) is None


def test_effective_attribute_wildcard_falls_back_to_stamped_specs():
    class Base:
        wildcardAttributeSpecs_: ClassVar[list] = [
            WildcardSpec(namespace="##any", process_contents="lax", is_attribute=True)
        ]
        _targetNamespace_ = None

    tree = schema_tree("<xs:complexType name='t'><xs:sequence/></xs:complexType>")
    complex_type = find_all(tree, "ComplexType")[0]
    result = complex_type._effectiveAttributeWildcard([Base])
    assert result is not None


def test_union_class_equality_and_repr(tmp_path):
    compiled = compile_schema(
        "<xs:simpleType name='u'><xs:union memberTypes='xs:int xs:string'/></xs:simpleType>",
        tmp_path,
    )
    union_class = compiled.classes["u"]
    first = union_class("42")
    second = union_class("42")
    assert first == second
    assert first == 42
    assert repr(first) == repr(first.memberValue)
    assert str(first) == str(first.memberValue)


def test_union_class_with_an_unresolvable_member(tmp_path):
    compiled = compile_schema(
        "<xs:simpleType name='u'><xs:union memberTypes='xs:int noSuchType'/></xs:simpleType>",
        tmp_path,
    )
    assert "unknown-type" in error_codes(compiled.report)
    union_class = compiled.classes["u"]
    assert str(union_class("7")) == "7"


def test_resolve_attribute_group_refs_without_a_host(tmp_path):
    compiled = compile_schema(
        "<xs:attributeGroup name='g'><xs:attribute name='x' type='xs:string'/></xs:attributeGroup>"
        "<xs:complexType name='t'><xs:attributeGroup ref='g'/></xs:complexType>",
        tmp_path,
    )
    complex_type = entries_of(compiled, "ComplexType")[0]
    # A reference to a group that does not exist is reported.
    ghost = SimpleNamespace(
        ref="ghost",
        _resolve_group=lambda *, parser=None, for_class_building=False: None,
    )
    complex_type.attributeGroupRefs = [ghost]
    complex_type.resolveAttributeGroupRefs(compiled._host)
    assert "unknown-attributeGroup" in error_codes(compiled.report)


def test_attribute_group_ref_registers_element_wildcards(tmp_path):
    compiled = compile_schema(
        "<xs:attributeGroup name='g'><xs:attribute name='x' type='xs:string'/></xs:attributeGroup>"
        "<xs:complexType name='t'><xs:attributeGroup ref='g'/></xs:complexType>",
        tmp_path,
    )
    group_er = next(
        entry
        for entry in entries_of(compiled, "AttributeGroup")
        if not getattr(entry, "isRefSite", False)
    )
    type_er = entries_of(compiled, "ComplexType")[0]
    element_spec = WildcardSpec(
        namespace="##other", process_contents="strict", is_attribute=False, target_namespace=None
    )
    group_er.wildcardElementSpecs = [element_spec]
    type_er.wildcardElementSpecs = []
    type_er.resolveAttributeGroupRefs(compiled._host)
    assert element_spec in type_er.wildcardElementSpecs


def test_default_attribute_group_registers_element_wildcards(tmp_path):
    compiled = compile_schema(
        "<xs:attributeGroup name='g'><xs:attribute name='x' type='xs:string'/></xs:attributeGroup>"
        "<xs:complexType name='t'><xs:sequence/></xs:complexType>",
        tmp_path,
    )
    group_er = next(
        entry
        for entry in entries_of(compiled, "AttributeGroup")
        if not getattr(entry, "isRefSite", False)
    )
    type_er = entries_of(compiled, "ComplexType")[0]
    element_spec = WildcardSpec(
        namespace="##other", process_contents="strict", is_attribute=False, target_namespace=None
    )
    group_er.wildcardElementSpecs = [element_spec]
    type_er.defaultAttributeGroup = group_er
    type_er.wildcardElementSpecs = []
    type_er._applyDefaultAttributeGroup(compiled._host)
    assert element_spec in type_er.wildcardElementSpecs


def test_default_attribute_group_registers_attribute_wildcards(tmp_path):
    compiled = compile_raw(
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' defaultAttributes='g'>"
        "<xs:attributeGroup name='g'><xs:anyAttribute namespace='##other'/></xs:attributeGroup>"
        "<xs:complexType name='t'><xs:sequence/></xs:complexType></xs:schema>",
        tmp_path,
    )
    type_er = entries_of(compiled, "ComplexType")[0]
    assert getattr(type_er, "wildcardAttributeSpecs", [])
    assert compiled.classes["t"].hasWildcardAttributes_ is True


def test_collect_attribute_group_nested_wildcards_and_missing(tmp_path):
    compiled = compile_schema(
        "<xs:attributeGroup name='inner'>"
        "<xs:attribute name='x' type='xs:string'/></xs:attributeGroup>"
        "<xs:attributeGroup name='outer'>"
        "<xs:attributeGroup ref='inner'/>"
        "<xs:attributeGroup ref='ghost'/>"
        "</xs:attributeGroup>"
        "<xs:complexType name='t'><xs:attributeGroup ref='outer'/></xs:complexType>",
        tmp_path,
    )
    # The nested reference resolves; the ghost is reported.
    assert "unknown-attributeGroup" in error_codes(compiled.report)


def test_collect_attribute_group_registers_element_wildcards(tmp_path):
    compiled = compile_schema(
        "<xs:attributeGroup name='g'>"
        "<xs:attribute name='x' type='xs:string'/></xs:attributeGroup>"
        "<xs:complexType name='t'><xs:attributeGroup ref='g'/></xs:complexType>",
        tmp_path,
    )
    group_er = next(
        entry
        for entry in entries_of(compiled, "AttributeGroup")
        if not getattr(entry, "isRefSite", False)
    )
    type_er = entries_of(compiled, "ComplexType")[0]
    element_spec = WildcardSpec(
        namespace="##other", process_contents="strict", is_attribute=False, target_namespace=None
    )
    group_er.wildcardElementSpecs = [element_spec]
    type_er.wildcardElementSpecs = []
    type_er._collectAttributeGroup(group_er, frozenset(), compiled._host)
    assert element_spec in type_er.wildcardElementSpecs


def test_collect_attribute_group_registers_nested_element_wildcards(tmp_path):
    compiled = compile_schema(
        "<xs:attributeGroup name='inner'>"
        "<xs:attribute name='x' type='xs:string'/></xs:attributeGroup>"
        "<xs:attributeGroup name='outer'><xs:attributeGroup ref='inner'/></xs:attributeGroup>"
        "<xs:complexType name='t'><xs:attributeGroup ref='outer'/></xs:complexType>",
        tmp_path,
    )
    groups = [
        entry
        for entry in entries_of(compiled, "AttributeGroup")
        if not getattr(entry, "isRefSite", False)
    ]
    inner = next(entry for entry in groups if entry.name == "inner")
    outer = next(entry for entry in groups if entry.name == "outer")
    type_er = entries_of(compiled, "ComplexType")[0]
    element_spec = WildcardSpec(
        namespace="##other", process_contents="strict", is_attribute=False, target_namespace=None
    )
    inner.wildcardElementSpecs = [element_spec]
    type_er.wildcardElementSpecs = []
    type_er._collectAttributeGroup(outer, frozenset(), compiled._host)
    assert element_spec in type_er.wildcardElementSpecs


def test_apply_default_attribute_group_ignores_the_absent_group():
    tree = schema_tree("<xs:complexType name='t'><xs:sequence/></xs:complexType>")
    complex_type = find_all(tree, "ComplexType")[0]
    assert complex_type._applyDefaultAttributeGroup(None) is None


def test_declared_member_class_uses_the_declared_document(tmp_path):
    compiled = compile_schema(
        "<xs:simpleType name='member'><xs:restriction base='xs:string'/></xs:simpleType>"
        "<xs:simpleType name='u'><xs:union memberTypes='member xs:int'/></xs:simpleType>",
        tmp_path,
    )
    union_class = compiled.classes["u"]
    assert str(union_class("hello")) == "hello"


# ---------------------------------------------------------------------------
# element.py
# ---------------------------------------------------------------------------


def test_xsd_derived_rejects_a_missing_class():
    assert _xsd_derived(None, int) is False


def test_element_str_names_its_container_and_type():
    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence><xs:element name='a'/></xs:sequence></xs:complexType>"
    )
    element = find_all(tree, "Element")[0]
    assert str(element).endswith("|Element|a")


def test_element_delete_removes_the_stored_value():
    tree = schema_tree("<xs:element name='a' type='xs:string'/>")
    element = find_all(tree, "Element")[0]
    holder = SimpleNamespace()
    holder.__dict__["a"] = "value"
    Element.__delete__(element, holder)
    assert "a" not in holder.__dict__


def parse_instance(compiled, xml, tmp_path):
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(xml, encoding="utf-8")
    return compiled.parse(str(instance_path))


def test_element_set_reports_an_invalid_value_and_appends_to_a_list(tmp_path):
    compiled = compile_schema(
        "<xs:element name='r'><xs:complexType><xs:sequence>"
        "<xs:element name='a' type='xs:int' maxOccurs='unbounded'/>"
        "</xs:sequence></xs:complexType></xs:element>",
        tmp_path,
    )
    doc = parse_instance(compiled, "<r/>", tmp_path)
    holder = doc.root
    element = type(holder).a
    report = ValidationReport()
    with active_context(SchemaContext(report=report)):
        Element.__set__(element, holder, "not-an-int")
    assert "invalid-element" in error_codes(report)
    assert holder.__dict__["a"] == ["not-an-int"]


def test_element_set_writes_through_to_the_child_node(tmp_path):
    compiled = compile_schema(
        "<xs:element name='r'><xs:complexType><xs:sequence>"
        "<xs:element name='a' type='xs:int'/></xs:sequence></xs:complexType></xs:element>",
        tmp_path,
    )
    doc = parse_instance(compiled, "<r><a>7</a></r>", tmp_path)
    holder = doc.root
    child = next(c for c in holder._children_ if c._name_ == "a")
    element = type(holder).a
    declared = element.getType()
    new_value = declared("5")
    Element.__set__(element, holder, new_value)
    assert child._value_ == new_value.lexical()


def test_element_unknown_attribute_is_reported(tmp_path):
    compiled = compile_schema("<xs:element name='r' bogus='1'/>", tmp_path)
    assert any(
        "unrecognised attribute 'bogus'" in issue.message for issue in compiled.report.errors
    )


def test_global_element_ref_is_reported(tmp_path):
    compiled = compile_schema("<xs:element name='r' ref='h'/>", tmp_path)
    assert any(
        "must not carry a ref attribute" in issue.message for issue in compiled.report.errors
    )


def test_element_ref_with_a_name_is_reported(tmp_path):
    compiled = compile_schema(
        "<xs:element name='g' type='xs:string'/>"
        "<xs:complexType name='t'><xs:sequence><xs:element ref='g' name='x'/></xs:sequence>"
        "</xs:complexType>",
        tmp_path,
    )
    assert any("must not also declare a name" in issue.message for issue in compiled.report.errors)


def test_element_ref_with_a_forbidden_attribute_is_reported(tmp_path):
    compiled = compile_schema(
        "<xs:element name='g' type='xs:string'/>"
        "<xs:complexType name='t'><xs:sequence><xs:element ref='g' type='xs:string'/>"
        "</xs:sequence></xs:complexType>",
        tmp_path,
    )
    assert any("must not also carry 'type'" in issue.message for issue in compiled.report.errors)


def test_element_ref_with_an_inline_type_is_reported(tmp_path):
    compiled = compile_schema(
        "<xs:element name='g' type='xs:string'/>"
        "<xs:complexType name='t'><xs:sequence><xs:element ref='g'>"
        "<xs:simpleType><xs:restriction base='xs:string'/></xs:simpleType>"
        "</xs:element></xs:sequence></xs:complexType>",
        tmp_path,
    )
    assert any(
        "must not also declare an inline type" in issue.message for issue in compiled.report.errors
    )


# ---------------------------------------------------------------------------
# complex_type.py
# ---------------------------------------------------------------------------


def test_explicit_content_empty_honors_max_occurs_zero():
    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence maxOccurs='0'>"
        "<xs:element name='a'/></xs:sequence></xs:complexType>"
    )
    complex_type = find_all(tree, "ComplexType")[0]
    assert complex_type._explicitContentEmpty() is True


def test_explicit_content_empty_with_an_empty_choice():
    tree = schema_tree("<xs:complexType name='t'><xs:choice/></xs:complexType>")
    complex_type = find_all(tree, "ComplexType")[0]
    assert complex_type._explicitContentEmpty() is False


def test_explicit_content_is_empty_sees_simple_content():
    tree = schema_tree(
        "<xs:complexType name='t'><xs:simpleContent>"
        "<xs:extension base='xs:string'/></xs:simpleContent></xs:complexType>"
    )
    complex_type = find_all(tree, "ComplexType")[0]
    assert complex_type._explicitContentIsEmpty() is False


def test_explicit_content_is_empty_without_simple_content():
    tree = schema_tree("<xs:complexType name='t'><xs:sequence/></xs:complexType>")
    complex_type = find_all(tree, "ComplexType")[0]
    assert complex_type._explicitContentIsEmpty() is True


def test_effective_content_variety_guards_a_cycle():
    tree = schema_tree(
        "<xs:complexType name='a'><xs:complexContent><xs:extension base='b'/></xs:complexContent></xs:complexType>"
        "<xs:complexType name='b'><xs:complexContent><xs:extension base='a'/></xs:complexContent></xs:complexType>"
    )
    a_type = next(t for t in find_all(tree, "ComplexType") if t.name == "a")
    seen = {id(a_type)}
    assert a_type._effectiveContentVariety(seen) == "empty"


def test_effective_content_variety_with_an_unresolvable_base():
    tree = schema_tree(
        "<xs:complexType name='d'><xs:complexContent>"
        "<xs:extension base='Missing'/></xs:complexContent></xs:complexType>"
    )
    derived = find_all(tree, "ComplexType")[0]
    assert derived._effectiveContentVariety() == "empty"


def test_effective_open_content_guards_a_cycle():
    tree = schema_tree(
        "<xs:complexType name='a'><xs:complexContent><xs:extension base='b'/></xs:complexContent></xs:complexType>"
        "<xs:complexType name='b'><xs:complexContent><xs:extension base='a'/></xs:complexContent></xs:complexType>"
    )
    a_type = next(t for t in find_all(tree, "ComplexType") if t.name == "a")
    seen = {id(a_type)}
    assert a_type.effectiveOpenContent(seen) is None


def test_effective_open_content_copies_an_own_wildcard():
    from pyxsd.open_content import OpenContent

    tree = schema_tree("<xs:complexType name='t'><xs:sequence/></xs:complexType>")
    complex_type = find_all(tree, "ComplexType")[0]
    complex_type.openContent = OpenContent(
        "suffix", WildcardSpec(namespace="##any", process_contents="lax", is_attribute=False)
    )
    result = complex_type.effectiveOpenContent()
    assert result.mode == "suffix"
    assert result.wildcard is not complex_type.openContent.wildcard


def test_flatten_group_ref_reports_a_group_without_a_content_model(tmp_path):
    compiled = compile_schema(
        "<xs:group name='g'/><xs:complexType name='t'><xs:group ref='g'/></xs:complexType>",
        tmp_path,
    )
    assert any("no content model" in issue.message for issue in compiled.report.errors)


def test_get_elements_reports_an_unresolvable_group_ref(tmp_path):
    compiled = compile_schema(
        "<xs:complexType name='t'><xs:group ref='ghost'/></xs:complexType>",
        tmp_path,
    )
    assert "unknown-group" in error_codes(compiled.report)


def test_compositor_elements_skips_rejected_children():
    from pyxsd.element_representatives.complex_type import ComplexType

    compositor = SimpleNamespace(
        processedChildren=[None, SimpleNamespace(__class__=SimpleNamespace(__name__="Other"))]
    )
    result = ComplexType._compositorElements(SimpleNamespace(), compositor, None, frozenset(), "g")
    assert result == []


def test_compositor_elements_nested_compositor_value_error():
    from pyxsd.element_representatives.complex_type import ComplexType

    nested = SimpleNamespace(
        __class__=SimpleNamespace(__name__="Sequence"),
        tagType="not-a-compositor",
        processedChildren=[],
    )
    compositor = SimpleNamespace(processedChildren=[nested])
    result = ComplexType._compositorElements(
        SimpleNamespace(_resolveElementRef=lambda site: None), compositor, None, frozenset(), "g"
    )
    assert result == []


def test_get_elements_skips_non_compositor_items():
    from pyxsd.element_representatives.complex_type import ComplexType

    stub = SimpleNamespace(
        elements_=None,
        sequencesOrChoices=[SimpleNamespace(tagType="not-a-compositor", elements=[])],
    )
    stub._flattenGroupRef = lambda site, visited: []
    assert ComplexType.getElements(stub) == []


@pytest.mark.parametrize("value", ["", "not-a-number"])
def test_silent_occurs_and_occurs_value_agree(value):
    tree = schema_tree("<xs:element name='a'/>")
    element = find_all(tree, "Element")[0]
    element.maxOccurs = value
    assert element._silentOccurs("maxOccurs") == 1


def test_add_super_class_name_is_idempotent():
    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence><xs:element name='a'/></xs:sequence></xs:complexType>"
    )
    element = find_all(tree, "Element")[0]
    element.addSuperClassName(None)
    assert element.getContainingType().superClassNames == []


def test_generated_simple_type_derives_from_its_base(tmp_path):
    compiled = compile_schema(
        "<xs:simpleType name='s'><xs:restriction base='xs:string'/></xs:simpleType>",
        tmp_path,
    )
    assert issubclass(compiled.classes["s"], XsdDataType)


# ---------------------------------------------------------------------------
# Additional type-resolution / class-building paths
# ---------------------------------------------------------------------------


def test_union_with_a_piped_member_name(tmp_path):
    compiled = compile_schema(
        "<xs:simpleType name='u'><xs:union memberTypes='t|member'/></xs:simpleType>",
        tmp_path,
    )
    assert "unknown-type" in error_codes(compiled.report)


def test_constraint_namespace_without_a_namespace_context():
    from pyxsd.xsd_data_types import String

    tree = schema_tree(
        "<xs:simpleType name='s'><xs:restriction base='xs:string'>"
        "<xs:minLength value='1'/></xs:restriction></xs:simpleType>"
    )
    simple = find_all(tree, "SimpleType")[0]
    host = SimpleNamespace(mode=ParseModes.STRICT)
    namespace = XsdType._constraintNamespace(simple, host, simple, String, None)
    assert isinstance(namespace, dict)


def test_restriction_new_handles_a_missing_value(tmp_path):
    compiled = compile_schema(
        "<xs:simpleType name='s'><xs:restriction base='xs:string'>"
        "<xs:minLength value='1'/></xs:restriction></xs:simpleType>",
        tmp_path,
    )
    cls = compiled.classes["s"]
    assert cls("ab") is not None
    assert cls() is not None
    # A non-string lexical skips the whitespace transform.
    assert cls(5) is not None


def test_restriction_with_only_an_assertion_has_no_facet_constraints(tmp_path):
    compiled = compile_schema(
        "<xs:simpleType name='s'><xs:restriction base='xs:string'>"
        "<xs:assertion test='string-length($value) &gt; 0'/>"
        "</xs:restriction></xs:simpleType>",
        tmp_path,
    )
    cls = compiled.classes["s"]
    assert cls("x") is not None
    assert getattr(cls, "_assertionFacets_", None)


def test_class_with_open_content_and_no_particle(tmp_path):
    compiled = compile_schema(
        "<xs:complexType name='t'>"
        "<xs:openContent mode='suffix'><xs:any/></xs:openContent>"
        "<xs:complexContent><xs:extension base='Missing'/></xs:complexContent>"
        "</xs:complexType>",
        tmp_path,
    )
    cls = compiled.classes["t"]
    assert "_contentModel_" not in cls.__dict__
    assert "_instanceContentModel_" not in cls.__dict__


def test_stamp_owner_schema_swallows_a_broken_schema():
    class _Broken:
        def getSchema(self):
            raise RuntimeError("detached")

    class _Cls:
        pass

    assert XsdType._stampOwnerSchema(_Broken(), _Cls) is None


def test_simple_content_namespace_with_an_unbuildable_inline_type():
    tree = schema_tree(
        "<xs:complexType name='t'><xs:simpleContent><xs:restriction base='xs:string'>"
        "<xs:simpleType><xs:restriction base='xs:string'/></xs:simpleType>"
        "</xs:restriction></xs:simpleContent></xs:complexType>"
    )
    complex_type = find_all(tree, "ComplexType")[0]
    inline = find_all(complex_type, "SimpleType")[0]
    inline._generatedClass = xsd_type_module._CLASS_IN_PROGRESS
    host = SimpleNamespace(mode=ParseModes.STRICT, components=ComponentTable())
    assert complex_type._simpleContentNamespace(host, []) == {}


def test_class_attribute_alias_loops_past_a_taken_key(tmp_path):
    compiled = compile_schema(
        "<xs:complexType name='seed'><xs:sequence/></xs:complexType>", tmp_path
    )
    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence><xs:element name='a|2'/></xs:sequence>"
        "</xs:complexType>"
    )
    complex_type = find_all(tree, "ComplexType")[0]

    def make_attr(expanded):
        return SimpleNamespace(
            isAttributeRef=False,
            name="a",
            expandedInstanceName=lambda is_attribute=True, parser=None: expanded,
            getUse=lambda: "optional",
        )

    complex_type.attributes = {"one": make_attr("a"), "two": make_attr("{urn:x}a")}
    cls = complex_type.clsFor(compiled._host)
    assert "a|3" in cls.__dict__


def test_class_creation_failure_is_logged_and_reraised(tmp_path):
    compiled = compile_schema(
        "<xs:simpleType name='s'><xs:restriction base='xs:string'/></xs:simpleType>", tmp_path
    )
    tree = schema_tree("<xs:simpleType name='s'><xs:restriction base='xs:string'/></xs:simpleType>")
    simple = find_all(tree, "SimpleType")[0]
    # A duplicate base cannot be linearized into a class.
    simple.superClassNames = ["xs:string", "xs:string"]
    with pytest.raises(TypeError):
        simple.clsFor(compiled._host)


def test_declared_member_class_prefers_the_declared_document(tmp_path):
    compiled = compile_raw(
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
        "xmlns:t='urn:t' targetNamespace='urn:t' elementFormDefault='qualified'>"
        "<xs:simpleType name='member'><xs:restriction base='xs:string'/></xs:simpleType>"
        "<xs:simpleType name='u'><xs:union memberTypes='t:member xs:int'/></xs:simpleType>"
        "</xs:schema>",
        tmp_path,
        mode=ParseModes.NAMESPACED,
    )
    union_class = compiled.classes["u"]
    assert str(union_class("hello")) == "hello"


def test_declared_member_class_falls_back_when_the_member_is_building(tmp_path):
    compiled = compile_raw(
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
        "xmlns:t='urn:t' targetNamespace='urn:t' elementFormDefault='qualified'>"
        "<xs:simpleType name='m'><xs:restriction base='t:u'/></xs:simpleType>"
        "<xs:simpleType name='u'><xs:union memberTypes='t:m xs:string'/></xs:simpleType>"
        "</xs:schema>",
        tmp_path,
        mode=ParseModes.NAMESPACED,
    )
    # The circular member reference is reported rather than crashing.
    assert "unknown-type" in error_codes(compiled.report)


# ---------------------------------------------------------------------------
# Element write-through and complex-type internals
# ---------------------------------------------------------------------------


def test_element_set_does_not_write_through_a_non_datatype(tmp_path):
    compiled = compile_schema(
        "<xs:element name='r'><xs:complexType><xs:sequence>"
        "<xs:element name='child'><xs:complexType><xs:sequence/></xs:complexType>"
        "</xs:element></xs:sequence></xs:complexType></xs:element>",
        tmp_path,
    )
    doc = parse_instance(compiled, "<r/>", tmp_path)
    holder = doc.root
    descriptor = type(holder).child
    value = descriptor.getType()()
    Element.__set__(descriptor, holder, value)
    assert holder.__dict__["child"] is value


def test_compositor_elements_tolerates_a_non_compositor_nested_kind():
    from pyxsd.element_representatives.complex_type import ComplexType

    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence><xs:sequence>"
        "<xs:element name='a'/></xs:sequence></xs:sequence></xs:complexType>"
    )
    sequences = find_all(tree, "Sequence")
    outer, inner = sequences[0], sequences[1]
    inner.tagType = "bogus"
    type_er = find_all(tree, "ComplexType")[0]
    result = ComplexType._compositorElements(type_er, outer, None, frozenset(), "g")
    assert [element.name for element in result] == ["a"]


def test_flatten_group_ref_tolerates_a_non_compositor(tmp_path):
    compiled = compile_schema(
        "<xs:group name='g'><xs:sequence><xs:element name='a'/></xs:sequence></xs:group>"
        "<xs:complexType name='t'><xs:group ref='g'/></xs:complexType>",
        tmp_path,
    )
    group_def = next(
        entry for entry in entries_of(compiled, "Group") if not getattr(entry, "isRefSite", False)
    )
    ref_site = next(
        entry for entry in entries_of(compiled, "Group") if getattr(entry, "isRefSite", False)
    )
    type_er = entries_of(compiled, "ComplexType")[0]
    group_def.getCompositor().tagType = "bogus"
    contributed = type_er._flattenGroupRef(ref_site, frozenset())
    assert [element.name for element in contributed] == ["a"]


def test_flatten_group_ref_registers_attribute_wildcards(tmp_path):
    compiled = compile_schema(
        "<xs:group name='g'><xs:sequence><xs:element name='a'/></xs:sequence></xs:group>"
        "<xs:complexType name='t'><xs:group ref='g'/></xs:complexType>",
        tmp_path,
    )
    group_def = next(
        entry for entry in entries_of(compiled, "Group") if not getattr(entry, "isRefSite", False)
    )
    ref_site = next(
        entry for entry in entries_of(compiled, "Group") if getattr(entry, "isRefSite", False)
    )
    type_er = entries_of(compiled, "ComplexType")[0]
    spec = WildcardSpec(
        namespace="##other", process_contents="strict", is_attribute=True, target_namespace=None
    )
    group_def.wildcardAttributeSpecs = [spec]
    type_er.wildcardAttributeSpecs = []
    type_er._flattenGroupRef(ref_site, frozenset())
    assert spec in type_er.wildcardAttributeSpecs
