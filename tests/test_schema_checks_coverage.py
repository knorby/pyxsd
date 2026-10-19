"""Coverage tests for ``pyxsd.schema_checks`` error paths and branches.

Every test pins an observable result: a returned value, a report issue
``code``/``message``, a reported log line, or a mutated attribute. The
schema-driven tests drive the real compile pipeline (``Schema.compile``)
through the shared helpers in :mod:`tests.test_misc_coverage`; the small
unit tests exercise the module's pure lookups and its defensive
``except`` arms with purpose-built stand-ins where the pipeline cannot
reach them.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from io import StringIO
from types import SimpleNamespace

from pyxsd.binding import ParseModes
from pyxsd.content_model import Particle
from pyxsd.namespaces import XSD_NS
from pyxsd.schema import Schema
from pyxsd.schema_checks import (
    _authoredAttributeDeclaration,
    _authoredValueClass,
    _GroupRedefineDeclaration,
    _resolve_head,
    _resolve_members,
    _sameIntegerFamily,
    _sameNameAttributeDeclarations,
    alternative_type_key,
    base_type_er,
    check_all_wildcard_overlap,
    check_declaration_value_constraint,
    check_substitution_edc,
    check_substitution_overlap,
    check_wildcard_element_edc,
    check_wildcard_particle_overlap,
    class_of_particle_entry,
    complex_type_derivation,
    containing_group_definition,
    declared_type_class,
    fixed_values_equal,
    global_element_lookup,
    group_redefine_has_self_reference,
    group_redefine_resolver,
    group_ref_definition,
    group_ref_sites,
    model_element_declarations,
    particle_is_empty,
    referenced_group_ids,
    report_extension_all_overlap,
    report_unique_particle_attribution,
    report_unqualified_type_candidate,
    resolve_particles,
    simple_content_inline_class,
    substitution_head_lookup,
    substitution_member_lookup,
)
from pyxsd.validation import ValidationReport
from test_misc_coverage import error_codes, find_all, schema_tree
from test_misc_coverage import schema_report as _misc_schema_report

#: Re-export the shared fixture under the name pytest resolves for it.
schema_report = _misc_schema_report


# ---------------------------------------------------------------------------
# Small stand-ins
# ---------------------------------------------------------------------------


def _raising(name):
    """A callable that raises ``AttributeError`` with *name* in the text."""

    def _raise(*_args, **_kwargs):
        raise AttributeError(name)

    return _raise


class _Decl:
    """A wrapped declaration whose ``getType`` returns a canned value."""

    def __init__(self, cls):
        self._cls = cls

    def getType(self):
        return self._cls


class _TypeRaises:
    """An element declaration whose ``getType`` raises ``TypeError``."""

    def getType(self):
        raise TypeError("no type")


class _XsElementGet:
    """A wildcard child's ``xsdElement`` with no ``notNamespace``/``notQName``."""

    def get(self, key):
        return None


def _attribute_child(name, *, is_ref=False, namespace="urn:t"):
    """A global attribute-declaration stand-in for the authored lookups."""
    return type(
        "Attribute",
        (),
        {
            "name": name,
            "isAttributeRef": is_ref,
            "authoredNamespace": lambda self, ns=namespace: ns,
        },
    )()


# ---------------------------------------------------------------------------
# _GroupRedefineDeclaration / _sameIntegerFamily
# ---------------------------------------------------------------------------


def test_group_redefine_declaration_get_type_variants():
    from pyxsd.xsd_data_types import Integer

    assert _GroupRedefineDeclaration(_Decl(None)).getType() is None
    # A non-class type raises TypeError inside issubclass and is passed through.
    assert _GroupRedefineDeclaration(_Decl("plain")).getType() == "plain"
    assert _GroupRedefineDeclaration(_Decl(Integer)).getType() is Integer


def test_same_integer_family_rejects_non_classes():
    assert _sameIntegerFamily("first", "second") is False


def test_group_redefine_resolver_without_a_descriptor():
    assert group_redefine_resolver(SimpleNamespace(descriptor=None)) is None


def test_group_redefine_self_reference_ignores_non_element_nodes():
    declaration = ET.Element("xs:group")
    declaration.append(ET.Comment("a note"))
    ET.SubElement(declaration, "xs:group", {"ref": "other"})
    assert group_redefine_has_self_reference(declaration, "g") is False


# ---------------------------------------------------------------------------
# fixed_values_equal
# ---------------------------------------------------------------------------


def test_fixed_values_equal_returns_false_when_types_cannot_be_built():
    class _Use:
        def __init__(self, fixed):
            self._fixed = fixed

        def getFixed(self):
            return self._fixed

        def getType(self):
            raise TypeError("no type")

    assert fixed_values_equal(_Use("1"), _Use("01")) is False


# ---------------------------------------------------------------------------
# alternative_type_key / particle_is_empty / model_element_declarations
# ---------------------------------------------------------------------------


def test_alternative_type_key_for_untyped_alternatives():
    assert alternative_type_key(SimpleNamespace(type_name=None, inline_type=None)) == ("none", None)
    inline = object()
    key = alternative_type_key(SimpleNamespace(type_name=None, inline_type=inline))
    assert key == ("inline", id(inline))


def test_particle_is_empty_reads_occurrence_and_shape():
    assert particle_is_empty(Particle("sequence", 0, 0)) is True
    assert particle_is_empty(Particle("sequence", 1, 1)) is True
    assert particle_is_empty(Particle("choice", 0, 1)) is True
    assert particle_is_empty(Particle("choice", 1, 1)) is False


def test_model_element_declarations_skips_descriptorless_particles():
    assert model_element_declarations(Particle("element", 1, 1, [])) == {}


# ---------------------------------------------------------------------------
# containing_group_definition / class_of_particle_entry
# ---------------------------------------------------------------------------


def test_containing_group_definition_walks_out_of_compositors():
    sequence = type("Sequence", (), {})()
    sequence.parent = None
    er = type("Unrelated", (), {})()
    er.parent = sequence
    assert containing_group_definition(er) is None


class _ElementDeclaration:
    def __init__(self, expanded_name, cls):
        self.expandedName = expanded_name
        self._cls = cls

    def getType(self):
        return self._cls


def test_class_of_particle_entry_resolves_declaration_keys():
    declaration = _ElementDeclaration("{urn:a}e", int)
    schema = SimpleNamespace(elements=[declaration])
    particle = SimpleNamespace(getSchema=lambda: schema)
    assert class_of_particle_entry("decl:{urn:a}e", particle) is int
    assert class_of_particle_entry("decl:{urn:a}missing", particle) is None
    assert class_of_particle_entry("xs:int", _ElementDeclaration(None, str)) is str


# ---------------------------------------------------------------------------
# substitution lookups
# ---------------------------------------------------------------------------


def test_substitution_lookups_without_a_schema():
    assert substitution_head_lookup(SimpleNamespace(getSchema=_raising("detached")), None) is None
    assert substitution_head_lookup(SimpleNamespace(getSchema=lambda: None), None) is None


def test_substitution_member_lookup_without_a_head_lookup():
    er = SimpleNamespace(getSchema=_raising("detached"))
    assert substitution_member_lookup(er, None) is None


class _HeadWithoutResolver:
    name = "member"

    def getSubstitutionGroupHead(self, parser=None):
        return "head"


def test_resolve_head_without_a_resolver():
    cache = {"by_name": {}, "candidates": []}
    assert _resolve_head(_HeadWithoutResolver(), cache, None) is None


def test_resolve_members_reads_the_block_attribute_directly():
    declaration = SimpleNamespace(tagAttributes={"block": "substitution"}, name="head")
    assert _resolve_members(declaration, {"candidates": []}, None) == []


def test_resolve_particles_skips_refs_without_a_resolver():
    particle = SimpleNamespace(isElementRef=True, getSchema=lambda: None)
    assert resolve_particles([particle]) == []


# ---------------------------------------------------------------------------
# group_ref_definition / group_ref_sites / referenced_group_ids
# ---------------------------------------------------------------------------


def test_group_ref_definition_rejects_unusable_sites():
    assert group_ref_definition(SimpleNamespace(ref=None)) is None
    assert group_ref_definition(SimpleNamespace(ref="g", getSchema=_raising("detached"))) is None
    without_groups = SimpleNamespace(ref="g", getSchema=lambda: SimpleNamespace(groups=None))
    assert group_ref_definition(without_groups) is None


def test_group_ref_definition_falls_back_to_the_group_table():
    sentinel = object()
    ref_site = SimpleNamespace(
        ref="g",
        getSchema=lambda: SimpleNamespace(groups={"g": sentinel}),
        resolveReference=lambda ref, groups, parser=None: None,
    )
    assert group_ref_definition(ref_site) is sentinel


def test_group_ref_sites_without_a_schema():
    assert group_ref_sites(SimpleNamespace(getSchema=_raising("detached"))) == []


class _SchemaStub:
    def __init__(self, children=None):
        self.processedChildren = list(children or [])


def test_referenced_group_ids_without_a_schema():
    assert referenced_group_ids(SimpleNamespace(getSchema=_raising("detached"))) == set()


def test_referenced_group_ids_handles_unusable_nodes():
    ref_site = type("Group", (), {"isRefSite": True, "ref": "missing"})()
    ref_site.processedChildren = []
    complex_type = type("ComplexType", (), {})()
    complex_type.processedChildren = [ref_site, ref_site]
    schema = _SchemaStub([complex_type, None])
    er = SimpleNamespace(getSchema=lambda: schema)
    assert referenced_group_ids(er) == set()


def test_referenced_group_ids_reuses_the_cached_schema_walk():
    schema = _SchemaStub([])
    er = SimpleNamespace(getSchema=lambda: schema)
    first = referenced_group_ids(er)
    second = referenced_group_ids(er)
    assert first == second == set()


# ---------------------------------------------------------------------------
# base_type_er / declared_type_class / unqualified-type reporting
# ---------------------------------------------------------------------------


def test_base_type_er_without_a_component_table():
    ctx = SimpleNamespace(schema_context=SimpleNamespace(components=None))
    assert base_type_er(ctx, object(), None) is None


def test_declared_type_class_survives_a_broken_get_type():
    assert declared_type_class(_TypeRaises()) is None


def test_report_unqualified_type_candidate_skips_a_missing_local_name():
    assert report_unqualified_type_candidate(None, None, "") is None


def test_report_unqualified_type_candidate_ignores_reserved_namespace_types():
    reserved_type = type("ComplexType", (), {"name": "T", "getNamespace": lambda self: XSD_NS})()
    ctx = SimpleNamespace(
        composed_target_namespaces={"urn:t"},
        resolved_imports=set(),
        schema_context=SimpleNamespace(components={"key": [reserved_type]}),
    )
    assert report_unqualified_type_candidate(ctx, SimpleNamespace(name="e"), "T") is None


# ---------------------------------------------------------------------------
# report_unique_particle_attribution / check_declaration_value_constraint
# ---------------------------------------------------------------------------


def test_report_unique_particle_attribution_ignores_non_complex_types():
    report = ValidationReport()
    assert (
        report_unique_particle_attribution(SimpleNamespace(report=report), object(), None) is None
    )
    assert report.errors == []


def test_check_declaration_value_constraint_survives_a_broken_get_type():
    er = type(
        "Element",
        (),
        {"tagAttributes": {"default": "x"}, "name": "e", "getType": _TypeRaises().getType},
    )()
    report = ValidationReport()
    check_declaration_value_constraint(SimpleNamespace(report=report), er)
    assert report.errors == []


# ---------------------------------------------------------------------------
# authored value-class lookups
# ---------------------------------------------------------------------------


def _value_declaration(namespace, value_type):
    """A global attribute stand-in whose class name is ``Attribute``."""

    def authored_namespace(self):
        return namespace

    def get_type(self):
        return value_type

    return type(
        "Attribute",
        (),
        {
            "name": "g",
            "isAttributeRef": False,
            "authoredNamespace": authored_namespace,
            "getType": get_type,
        },
    )()


def _attribute_use(candidate):
    schema = SimpleNamespace(processedChildren=[candidate])
    return type(
        "Attribute",
        (),
        {
            "tagAttributes": {"ref": "t:g"},
            "referenceTargetDeclaration": lambda self, raw: None,
            "referenceNamespace": lambda self, raw: "urn:t",
            "_effectiveSchema": lambda self: schema,
        },
    )()


def test_authored_value_class_keeps_the_primary_without_a_reference_target():
    primary = object()
    assert _authoredValueClass(SimpleNamespace(), primary) is primary


def test_authored_value_class_keeps_the_primary_when_the_referred_type_is_not_simple():
    primary = object()
    declaration = _value_declaration("urn:t", object)
    assert _authoredValueClass(_attribute_use(declaration), primary) is primary


def test_authored_attribute_declaration_without_a_matching_namespace():
    declaration = _value_declaration("urn:other", int)
    assert _authoredAttributeDeclaration(_attribute_use(declaration), "t:g") is None


def test_same_name_attribute_declarations_skips_unusable_children():
    ref_child = _attribute_child("g", is_ref=True)
    nameless = _attribute_child(123)
    named = _attribute_child("g")
    schema = SimpleNamespace(processedChildren=[ref_child, nameless, named])
    er = SimpleNamespace(_effectiveSchema=lambda: schema)
    assert _sameNameAttributeDeclarations(er, "g") == [named]


def test_same_name_attribute_declarations_without_a_schema():
    er = SimpleNamespace(_effectiveSchema=lambda: None)
    assert _sameNameAttributeDeclarations(er, "g") == []


# ---------------------------------------------------------------------------
# simple_content_inline_class
# ---------------------------------------------------------------------------


def test_simple_content_inline_class_skips_facets_and_broken_types():
    class _Facet:
        pass

    inline = type(
        "SimpleType",
        (),
        {"clsFor": lambda self, py_xsd: (_ for _ in ()).throw(TypeError("no class"))},
    )

    restriction = SimpleNamespace(processedChildren=[_Facet(), inline()])
    assert simple_content_inline_class(restriction, None) is None


def test_simple_content_inline_class_without_an_inline_type():
    restriction = SimpleNamespace(processedChildren=[type("Annotation", (), {})()])
    assert simple_content_inline_class(restriction, None) is None


# ---------------------------------------------------------------------------
# Stub-driven defensive branches
# ---------------------------------------------------------------------------


class _DetachedWildcardEr:
    def _particleChildren(self):
        return [type("Any", (), {"xsdElement": _XsElementGet(), "wildcardSpec": object()})()]

    def getSchema(self):
        raise AttributeError("detached")

    def getNamespace(self):
        raise AttributeError("detached")


def test_wildcard_element_edc_skips_a_detached_wildcard_owner():
    report = ValidationReport()
    resolved = [(("urn:x", "e"), "xs:int", object())]
    check_wildcard_element_edc(SimpleNamespace(report=report), _DetachedWildcardEr(), resolved)
    assert report.errors == []


class _NoNamespaceAll:
    processedChildren = ()

    def getNamespace(self):
        raise AttributeError("detached")


def test_all_wildcard_overlap_without_a_namespace():
    report = ValidationReport()
    check_all_wildcard_overlap(SimpleNamespace(report=report), _NoNamespaceAll())
    assert report.errors == []


class _NoNamespaceSequence:
    def _particleChildren(self):
        child = type(
            "Any",
            (),
            {
                "xsdElement": _XsElementGet(),
                "wildcardSpec": None,
                "_silentOccurs": lambda self, key: 1,
            },
        )
        return [child(), child()]

    def getContainingType(self):
        return None

    def getNamespace(self):
        raise AttributeError("detached")


def test_wildcard_particle_overlap_without_a_namespace():
    report = ValidationReport()
    check_wildcard_particle_overlap(SimpleNamespace(report=report), _NoNamespaceSequence())
    assert report.errors == []


class _NoNamespaceEr:
    def getNamespace(self):
        raise AttributeError("detached")


def test_report_extension_all_overlap_without_members():
    report = ValidationReport()
    report_extension_all_overlap(SimpleNamespace(report=report), [], [], _NoNamespaceEr(), None)
    assert report.errors == []


def test_report_extension_all_overlap_skips_descriptorless_elements():
    report = ValidationReport()
    member = Particle("element", 1, 1, [], "a")
    er = SimpleNamespace(getNamespace=lambda: "urn:t")
    report_extension_all_overlap(SimpleNamespace(report=report), [member], [], er, None)
    assert report.errors == []


def _schema_less_particle():
    return SimpleNamespace(getSchema=_raising("detached"))


def test_check_substitution_overlap_without_a_schema():
    report = ValidationReport()
    resolved = [(("urn:t", "a"), "xs:int", _schema_less_particle())]
    check_substitution_overlap(SimpleNamespace(report=report), resolved, None)
    assert report.errors == []


def test_check_substitution_overlap_ignores_non_element_globals():
    report = ValidationReport()
    schema = SimpleNamespace(elements=[object()])
    particle = SimpleNamespace(
        isElementRef=False,
        tagAttributes={},
        name="a",
        getSchema=lambda: schema,
    )
    resolved = [(("urn:t", "a"), "xs:string", particle)]
    check_substitution_overlap(SimpleNamespace(report=report), resolved, None)
    assert report.errors == []


# ---------------------------------------------------------------------------
# global_element_lookup / complex_type_derivation
# ---------------------------------------------------------------------------


def test_global_element_lookup_matches_namespace_and_local_name():
    tree = schema_tree(
        "<xs:element name='e' type='xs:string'/>"
        "<xs:complexType name='t'><xs:sequence/></xs:complexType>"
    )
    complex_type = find_all(tree, "ComplexType")[0]
    lookup = global_element_lookup(complex_type)
    element = find_all(tree, "Element")[0]
    assert lookup("e") is element
    assert lookup("{urn:other}e") is None


def test_complex_type_derivation_reads_simple_content_wrappers():
    tree = schema_tree(
        "<xs:complexType name='e'><xs:simpleContent>"
        "<xs:extension base='xs:string'/></xs:simpleContent></xs:complexType>"
        "<xs:complexType name='r'><xs:simpleContent>"
        "<xs:restriction base='xs:string'/></xs:simpleContent></xs:complexType>"
    )
    types = {ct.name: ct for ct in find_all(tree, "ComplexType") if ct.name}
    assert complex_type_derivation(types["e"]) == "extension"
    assert complex_type_derivation(types["r"]) == "restriction"


# ---------------------------------------------------------------------------
# Pipeline: redefine, default attributes/open content, identity constraints
# ---------------------------------------------------------------------------


def _write(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")


def _compile(tmp_path, name="main.xsd"):
    return Schema.compile(str(tmp_path / name), mode=ParseModes.NAMESPACED)


def test_group_redefine_without_a_derived_group(tmp_path):
    _write(
        tmp_path,
        "base.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
        "targetNamespace='urn:a' xmlns:a='urn:a'>"
        "<xs:group name='g'><xs:sequence><xs:element name='x'/></xs:sequence></xs:group>"
        "</xs:schema>",
    )
    _write(
        tmp_path,
        "main.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
        "targetNamespace='urn:a' xmlns:a='urn:a'>"
        "<xs:redefine schemaLocation='base.xsd'>"
        "<xs:attributeGroup name='g'><xs:attribute name='q' type='xs:string'/></xs:attributeGroup>"
        "</xs:redefine>"
        "<xs:element name='r' type='xs:string'/>"
        "</xs:schema>",
    )
    assert "compose-invalid" in error_codes(_compile(tmp_path).report)


def test_group_redefine_without_a_compilable_base_model(tmp_path):
    _write(
        tmp_path,
        "base.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
        "targetNamespace='urn:a' xmlns:a='urn:a'>"
        "<xs:group name='g'/>"
        "</xs:schema>",
    )
    _write(
        tmp_path,
        "main.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' "
        "targetNamespace='urn:a' xmlns:a='urn:a'>"
        "<xs:redefine schemaLocation='base.xsd'>"
        "<xs:group name='g'><xs:sequence><xs:element name='x'/></xs:sequence></xs:group>"
        "</xs:redefine>"
        "<xs:element name='r' type='xs:string'/>"
        "</xs:schema>",
    )
    assert "compose-invalid" not in error_codes(_compile(tmp_path).report)


def test_default_attributes_unbound_prefix_is_reported(tmp_path):
    _write(
        tmp_path,
        "main.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' defaultAttributes='nope:g'>"
        "<xs:attributeGroup name='g'/>"
        "<xs:element name='r' type='xs:string'/>"
        "</xs:schema>",
    )
    report = _compile(tmp_path).report
    issues = [issue for issue in report.errors if issue.code == "unknown-namespace-prefix"]
    assert len(issues) == 1
    assert "defaultAttributes" in issues[0].message


def test_default_attributes_scans_past_a_foreign_same_named_group(tmp_path):
    _write(
        tmp_path,
        "sideb.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' targetNamespace='urn:b'>"
        "<xs:attributeGroup name='da'><xs:attribute name='y' type='xs:string'/></xs:attributeGroup>"
        "</xs:schema>",
    )
    _write(
        tmp_path,
        "sidec.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' targetNamespace='urn:c'>"
        "<xs:attributeGroup name='da'><xs:attribute name='z' type='xs:string'/></xs:attributeGroup>"
        "</xs:schema>",
    )
    _write(
        tmp_path,
        "main.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' targetNamespace='urn:a' "
        "xmlns:c='urn:c' defaultAttributes='c:da'>"
        "<xs:import namespace='urn:b' schemaLocation='sideb.xsd'/>"
        "<xs:import namespace='urn:c' schemaLocation='sidec.xsd'/>"
        "<xs:element name='r' type='xs:string'/>"
        "</xs:schema>",
    )
    assert "unknown-attributeGroup" not in error_codes(_compile(tmp_path).report)


def test_default_attributes_skips_non_top_level_candidates(tmp_path):
    _write(
        tmp_path,
        "main.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' defaultAttributes='local'>"
        "<xs:complexType name='b'>"
        "<xs:attributeGroup name='local'><xs:attribute name='a' type='xs:string'/></xs:attributeGroup>"
        "</xs:complexType>"
        "<xs:element name='r' type='b'/>"
        "</xs:schema>",
    )
    issues = [
        issue
        for issue in _compile(tmp_path).report.errors
        if issue.code == "unknown-attributeGroup"
    ]
    assert len(issues) == 1


def test_default_open_content_resolves_notqname_prefixes(tmp_path):
    _write(
        tmp_path,
        "main.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' targetNamespace='urn:t' "
        "xmlns:t='urn:t'>"
        "<xs:defaultOpenContent mode='interleave' appliesToEmpty='true'>"
        "<xs:any namespace='##any' notQName='t:foo' processContents='lax'/>"
        "</xs:defaultOpenContent>"
        "<xs:complexType name='ct'><xs:sequence/></xs:complexType>"
        "<xs:element name='r' type='t:ct'/>"
        "</xs:schema>",
    )
    assert error_codes(_compile(tmp_path).report) == set()


def test_nameless_key_and_keyref_skip_the_clark_index(schema_report):
    report = schema_report(
        "<xs:element name='r'><xs:complexType><xs:sequence>"
        "<xs:element name='c' type='xs:string'/></xs:sequence></xs:complexType>"
        "<xs:key><xs:selector xpath='.'/><xs:field xpath='c'/></xs:key>"
        "<xs:keyref><xs:selector xpath='.'/><xs:field xpath='c'/></xs:keyref>"
        "</xs:element>"
    )
    assert {"declaration-name", "declaration-attribute"} <= error_codes(report)


def test_constraint_reference_sites_report_bad_namespaces_and_empty_refs(schema_report):
    report = schema_report(
        "<xs:element name='r'><xs:complexType><xs:sequence>"
        "<xs:element name='c' type='xs:string'/></xs:sequence></xs:complexType>"
        "<xs:key name='k'><xs:selector xpath='.'/><xs:field xpath='c'/></xs:key>"
        "<xs:key ref='k' xpathDefaultNamespace='##bogus'/>"
        "<xs:unique ref=''/>"
        "</xs:element>"
    )
    codes = error_codes(report)
    assert "xpath-invalid" in codes
    assert "identity-refer" in codes
    assert any("the reference is empty" in issue.message for issue in report.errors)


# ---------------------------------------------------------------------------
# Pipeline: particle/substitution/alternative checks
# ---------------------------------------------------------------------------


def test_collect_particles_skips_an_unresolved_group_reference(schema_report):
    report = schema_report(
        "<xs:complexType name='t'><xs:sequence><xs:group ref='bogus'/>"
        "<xs:element name='a' type='xs:string'/></xs:sequence></xs:complexType>"
    )
    assert "unknown-group" in error_codes(report)


def test_group_ref_sites_skips_unresolvable_references(schema_report):
    report = schema_report(
        "<xs:group name='g'><xs:sequence/></xs:group>"
        "<xs:group name='h'><xs:sequence><xs:element name='z' type='xs:string'/></xs:sequence></xs:group>"
        "<xs:complexType name='t'><xs:sequence>"
        "<xs:group ref='g' minOccurs='0'/>"
        "<xs:group ref='h' minOccurs='0'/>"
        "<xs:group ref='bogus:g' minOccurs='0'/>"
        "</xs:sequence></xs:complexType>"
    )
    assert "pointless-particle" in error_codes(report)


def test_substitution_edc_transitive_cycle_is_not_reported_as_conflicting(schema_report):
    report = schema_report(
        "<xs:element name='a' type='xs:string' substitutionGroup='a'/>"
        "<xs:complexType name='t' elementFormDefault='qualified'><xs:sequence>"
        "<xs:element name='a' type='xs:string'/></xs:sequence></xs:complexType>"
    )
    codes = error_codes(report)
    assert "circular-substitution-group" in codes
    assert "element-consistent" not in codes


def test_substitution_edc_skips_an_undeclared_member_type(schema_report):
    report = schema_report(
        "<xs:element name='m' type='NoSuchType' substitutionGroup='h'/>"
        "<xs:element name='h' type='xs:string'/>"
        "<xs:complexType name='t' elementFormDefault='qualified'><xs:sequence>"
        "<xs:element name='h' type='xs:string'/>"
        "<xs:element name='m' type='xs:int'/>"
        "</xs:sequence></xs:complexType>"
    )
    assert "element-consistent" not in error_codes(report)


def test_substitution_edc_reports_a_member_once(schema_report):
    report = schema_report(
        "<xs:element name='m' type='xs:string' substitutionGroup='h'/>"
        "<xs:element name='h' type='xs:string'/>"
        "<xs:complexType name='t' elementFormDefault='qualified'><xs:sequence>"
        "<xs:element name='h' type='xs:string'/>"
        "<xs:element name='m' type='xs:int'/>"
        "<xs:element name='m' type='xs:int'/>"
        "</xs:sequence></xs:complexType>"
    )
    conflicting = [issue for issue in report.errors if issue.code == "element-consistent"]
    assert len(conflicting) == 1
    assert "substitution member" in conflicting[0].message


def test_particle_restriction_skips_an_uncompilable_derived_model(schema_report, caplog):
    with caplog.at_level(logging.DEBUG, logger="pyxsd.schema_checks"):
        report = schema_report(
            "<xs:complexType name='b'><xs:sequence>"
            "<xs:element name='a' type='xs:string'/></xs:sequence></xs:complexType>"
            "<xs:complexType name='d'><xs:complexContent><xs:restriction base='b'>"
            "<xs:group ref='bogus'/>"
            "</xs:restriction></xs:complexContent></xs:complexType>"
        )
    assert "particle-restriction" not in error_codes(report)
    assert any("could not be compiled; skipped" in message for message in caplog.messages)


def test_mixed_restriction_skips_anytype_and_unresolved_bases(schema_report, caplog):
    with caplog.at_level(logging.DEBUG, logger="pyxsd.schema_checks"):
        report = schema_report(
            "<xs:complexType name='t1' mixed='true'><xs:complexContent>"
            "<xs:restriction base='xs:anyType'><xs:sequence/></xs:restriction>"
            "</xs:complexContent></xs:complexType>"
            "<xs:complexType name='t2' mixed='true'><xs:complexContent>"
            "<xs:restriction base='Nope'><xs:sequence/></xs:restriction>"
            "</xs:complexContent></xs:complexType>"
        )
    assert "particle-restriction" not in error_codes(report)
    assert any("mixed restriction: base type" in message for message in caplog.messages)


def test_complex_content_from_simple_base_without_complex_content(schema_report):
    report = schema_report(
        "<xs:complexType name='b'><xs:sequence>"
        "<xs:element name='a' type='xs:string'/></xs:sequence></xs:complexType>"
        "<xs:complexType name='t'><xs:extension base='b'><xs:sequence>"
        "<xs:element name='c' type='xs:string'/></xs:sequence></xs:extension></xs:complexType>"
    )
    assert "declaration-child" in error_codes(report)
    assert not any("adopts explicit complex content" in issue.message for issue in report.errors)


def test_attribute_use_restriction_with_a_new_attribute(schema_report):
    report = schema_report(
        "<xs:complexType name='b'><xs:attribute name='a' type='xs:string'/></xs:complexType>"
        "<xs:complexType name='d'><xs:complexContent><xs:restriction base='b'>"
        "<xs:attribute name='a' type='xs:string'/><xs:attribute name='c' type='xs:string'/>"
        "</xs:restriction></xs:complexContent></xs:complexType>"
    )
    assert not any("attribute-use restriction" in issue.message for issue in report.errors)


def test_restricted_attribute_uses_skip_redeclared_uses(schema_report):
    report = schema_report(
        "<xs:complexType name='b'><xs:sequence/>"
        "<xs:attribute name='a' type='xs:string'/>"
        "<xs:anyAttribute namespace='urn:hidden'/>"
        "</xs:complexType>"
        "<xs:complexType name='d'><xs:complexContent><xs:restriction base='b'>"
        "<xs:sequence/><xs:attribute name='a' type='xs:string'/>"
        "<xs:anyAttribute namespace='urn:hidden'/>"
        "</xs:restriction></xs:complexContent></xs:complexType>"
    )
    assert not any(
        "is not admitted by the attribute wildcard" in issue.message for issue in report.errors
    )


def test_conditional_type_substitution_compiles_an_inline_derived_model(schema_report):
    report = schema_report(
        "<xs:complexType name='b'><xs:sequence>"
        "<xs:element name='a' type='xs:string'/></xs:sequence></xs:complexType>"
        "<xs:element name='r'><xs:complexType><xs:complexContent>"
        "<xs:restriction base='b'><xs:sequence>"
        "<xs:element name='a' type='xs:string'/></xs:sequence>"
        "</xs:restriction></xs:complexContent></xs:complexType></xs:element>"
    )
    assert "particle-restriction" not in error_codes(report)


def test_alternative_substitutability_reports_different_test_tables(schema_report):
    report = schema_report(
        "<xs:complexType name='b'><xs:sequence>"
        "<xs:element name='a' type='xs:string'>"
        "<xs:alternative test='@v = 1' type='xs:int'/></xs:element>"
        "</xs:sequence></xs:complexType>"
        "<xs:complexType name='d'><xs:complexContent><xs:restriction base='b'><xs:sequence>"
        "<xs:element name='a' type='xs:string'>"
        "<xs:alternative test='@w = 1' type='xs:int'/></xs:element>"
        "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
    )
    issues = [issue for issue in report.errors if "cos-cta-substitutable" in issue.message]
    assert len(issues) == 1
    assert "the type tables carry different tests" in issues[0].message


def test_alternative_substitutability_skips_unresolvable_alternatives(schema_report):
    report = schema_report(
        "<xs:complexType name='b'><xs:sequence>"
        "<xs:element name='a' type='xs:string'>"
        "<xs:alternative test='@v = 1' type='xs:int'/></xs:element>"
        "</xs:sequence></xs:complexType>"
        "<xs:complexType name='d'><xs:complexContent><xs:restriction base='b'><xs:sequence>"
        "<xs:element name='a' type='xs:string'>"
        "<xs:alternative test='@v = 1' type='NoSuchType'/></xs:element>"
        "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
    )
    assert "alternative-invalid" in error_codes(report)
    assert not any("cos-cta-substitutable" in issue.message for issue in report.errors)


def test_substitution_group_cycle_walk_revisits_a_head(schema_report):
    report = schema_report(
        "<xs:element name='d' type='xs:string'/>"
        "<xs:element name='b' type='xs:string' substitutionGroup='d'/>"
        "<xs:element name='c' type='xs:string' substitutionGroup='d'/>"
        "<xs:element name='a' type='xs:string' substitutionGroup='b c'/>"
    )
    assert "circular-substitution-group" not in error_codes(report)


def test_substitution_group_exclusions_skip_an_unresolved_head_type(schema_report):
    report = schema_report(
        "<xs:element name='h' type='NoSuchType'/>"
        "<xs:element name='m' type='xs:string' substitutionGroup='h'/>"
    )
    assert "substitution-type" not in error_codes(report)


# ---------------------------------------------------------------------------
# Pipeline: extension structure / wildcard overlap
# ---------------------------------------------------------------------------


def test_extension_all_overlap_ignores_non_overlapping_wildcards(schema_report):
    report = schema_report(
        "<xs:complexType name='b'><xs:all>"
        "<xs:any namespace='urn:a' processContents='lax'/></xs:all></xs:complexType>"
        "<xs:complexType name='d'><xs:complexContent><xs:extension base='b'>"
        "<xs:all><xs:any namespace='urn:b' processContents='lax'/></xs:all>"
        "</xs:extension></xs:complexContent></xs:complexType>"
    )
    assert "particle-restriction" not in error_codes(report)


def test_wildcard_particle_overlap_continues_past_an_unskippable_gap(schema_report):
    report = schema_report(
        "<xs:complexType name='t'><xs:sequence>"
        "<xs:any namespace='##any' minOccurs='0'/>"
        "<xs:element name='a' type='xs:string'/>"
        "<xs:any namespace='##any'/>"
        "</xs:sequence></xs:complexType>"
    )
    assert not any(
        "two wildcards with overlapping namespace constraints" in issue.message
        for issue in report.errors
    )


def test_wildcard_particle_overlap_skips_a_malformed_second_constraint(schema_report):
    report = schema_report(
        "<xs:complexType name='t'><xs:choice>"
        "<xs:any namespace='##any'/><xs:any namespace='##bogus'/>"
        "</xs:choice></xs:complexType>"
    )
    codes = error_codes(report)
    assert "wildcard-invalid" in codes
    assert not any(
        "two wildcards with overlapping namespace constraints" in issue.message
        for issue in report.errors
    )


# ---------------------------------------------------------------------------
# Pipeline: type references, ids, namespaced attributes, form defaults
# ---------------------------------------------------------------------------


def test_check_type_references_skips_composed_unqualified_refs(tmp_path):
    _write(
        tmp_path,
        "side.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
        "<xs:element name='e' type='Ghost'/>"
        "</xs:schema>",
    )
    _write(
        tmp_path,
        "main.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' targetNamespace='urn:a'>"
        "<xs:include schemaLocation='side.xsd'/>"
        "<xs:element name='r' type='xs:string'/>"
        "</xs:schema>",
    )
    assert "unknown-type" not in error_codes(_compile(tmp_path).report)


def test_check_declaration_id_scopes_uniqueness_to_the_main_document(tmp_path):
    _write(
        tmp_path,
        "side.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' targetNamespace='urn:a'>"
        "<xs:complexType name='c' id='dup'><xs:sequence/></xs:complexType>"
        "</xs:schema>",
    )
    _write(
        tmp_path,
        "main.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' targetNamespace='urn:a'>"
        "<xs:include schemaLocation='side.xsd'/>"
        "<xs:complexType name='c2' id='dup'><xs:sequence/></xs:complexType>"
        "<xs:complexType name='c3' id='dup'><xs:sequence/></xs:complexType>"
        "<xs:element name='r' type='xs:string'/>"
        "</xs:schema>",
    )
    duplicates = [
        issue for issue in _compile(tmp_path).report.errors if issue.code == "declaration-duplicate"
    ]
    assert len(duplicates) == 1
    assert "duplicate id 'dup'" in duplicates[0].message


def test_check_schema_namespaced_attributes_ignores_foreign_elements(schema_report):
    report = schema_report(
        "<xs:element name='r' type='xs:string'/><foreign:extra xmlns:foreign='urn:foreign'/>"
    )
    assert "declaration-child" in error_codes(report)
    assert not any(issue.code == "unexpected-attribute" for issue in report.errors)


def test_check_form_defaults_reports_an_unknown_value(tmp_path):
    _write(
        tmp_path,
        "main.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' elementFormDefault='Bogus'>"
        "<xs:element name='r' type='xs:string'/>"
        "</xs:schema>",
    )
    issues = [
        issue
        for issue in _compile(tmp_path).report.errors
        if issue.code == "declaration-attribute" and "elementFormDefault" in issue.message
    ]
    assert len(issues) == 1
    assert "'Bogus'" in issues[0].message


def test_check_substitution_edc_without_a_schema():
    report = ValidationReport()
    resolved = [(("urn:t", "a"), "xs:string", _schema_less_particle())]
    check_substitution_edc(SimpleNamespace(report=report), resolved, None)
    assert report.errors == []


def test_included_default_open_content_resolves_notqname_prefixes(tmp_path):
    _write(
        tmp_path,
        "main.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' targetNamespace='urn:a' "
        "xmlns:a='urn:a' elementFormDefault='qualified'>"
        "<xs:include schemaLocation='side.xsd'/>"
        "<xs:element name='r' type='a:ct'/>"
        "</xs:schema>",
    )
    _write(
        tmp_path,
        "side.xsd",
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema' targetNamespace='urn:a' "
        "xmlns:a='urn:a'>"
        "<xs:defaultOpenContent mode='interleave' appliesToEmpty='true'>"
        "<xs:any namespace='##any' notQName='a:foo' processContents='lax'/>"
        "</xs:defaultOpenContent>"
        "<xs:complexType name='ct'><xs:sequence/></xs:complexType>"
        "</xs:schema>",
    )
    schema = _compile(tmp_path)
    excluded = schema.parse(StringIO("<a:r xmlns:a='urn:a'><a:foo/></a:r>"))
    assert "unexpected-element" in {issue.code for issue in excluded.report.errors}
    admitted = schema.parse(StringIO("<a:r xmlns:a='urn:a'><a:bar/></a:r>"))
    assert admitted.report.errors == []


def test_conditional_type_substitution_compiles_a_missing_component_model(schema_report):
    report = schema_report(
        "<xs:complexType name='b'><xs:sequence>"
        "<xs:element name='a' type='xs:string'/></xs:sequence></xs:complexType>"
        "<xs:complexType name='d'><xs:complexContent><xs:restriction base='b'>"
        "<xs:group ref='bogus'/>"
        "</xs:restriction></xs:complexContent></xs:complexType>"
    )
    assert "unknown-group" in error_codes(report)
    assert "particle-restriction" not in error_codes(report)


def test_alternative_substitutability_skips_error_alternatives(schema_report):
    report = schema_report(
        "<xs:complexType name='b'><xs:sequence>"
        "<xs:element name='a' type='xs:string'>"
        "<xs:alternative test='@v = 1' type='xs:error'/></xs:element>"
        "</xs:sequence></xs:complexType>"
        "<xs:complexType name='d'><xs:complexContent><xs:restriction base='b'><xs:sequence>"
        "<xs:element name='a' type='xs:string'>"
        "<xs:alternative test='@v = 1' type='xs:error'/></xs:element>"
        "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
    )
    assert report.errors == []
