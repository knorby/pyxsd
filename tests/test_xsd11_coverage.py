"""Coverage tests for the XSD 1.1 assertion/alternative/open-content phases.

Complements ``test_xsd11_features.py`` with the defensive and edge paths of
``pyxsd.assertions``, ``pyxsd.alternatives``, ``pyxsd.open_content`` and
``pyxsd.xpath_assertions``: unusable declarations that must be reported
instead of raised, type-adapter fallbacks that keep assertion evaluation
total, and the open-content combination/derivation rules.
"""

import io
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from pyxsd.alternatives import (
    _UNSET,
    _alternatives_owner,
    _class_label,
    _is_error_name,
    check_element_alternatives,
    compile_alternatives,
    select_alternative_type,
)
from pyxsd.assertions import (
    SimpleAssertion,
    _assert_representatives,
    _ElementPathType,
    _simple_assertion_value,
    attribute_type_map,
    check_element_assertions,
    compile_simple_assertions,
    element_type_map,
)
from pyxsd.binding import ParseModes
from pyxsd.namespaces import XSD_NS, clark
from pyxsd.open_content import (
    OpenContent,
    _covered_by_particle,
    _particle_emptiable,
    _particle_empty,
    _process_strength,
    combine_open_content,
    default_open_content_element,
    namespace_resolver,
    open_content_derivation_problem,
)
from pyxsd.schema import Schema
from pyxsd.wildcards import wildcard_spec
from pyxsd.xpath_assertions import (
    assertion_requires_context,
    evaluate,
    parse_assertion_xpath,
    parse_cta_xpath,
)
from pyxsd.xpath_subset import XPathError

XS = "http://www.w3.org/2001/XMLSchema"
NS = {"xs": XS}

SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"{extra}>
{body}
</xs:schema>"""


def parse_doc(body, xml, extra=""):
    return Schema.compile(
        io.StringIO(SCHEMA.format(body=body, extra=extra)), mode=ParseModes.NAMESPACED
    ).parse(io.StringIO(xml))


def parse(body, xml, extra=""):
    return parse_doc(body, xml, extra).report


def codes(report):
    return [issue.code for issue in report.errors]


def component(doc, *type_names):
    for entries in doc.schema.components.values():
        for entry in entries:
            if type(entry).__name__ in type_names:
                return entry
    raise AssertionError(f"no component of type {type_names}")


# --- xs:assert / xs:assertion declaration legality --------------------------


def test_assert_rejects_unknown_but_not_foreign_attributes() -> None:
    body = (
        '<xs:element name="t"><xs:complexType><xs:sequence/>'
        '<xs:attribute name="x" type="xs:string"/>'
        '<xs:assert test="@x = \'1\'" bogus="x" xmlns:f="urn:f" f:x="1"/>'
        "</xs:complexType></xs:element>"
    )
    assert codes(parse(body, '<t x="1"/>')) == ["declaration-attribute"]


def test_assertion_facet_rejects_unknown_but_not_foreign_attributes() -> None:
    body = (
        '<xs:element name="t"><xs:simpleType><xs:restriction base="xs:string">'
        '<xs:assertion test="$value = \'ok\'" bogus="x" xmlns:f="urn:f" f:x="1"/>'
        "</xs:restriction></xs:simpleType></xs:element>"
    )
    assert codes(parse(body, "<t>ok</t>")) == ["declaration-attribute"]


def test_misplaced_assert_inside_complex_content_is_reported() -> None:
    body = (
        '<xs:element name="t"><xs:complexType><xs:complexContent>'
        '<xs:assert test="true"/>'
        '<xs:restriction base="xs:anyType"><xs:sequence/></xs:restriction>'
        "</xs:complexContent></xs:complexType></xs:element>"
    )
    assert "declaration-child" in codes(parse(body, "<t/>"))


def test_assertion_facet_nested_in_annotated_simple_content() -> None:
    body = (
        '<xs:complexType name="base"><xs:simpleContent>'
        '<xs:extension base="xs:string"/></xs:simpleContent></xs:complexType>'
        '<xs:element name="t"><xs:complexType><xs:simpleContent>'
        "<xs:annotation><xs:documentation>d</xs:documentation></xs:annotation>"
        '<xs:restriction base="base">'
        "<xs:assertion test=\"$value = 'ok'\"/>"
        "</xs:restriction>"
        "</xs:simpleContent></xs:complexType></xs:element>"
    )
    assert codes(parse(body, "<t>ok</t>")) == []
    assert codes(parse(body, "<t>bad</t>")) == ["assert-failed"]


def test_assert_skips_bad_children() -> None:
    body = (
        '<xs:complexType name="T"><xs:sequence/>'
        '<xs:element name="x"/>'
        "<xs:assert test=\"@x = '1'\"/>"
        '<xs:attribute name="x" type="xs:string"/></xs:complexType>'
        '<xs:element name="t" type="T"/>'
    )
    assert "assert-failed" not in codes(parse(body, '<t x="1"/>'))
    assert "assert-failed" in codes(parse(body, "<t/>"))


def test_compile_simple_assertions_caches_per_type() -> None:
    class _Representative:
        def compile(self):
            return SimpleAssertion("true()", None, {}, None, False)

    class _Source:
        assertions = None

    _Source.assertions = [_Representative()]

    source = _Source()
    assert compile_simple_assertions(source) is compile_simple_assertions(source)


def test_check_element_assertions_without_assertions_is_silent() -> None:
    class _Plain:
        pass

    check_element_assertions(_Plain, ET.fromstring("<t/>"))


# --- elementpath type adapter and declared-type maps -------------------------


def test_elementpath_type_adapter_surface() -> None:
    adapter = _ElementPathType(clark(XSD_NS, "date"))
    assert adapter.is_simple() is True
    assert adapter.is_list() is False
    assert adapter.parent is None
    assert adapter.simple_type is None
    assert adapter.root_type is adapter


def _descriptor_holder():
    from pyxsd import xsd_data_types

    class _Broken:
        def getType(self):
            raise RuntimeError("boom")

    class _Nameless:
        def getType(self):
            return xsd_data_types.Date

        name = None

        def instanceName(self, parser=None):
            raise RuntimeError("no instance name")

    class _ClarkNamed:
        def getType(self):
            return xsd_data_types.Date

        name = "d"

        def instanceName(self, parser=None):
            return "{urn:x}d"

    return _Broken, _Nameless, _ClarkNamed


def test_attribute_type_map_skips_unusable_descriptors() -> None:
    broken_cls, nameless_cls, clark_cls = _descriptor_holder()
    broken, nameless, clark = broken_cls(), nameless_cls(), clark_cls()

    class _Holder:
        _attributeNames_ = ("absent", "broken", "nameless", "clark")

        absent = None
        broken_descriptor = broken
        nameless_descriptor = nameless
        clark_descriptor = clark

    _Holder.broken = _Holder.broken_descriptor
    _Holder.nameless = _Holder.nameless_descriptor
    _Holder.clark = _Holder.clark_descriptor

    assert set(attribute_type_map(_Holder)) == {"d", "{urn:x}d"}


def test_element_type_map_skips_unusable_descriptors_and_recurses() -> None:
    from pyxsd import xsd_data_types

    broken_cls, nameless_cls, clark_cls = _descriptor_holder()
    broken, nameless, clark = broken_cls(), nameless_cls(), clark_cls()

    class _BrokenNamed:
        def getType(self):
            return xsd_data_types.Date

        name = "e"

        def instanceName(self, parser=None):
            raise RuntimeError("no instance name")

    broken_named = _BrokenNamed()

    class _Child:
        _elementNames_ = ("shared",)

    _Child.shared = clark

    class _ChildDescriptor:
        def getType(self):
            return _Child

        name = "child"

    class _Odd:
        def getType(self):
            return SimpleNamespace()

    class _Holder:
        _elementNames_ = ("absent", "broken", "nameless", "odd", "child", "clark", "broken_named")

        absent = None
        broken_descriptor = broken
        nameless_descriptor = nameless
        odd = _Odd()
        child = _ChildDescriptor()
        clark_descriptor = clark
        broken_named_descriptor = broken_named

    _Holder.broken = _Holder.broken_descriptor
    _Holder.nameless = _Holder.nameless_descriptor
    _Holder.clark = _Holder.clark_descriptor
    _Holder.broken_named = _Holder.broken_named_descriptor

    assert set(element_type_map(_Holder)) == {"d", "{urn:x}d", "e"}


def test_element_type_map_cycle_guard() -> None:
    class _Self:
        _elementNames_ = ()

    assert element_type_map(_Self, _seen={id(_Self)}) == {}


# --- $value for simple-content complex types ---------------------------------


def test_assert_list_simple_content_sees_list_items() -> None:
    body = (
        '<xs:simpleType name="ints"><xs:list itemType="xs:integer"/></xs:simpleType>'
        '<xs:element name="t"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="ints">'
        '<xs:assert test="count($value) eq 2"/>'
        "</xs:extension></xs:simpleContent></xs:complexType></xs:element>"
    )
    assert codes(parse(body, "<t>1 2</t>")) == []
    assert codes(parse(body, "<t>1</t>")) == ["assert-failed"]


def test_assert_union_simple_content_value_stays_empty() -> None:
    body = (
        '<xs:simpleType name="u"><xs:union memberTypes="xs:date xs:integer"/></xs:simpleType>'
        '<xs:element name="t"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="u">'
        '<xs:assert test="empty($value)"/>'
        "</xs:extension></xs:simpleContent></xs:complexType></xs:element>"
    )
    assert codes(parse(body, "<t>5</t>")) == []


def test_assert_list_of_union_items_stay_untyped() -> None:
    body = (
        '<xs:simpleType name="u"><xs:union memberTypes="xs:date xs:integer"/></xs:simpleType>'
        '<xs:simpleType name="lu"><xs:list itemType="u"/></xs:simpleType>'
        '<xs:element name="t"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="lu">'
        '<xs:assert test="count($value) eq 2"/>'
        "</xs:extension></xs:simpleContent></xs:complexType></xs:element>"
    )
    assert codes(parse(body, "<t>1 2</t>")) == ["assert-failed"]


def test_assertion_facet_list_of_union_stays_empty_value() -> None:
    body = (
        '<xs:simpleType name="u"><xs:union memberTypes="xs:int xs:date"/></xs:simpleType>'
        '<xs:simpleType name="lu"><xs:list itemType="u"/></xs:simpleType>'
        '<xs:element name="t"><xs:simpleType><xs:restriction base="lu">'
        '<xs:assertion test="count($value) eq 2"/>'
        "</xs:restriction></xs:simpleType></xs:element>"
    )
    assert codes(parse(body, "<t>1 2</t>")) == ["assert-failed"]


def test_simple_assertion_value_of_untyped_instance_is_empty() -> None:
    class _Opaque:
        pass

    assert _simple_assertion_value(_Opaque(), "x") == []


# --- xs:alternative schema and selection phases ------------------------------


def test_alternative_attributes_and_annotation_child() -> None:
    body = (
        '<xs:complexType name="Base"><xs:sequence/></xs:complexType>'
        '<xs:complexType name="Derived"><xs:complexContent>'
        '<xs:extension base="Base"><xs:sequence/></xs:extension>'
        "</xs:complexContent></xs:complexType>"
        '<xs:element name="t" type="Base">'
        '<xs:alternative test="@kind = \'d\'" type="Derived" bogus="x"'
        ' xmlns:f="urn:f" f:x="1">'
        "<xs:annotation><xs:documentation>d</xs:documentation></xs:annotation>"
        "</xs:alternative></xs:element>"
    )
    assert codes(parse(body, "<t/>")) == ["declaration-attribute"]


def test_alternative_test_free_with_unusable_xpath_default_namespace() -> None:
    body = (
        '<xs:complexType name="Base"><xs:sequence/></xs:complexType>'
        '<xs:complexType name="Derived"><xs:complexContent>'
        '<xs:extension base="Base"><xs:sequence><xs:element name="d"/></xs:sequence>'
        "</xs:extension></xs:complexContent></xs:complexType>"
        '<xs:element name="t" type="Base">'
        '<xs:alternative type="Derived" xpathDefaultNamespace="##bogus"/></xs:element>'
    )
    assert codes(parse(body, "<t><d/></t>")) == []


def test_alternative_resolve_type_class_compiles_lazily() -> None:
    doc = parse_doc(
        '<xs:complexType name="B"><xs:sequence/></xs:complexType>'
        '<xs:element name="t" type="B">'
        '<xs:alternative test="@k" type="B"/></xs:element>',
        "<t/>",
    )
    representative = component(doc, "AlternativeER")
    representative._compiled = _UNSET
    assert representative.resolveTypeClass(doc.schema._host) is not None
    assert representative._compiled is not _UNSET


def test_alternative_inline_type_with_unresolvable_base() -> None:
    body = (
        '<xs:complexType name="Base"><xs:sequence/></xs:complexType>'
        '<xs:element name="t" type="Base">'
        "<xs:alternative test=\"@k = 'x'\">"
        "<xs:complexType><xs:complexContent>"
        '<xs:extension base="Missing"><xs:sequence/></xs:extension>'
        "</xs:complexContent></xs:complexType></xs:alternative></xs:element>"
    )
    report = parse(body, "<t/>")
    assert "unknown-type" in codes(report)
    assert "alternative-invalid" in codes(report)


def test_alternative_helper_predicates() -> None:
    assert _alternatives_owner(None) is None
    assert (
        _alternatives_owner(SimpleNamespace(isElementRef=True, referredElement="target"))
        == "target"
    )
    ref = SimpleNamespace(isElementRef=True)
    assert _alternatives_owner(ref) is ref
    assert _is_error_name(None) is False
    assert _is_error_name("{http://www.w3.org/2001/XMLSchema}error") is True
    assert _is_error_name("urn:other:error") is False
    assert _class_label(int) == "int"


def test_assert_representatives_collect_asserts_from_derivations() -> None:
    class Assert:
        pass

    class ComplexContent:
        processedChildren = None

    ComplexContent.processedChildren = [Assert()]

    class _HoldingType:
        processedChildren = None

    _HoldingType.processedChildren = [ComplexContent()]

    collected = _assert_representatives(_HoldingType)
    assert len(collected) == 1
    assert isinstance(collected[0], Assert)


def test_alternative_inline_type_resolves_lazily_for_ur_type() -> None:
    body = (
        '<xs:element name="t">'
        "<xs:alternative test=\"@kind = 'x'\">"
        '<xs:complexType><xs:sequence><xs:element name="o"/></xs:sequence>'
        '<xs:anyAttribute processContents="skip"/></xs:complexType>'
        "</xs:alternative></xs:element>"
    )
    assert codes(parse(body, '<t kind="x"><o/></t>')) == []


def test_alternative_inline_type_super_names_are_checked_lazily() -> None:
    base = '<xs:complexType name="Base"><xs:sequence/></xs:complexType>'
    resolvable = (
        base + '<xs:element name="t">'
        "<xs:alternative test=\"@kind = 'x'\">"
        "<xs:complexType><xs:complexContent>"
        '<xs:extension base="Base"><xs:sequence/>'
        '<xs:anyAttribute processContents="skip"/></xs:extension>'
        "</xs:complexContent></xs:complexType></xs:alternative></xs:element>"
    )
    assert codes(parse(resolvable, '<t kind="x"/>')) == []
    unresolvable = (
        '<xs:element name="t">'
        "<xs:alternative test=\"@kind = 'x'\">"
        "<xs:complexType><xs:complexContent>"
        '<xs:extension base="Missing"><xs:sequence/></xs:extension>'
        "</xs:complexContent></xs:complexType></xs:alternative></xs:element>"
    )
    report = parse(unresolvable, '<t kind="x"/>')
    assert codes(report) == []


def test_compile_alternatives_drops_uncompilable_representatives() -> None:
    class _NullRepresentative:
        def compile(self):
            return None

    element = SimpleNamespace(alternatives=[_NullRepresentative()])
    assert compile_alternatives(element) == []


def test_alternative_without_type_under_ur_type_keeps_declared_type() -> None:
    body = '<xs:element name="t"><xs:alternative test="@k = \'x\'"/></xs:element>'
    assert "alternative-invalid" in codes(parse(body, "<t k='x'/>"))


def test_alternative_usable_check_survives_inline_build_failure(monkeypatch) -> None:
    doc = parse_doc(
        '<xs:element name="t">'
        "<xs:alternative test=\"@kind = 'x'\">"
        "<xs:complexType><xs:sequence/></xs:complexType>"
        "</xs:alternative></xs:element>",
        '<t kind="x"/>',
    )
    representative = component(doc, "AlternativeER")
    representative._compiled.type_class = None

    def _raise(host):
        raise RuntimeError("boom")

    monkeypatch.setattr(representative._compiled.inline_type, "clsFor", _raise)
    assert (
        select_alternative_type(
            component(doc, "Element"), ET.fromstring('<t kind="x"/>'), doc.schema._host
        )
        is None
    )


def test_selected_alternative_survives_qname_resolution_failure(monkeypatch) -> None:
    doc = parse_doc(
        '<xs:complexType name="B"><xs:sequence/></xs:complexType>'
        '<xs:element name="t">'
        '<xs:alternative test="@k" type="B"/></xs:element>',
        "<t/>",
    )
    representative = component(doc, "AlternativeER")

    def _raise(raw, parser=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(representative, "resolveSchemaQName", _raise)
    assert (
        select_alternative_type(
            component(doc, "Element"), ET.fromstring("<t k='1'/>"), doc.schema._host
        )
        is None
    )


def test_compile_alternatives_is_idempotent() -> None:
    doc = parse_doc(
        '<xs:complexType name="B"><xs:sequence/></xs:complexType>'
        '<xs:element name="t" type="B">'
        '<xs:alternative test="@k" type="B"/></xs:element>',
        "<t/>",
    )
    element = component(doc, "Element")
    assert compile_alternatives(element) is compile_alternatives(element)


def test_alternative_without_type_falls_back_to_declared() -> None:
    body = (
        '<xs:complexType name="Base"><xs:sequence>'
        '<xs:element name="s"/></xs:sequence></xs:complexType>'
        '<xs:element name="t" type="Base">'
        '<xs:alternative test="@k"/></xs:element>'
    )
    report = parse(body, "<t><s/></t>")
    assert "alternative-invalid" in codes(report)
    assert "unexpected-element" not in codes(report)


def test_alternative_inline_type_governs_instance() -> None:
    body = (
        '<xs:complexType name="Base"><xs:sequence>'
        '<xs:element name="shared"/></xs:sequence>'
        '<xs:attribute name="kind" type="xs:string"/></xs:complexType>'
        '<xs:element name="t" type="Base">'
        "<xs:alternative test=\"@kind = 'x'\">"
        "<xs:complexType><xs:complexContent>"
        '<xs:extension base="Base"><xs:sequence><xs:element name="o"/></xs:sequence>'
        "</xs:extension></xs:complexContent></xs:complexType>"
        "</xs:alternative></xs:element>"
    )
    assert codes(parse(body, '<t kind="x"><shared/><o/></t>')) == []
    assert codes(parse(body, '<t kind="x"><shared/></t>')) != []


def test_selected_alternative_survives_resolution_failure(monkeypatch) -> None:
    doc = parse_doc(
        '<xs:complexType name="B"><xs:sequence/></xs:complexType>'
        '<xs:element name="t" type="B">'
        '<xs:alternative test="@k" type="B"/></xs:element>',
        "<t/>",
    )
    element = component(doc, "Element")
    representative = element.alternatives[0]
    representative._compiled.type_class = None

    def _raise(host):
        raise RuntimeError("boom")

    monkeypatch.setattr(representative, "resolveTypeClass", _raise)
    assert select_alternative_type(element, ET.fromstring("<t k='1'/>"), doc.schema._host) is None


def test_check_element_alternatives_survives_broken_elements() -> None:
    class _BrokenGetType:
        name = "e"
        compiledAlternatives = None

        def getType(self):
            raise RuntimeError("boom")

    _BrokenGetType.compiledAlternatives = [object()]

    check_element_alternatives(_BrokenGetType())

    class _NoHost:
        name = "e"
        compiledAlternatives = None

        def getType(self):
            return int

        def getSchema(self):
            return SimpleNamespace()

    _NoHost.compiledAlternatives = [object()]

    check_element_alternatives(_NoHost())


def test_alternative_inline_type_not_derived_is_reported() -> None:
    body = (
        '<xs:complexType name="Base"><xs:sequence/></xs:complexType>'
        '<xs:complexType name="Stranger"><xs:sequence/></xs:complexType>'
        '<xs:element name="t" type="Base">'
        "<xs:alternative test=\"@k = 'x'\">"
        "<xs:complexType><xs:complexContent>"
        '<xs:extension base="Stranger"><xs:sequence/></xs:extension>'
        "</xs:complexContent></xs:complexType></xs:alternative></xs:element>"
    )
    assert codes(parse(body, "<t/>")) == ["alternative-invalid"]


def test_alternative_against_anonymous_declared_type_is_reported() -> None:
    body = (
        '<xs:complexType name="Stranger"><xs:sequence/></xs:complexType>'
        '<xs:element name="t"><xs:complexType><xs:sequence/></xs:complexType>'
        '<xs:alternative test="@k" type="Stranger"/></xs:element>'
    )
    assert codes(parse(body, "<t/>")) == ["alternative-invalid"]


# --- open content combination, derivation and helpers ------------------------


def test_combine_open_content_shortcuts() -> None:
    explicit = OpenContent("interleave", wildcard_spec({"namespace": "urn:a"}))
    assert combine_open_content(explicit, None) is explicit
    assert combine_open_content(explicit, OpenContent("none", None)) is explicit
    assert combine_open_content(explicit, OpenContent("suffix", None)) is explicit


def test_open_content_helpers_handle_degenerate_input() -> None:
    wildcard = wildcard_spec({"namespace": "urn:a"})
    assert _process_strength(None) == 2
    assert _covered_by_particle(wildcard, None, None) is False
    assert _particle_empty(None) is True
    assert _particle_emptiable(None) is True


def test_particle_emptiability_rules() -> None:
    def particle(kind, children=(), min_occurs=1, max_occurs=1):
        return SimpleNamespace(
            kind=kind, children=list(children), min_occurs=min_occurs, max_occurs=max_occurs
        )

    assert _particle_emptiable(particle("element", min_occurs=0)) is True
    assert _particle_emptiable(particle("element")) is False
    assert _particle_emptiable(particle("sequence", [particle("element", min_occurs=0)])) is True
    assert _particle_emptiable(particle("sequence", [particle("element")])) is False
    assert (
        _particle_emptiable(
            particle("choice", [particle("element"), particle("any", min_occurs=0)])
        )
        is True
    )
    assert _particle_emptiable(particle("all", [particle("element", min_occurs=0)])) is True
    assert _particle_emptiable(particle("other")) is False
    assert _particle_empty(particle("element", max_occurs=0)) is True
    assert _particle_empty(particle("element")) is False
    assert _particle_empty(particle("sequence")) is True


def test_open_content_derivation_rule_edges() -> None:
    wide = wildcard_spec({"namespace": "urn:a urn:b"})
    narrow = wildcard_spec({"namespace": "urn:a"})
    assert open_content_derivation_problem(None, None, "restriction") is None
    assert (
        open_content_derivation_problem(
            OpenContent("interleave", narrow),
            OpenContent("interleave", wide),
            "extension",
            derived_variety="element-only",
            base_variety="element-only",
        )
        is not None
    )
    assert (
        open_content_derivation_problem(
            OpenContent("none", None),
            OpenContent("suffix", wide),
            "extension",
            derived_variety="empty",
            base_variety="element-only",
        )
        is None
    )
    assert (
        open_content_derivation_problem(
            OpenContent("none", None), OpenContent("suffix", wide), "copy"
        )
        is None
    )


def test_restriction_open_content_admitted_by_base_particle_wildcard() -> None:
    body = (
        '<xs:complexType name="B"><xs:sequence>'
        '<xs:any namespace="urn:open" processContents="lax" minOccurs="0" maxOccurs="unbounded"/>'
        "</xs:sequence></xs:complexType>"
        '<xs:complexType name="R"><xs:complexContent><xs:restriction base="B">'
        '<xs:openContent mode="interleave">'
        '<xs:any namespace="urn:open" processContents="lax"/></xs:openContent>'
        "<xs:sequence>"
        '<xs:any namespace="urn:open" processContents="lax" minOccurs="0" maxOccurs="unbounded"/>'
        "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
        '<xs:element name="t" type="R"/>'
    )
    assert codes(parse(body, "<t/>")) == []


def test_restriction_interleave_over_unobservable_suffix_is_allowed() -> None:
    body = (
        '<xs:complexType name="B">'
        '<xs:openContent mode="suffix">'
        '<xs:any namespace="urn:open" processContents="lax"/></xs:openContent>'
        '<xs:sequence><xs:element name="a" minOccurs="0" maxOccurs="unbounded"/></xs:sequence>'
        "</xs:complexType>"
        '<xs:complexType name="R"><xs:complexContent><xs:restriction base="B">'
        '<xs:openContent mode="interleave">'
        '<xs:any namespace="urn:open" processContents="lax"/></xs:openContent>'
        '<xs:sequence><xs:element name="a" minOccurs="0" maxOccurs="0"/></xs:sequence>'
        "</xs:restriction></xs:complexContent></xs:complexType>"
        '<xs:element name="t" type="R"/>'
    )
    assert codes(parse(body, "<t/>")) == []


def test_namespace_resolver_is_absent_without_context() -> None:
    assert namespace_resolver(SimpleNamespace(namespaceContext=object()), None) is None
    assert namespace_resolver(SimpleNamespace(), ET.fromstring("<t/>")) is None


def test_default_open_content_lookup() -> None:
    assert default_open_content_element(None) is None


# --- open content wildcard and defaultOpenContent grammar --------------------


def test_open_content_not_qname_resolves_declared_prefix() -> None:
    body = (
        '<xs:element name="t"><xs:complexType>'
        '<xs:openContent mode="interleave">'
        '<xs:any notQName="pre:x" xmlns:pre="urn:p" processContents="lax"/></xs:openContent>'
        "<xs:sequence/></xs:complexType></xs:element>"
    )
    assert codes(parse(body, "<t/>")) == []


def test_open_content_not_qname_outside_constraint_is_reported() -> None:
    body = (
        '<xs:element name="t"><xs:complexType>'
        '<xs:openContent mode="interleave">'
        '<xs:any namespace="urn:ok" notQName="thing" processContents="lax"/></xs:openContent>'
        "<xs:sequence/></xs:complexType></xs:element>"
    )
    assert "wildcard-invalid" in codes(parse(body, "<t/>"))


def test_default_open_content_attribute_legality() -> None:
    body = (
        '<xs:defaultOpenContent mode="suffix" bogus="x" xmlns:f="urn:f" f:x="1">'
        "<xs:any/></xs:defaultOpenContent>"
        '<xs:element name="t" type="xs:string"/>'
    )
    assert codes(parse(body, "<t>x</t>")) == ["declaration-attribute"]


def test_default_open_content_applies_to_empty_false_is_valid() -> None:
    body = (
        '<xs:defaultOpenContent mode="suffix" appliesToEmpty="false">'
        '<xs:any namespace="urn:d"/></xs:defaultOpenContent>'
        '<xs:complexType name="E"><xs:sequence/></xs:complexType>'
        '<xs:element name="t" type="E"/>'
    )
    assert codes(parse(body, "<t/>")) == []


def test_empty_content_model_rejects_whitespace_only_content() -> None:
    # Saxon open012.n3: a complex type whose content type is *empty* has
    # no character content at all, not even whitespace.
    body = (
        '<xs:defaultOpenContent mode="suffix" appliesToEmpty="false">'
        '<xs:any namespace="urn:d"/></xs:defaultOpenContent>'
        '<xs:complexType name="E"><xs:sequence/></xs:complexType>'
        '<xs:element name="t" type="E"/>'
    )
    assert "unexpected-character" in codes(parse(body, "<t>\n  \n</t>"))


def test_element_only_content_still_allows_whitespace() -> None:
    body = (
        '<xs:complexType name="E"><xs:sequence>'
        '<xs:element name="x" minOccurs="0"/></xs:sequence></xs:complexType>'
        '<xs:element name="t" type="E"/>'
    )
    assert codes(parse(body, "<t>\n  <x/>\n</t>")) == []


_SIMPLE_CONTENT_BODY = (
    '<xs:complexType name="E"><xs:simpleContent>'
    '<xs:extension base="xs:date"><xs:attribute name="evidence"/></xs:extension>'
    "</xs:simpleContent></xs:complexType>"
    '<xs:element name="t" type="E"/>'
)


def test_simple_content_rejects_child_elements() -> None:
    # Saxon open016.n1: default open content does not apply to simple
    # content, and a simple-content element admits no child elements.
    body = (
        '<xs:defaultOpenContent mode="suffix" appliesToEmpty="false">'
        '<xs:any namespace="urn:d"/></xs:defaultOpenContent>' + _SIMPLE_CONTENT_BODY
    )
    xml = '<t xmlns:d="urn:d" evidence="none">2009-12-12<d:extra>42</d:extra></t>'
    assert "unexpected-element" in codes(parse(body, xml))


def test_simple_content_with_text_only_is_valid() -> None:
    assert codes(parse(_SIMPLE_CONTENT_BODY, '<t evidence="none">2009-12-12</t>')) == []


def test_default_open_content_rejects_stray_child() -> None:
    body = (
        '<xs:defaultOpenContent mode="suffix">'
        '<xs:element name="x"/><xs:any/></xs:defaultOpenContent>'
        '<xs:element name="t" type="xs:string"/>'
    )
    assert "declaration-child" in codes(parse(body, "<t>x</t>"))


def test_default_open_content_annotation_must_come_first() -> None:
    body = (
        '<xs:defaultOpenContent mode="suffix">'
        "<xs:any/><xs:annotation/></xs:defaultOpenContent>"
        '<xs:element name="t" type="xs:string"/>'
    )
    assert "declaration-order" in codes(parse(body, "<t>x</t>"))


def test_default_open_content_second_wildcard_is_rejected() -> None:
    body = (
        '<xs:defaultOpenContent mode="suffix">'
        "<xs:annotation><xs:documentation>d</xs:documentation></xs:annotation>"
        "<xs:any/><xs:any/></xs:defaultOpenContent>"
        '<xs:element name="t" type="xs:string"/>'
    )
    report = codes(parse(body, "<t>x</t>"))
    assert "declaration-duplicate" in report
    assert "open-content-invalid" in report


def test_open_content_inside_attribute_group_is_reported() -> None:
    body = (
        '<xs:attributeGroup name="g">'
        '<xs:openContent mode="interleave"><xs:any/></xs:openContent>'
        "</xs:attributeGroup>"
        '<xs:element name="t"/>'
    )
    assert "declaration-child" in codes(parse(body, "<t/>"))


def test_open_content_foreign_namespace_attribute_is_allowed() -> None:
    body = (
        '<xs:element name="t"><xs:complexType>'
        '<xs:openContent mode="none" xmlns:f="urn:f" f:x="1"/>'
        "<xs:sequence/></xs:complexType></xs:element>"
    )
    assert codes(parse(body, "<t/>")) == []


def test_open_content_qname_resolver_survives_missing_schema(monkeypatch) -> None:
    doc = parse_doc(
        '<xs:element name="t"><xs:complexType>'
        '<xs:openContent mode="interleave"><xs:any/></xs:openContent>'
        "<xs:sequence/></xs:complexType></xs:element>",
        "<t/>",
    )
    representative = component(doc, "OpenContentER")

    def _raise():
        raise AttributeError("no schema")

    monkeypatch.setattr(representative, "getSchema", _raise)
    assert representative._qnameResolver(None) is None


def test_open_content_without_holding_type_is_skipped(monkeypatch) -> None:
    doc = parse_doc(
        '<xs:element name="t"><xs:complexType>'
        '<xs:openContent mode="interleave"><xs:any/></xs:openContent>'
        "<xs:sequence/></xs:complexType></xs:element>",
        "<t/>",
    )
    representative = component(doc, "OpenContentER")
    monkeypatch.setattr(representative, "getContainingType", lambda: None)
    representative.checkDeclarationLegality()


def test_default_open_content_declaration_survives_missing_schema(monkeypatch) -> None:
    doc = parse_doc(
        '<xs:defaultOpenContent mode="suffix"><xs:any/></xs:defaultOpenContent>'
        '<xs:element name="t" type="xs:string"/>',
        "<t>x</t>",
    )
    representative = component(doc, "DefaultOpenContentER")

    def _raise():
        raise AttributeError("no schema")

    monkeypatch.setattr(representative, "getSchema", _raise)
    assert representative._qnameResolver() is None
    monkeypatch.setattr(representative, "getSchema", lambda: None)
    representative.checkDeclarationLegality()


# --- assertion / CTA XPath subset edges --------------------------------------


def test_assertion_context_detection_covers_axes_and_bare_names() -> None:
    assert assertion_requires_context(parse_assertion_xpath("attribute::x = '1'", NS))
    assert assertion_requires_context(parse_assertion_xpath("foo = 'x'", NS))
    assert not assertion_requires_context(parse_assertion_xpath("$value = 'x'", NS))
    from elementpath import XPath2Parser

    namespace_axis = XPath2Parser(namespaces={}).parse("namespace::*")
    assert assertion_requires_context(namespace_axis)


def test_cta_rejects_wildcard_attr() -> None:
    with pytest.raises(XPathError, match="malformed attribute test"):
        parse_cta_xpath("@* = 'x'", NS)


def test_cta_rejects_empty_parentheses() -> None:
    with pytest.raises(XPathError, match="malformed parenthesized"):
        parse_cta_xpath("() = 'x'", NS)


def test_cta_cast_and_constructor_operands() -> None:
    parse_cta_xpath("'3' cast as xs:integer", NS)
    with pytest.raises(XPathError, match="not an attribute test or literal"):
        parse_cta_xpath("xs:int(.) = 1", NS)


def test_cta_rejects_bare_prefixed_name() -> None:
    with pytest.raises(XPathError, match="prefixed name"):
        parse_cta_xpath("p:name = '1'", {"p": "urn:p", "xs": XS})


def test_cta_cast_target_must_not_be_constructor_call() -> None:
    with pytest.raises(XPathError, match="cast target"):
        parse_cta_xpath("@a cast as xs:int(3) = 1", NS)


def test_cta_admits_unprefixed_constructor_under_default_namespace() -> None:
    compiled = parse_cta_xpath("int(@n) = 1", {}, default_namespace=XS)
    assert evaluate(compiled, ET.fromstring("<t n='1'/>")) is True
    assert evaluate(compiled, ET.fromstring("<t n='2'/>")) is False


def test_assert_ignores_undeclared_xsi_attributes() -> None:
    body = (
        '<xs:complexType name="T"><xs:sequence/>'
        '<xs:attribute name="x" type="xs:string"/>'
        "<xs:assert test=\"@x = '1'\"/></xs:complexType>"
        '<xs:element name="t" type="T"/>'
    )
    xml = '<t xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="T" x="1"/>'
    assert codes(parse(body, xml)) == []
