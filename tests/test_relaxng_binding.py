"""Generated RELAX NG projection classes and their occurrence-aware views.

Projection classes come only from the compile-time route plan.  Descriptors are
live views over the one lexical tree, typed values come from the existing
typed-evidence snapshot, and generic routes keep the complete generic XML
interface without taking stable parents or siblings with them.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from dataclasses import replace
from xml.etree import ElementTree as ET

import pytest

from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng import AlternativeValue, StaleInterpretationError
from pyxsd.relaxng.binding import (
    WILDCARD_CHILDREN,
    AttributeDescriptor,
    ChildDescriptor,
    ChildSequence,
    ProjectionClassFactory,
    WildcardChildren,
    bind_projection,
    projection_class_factory,
)
from pyxsd.relaxng.datatypes import DatatypeContext
from pyxsd.relaxng.evidence import EvidenceGraph, EvidenceOccurrence, build_binding_evidence
from pyxsd.relaxng.projection import ProjectionPlan
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.schema_base import SchemaBase

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


def data(type_name: str, library: str = XSD) -> str:
    return f'<data type="{type_name}" datatypeLibrary="{library}"/>'


def element(name: str, content: str = "<empty/>") -> str:
    return f'<element name="{name}">{content}</element>'


def compile_body(body: str, **kwargs: object) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="doc">{body}</element>'), **kwargs
    )
    schema.require_valid()
    return schema


def typed(schema: RelaxNGSchema, xml: str, **kwargs: object):
    document = schema.parse(io.StringIO(xml), typed=True, **kwargs)  # type: ignore[arg-type]
    assert document.is_valid
    assert document.binding_evidence is not None
    return document


def root_route(schema: RelaxNGSchema):
    return schema.projection.routes[schema.projection.roots[0]]


def test_class_for_is_cached_and_stable_across_repeated_parses():
    schema = compile_body(element("a", "<text/>"))
    route = root_route(schema)
    factory = schema.projection_classes

    generated = factory.class_for(route.route_id)

    assert factory.class_for(route.route_id) is generated
    assert generated.__module__ == "pyxsd.relaxng.generated"
    assert generated.__qualname__ == "Doc"
    assert generated.__slots__ == ()
    assert issubclass(generated, RelaxNGElement)

    first = typed(schema, "<doc><a>one</a></doc>")
    second = typed(schema, "<doc><a>two</a></doc>")

    assert type(first.root) is type(second.root) is generated
    child_field = route.children["a"]
    child_class = factory.class_for(child_field.child_route_id)
    assert type(first.root.a) is type(second.root.a) is child_class


def test_projection_class_factory_cache_identity_is_plan_scoped():
    schema = compile_body(element("a", "<text/>"))
    plan = schema.projection

    assert projection_class_factory(plan) is projection_class_factory(plan)
    assert projection_class_factory(plan) is schema.projection_classes

    equivalent = compile_body(element("a", "<text/>"))
    assert equivalent.projection.grammar_fingerprint == plan.grammar_fingerprint
    assert equivalent.projection.roots == plan.roots
    assert equivalent.projection_classes is not schema.projection_classes

    route = root_route(schema)
    other_route = equivalent.projection.routes[equivalent.projection.roots[0]]
    left = schema.projection_classes.class_for(route.route_id)
    right = equivalent.projection_classes.class_for(other_route.route_id)
    assert left is not right
    assert (left.__module__, left.__qualname__) == (right.__module__, right.__qualname__)


def test_class_names_are_deterministic_identifiers_for_every_route():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f"""<choice xmlns="{NS}">
            <element name="class"><empty/></element>
            <element name="a-b"><empty/></element>
            <element name="_private"><empty/></element>
            <element><anyName><except><name>class</name></except></anyName><empty/></element>
            <element name="a" ns="urn:one"><empty/></element>
            <element name="a" ns="urn:two"><empty/></element>
            </choice>"""
        )
    )
    schema.require_valid()
    plan = schema.projection
    factory = ProjectionClassFactory(plan)
    classes = [factory.class_for(route_id) for route_id in plan.roots]

    assert len(classes) == len(plan.roots)
    assert len({cls.__qualname__ for cls in classes}) == len(classes)
    assert all(cls.__qualname__.isidentifier() for cls in classes)
    assert all(cls.__module__ == "pyxsd.relaxng.generated" for cls in classes)
    anonymous = [
        cls.__qualname__
        for cls in classes
        if plan.routes[cls._relaxng_route_id_].expanded_name is None
    ]
    assert len(anonymous) == 1
    assert anonymous[0].startswith("Element_")
    a_names = {cls.__qualname__ for cls in classes if cls.__qualname__.startswith("A")}
    assert len(a_names) == 3


def test_class_name_collisions_and_equivalent_plans_are_deterministic():
    body = (
        '<element name="a" ns="urn:one"><empty/></element>'
        '<element name="a" ns="urn:two"><empty/></element>'
    )
    first = RelaxNGSchema.compile(io.StringIO(f'<choice xmlns="{NS}">{body}</choice>'))
    second = RelaxNGSchema.compile(io.StringIO(f'<choice xmlns="{NS}">{body}</choice>'))
    first.require_valid()
    second.require_valid()
    names_first = [
        first.projection_classes.class_for(route_id).__qualname__
        for route_id in first.projection.roots
    ]
    names_second = [
        second.projection_classes.class_for(route_id).__qualname__
        for route_id in second.projection.roots
    ]
    assert names_first == names_second
    assert len(set(names_first)) == len(names_first)


def test_hash_seed_does_not_change_generated_class_identity():
    script = f"""import io
from pyxsd.relaxng.schema import RelaxNGSchema
s = RelaxNGSchema.compile(io.StringIO('''<choice xmlns="{NS}">
<element name="class"><empty/></element>
<element name="a-b"><empty/></element>
<element><anyName><except><name>class</name></except></anyName><empty/></element>
</choice>'''))
print([
    (s.projection_classes.class_for(route_id).__module__,
     s.projection_classes.class_for(route_id).__qualname__)
    for route_id in s.projection.roots
])
"""
    results = [
        subprocess.check_output(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
        )
        for seed in ("1", "981")
    ]
    assert results[0] == results[1]


def test_recursive_routes_reuse_the_same_class_at_each_instance_depth():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<grammar xmlns="{NS}"><start><ref name="node"/></start>'
            '<define name="node"><element name="node"><zeroOrMore>'
            '<ref name="node"/></zeroOrMore></element></define></grammar>'
        )
    )
    schema.require_valid()
    route = root_route(schema)
    factory = schema.projection_classes
    generated = factory.class_for(route.route_id)

    shallow = typed(schema, "<node/>")
    deep = typed(schema, "<node><node><node/></node></node>")

    def classes(root: RelaxNGElement) -> set[type[RelaxNGElement]]:
        found = set()
        pending = [root]
        while pending:
            node = pending.pop()
            found.add(type(node))
            pending.extend(node.children)
        return found

    assert classes(shallow.root) == {generated}
    assert classes(deep.root) == {generated}
    assert route.children["node"].child_route_id == route.route_id


def test_attribute_descriptors_reuse_typed_values_and_schema_ambiguity():
    schema = compile_body(
        '<attribute name="count">' + data("integer") + "</attribute>"
        '<attribute name="label"><text/></attribute>'
        '<attribute name="tags"><list><oneOrMore>'
        + data("token")
        + "</oneOrMore></list></attribute>"
        '<optional><attribute name="maybe">' + data("integer") + "</attribute></optional>"
    )
    document = typed(schema, '<doc count="7" label="hi" tags="a b"/>')
    root = document.root

    assert isinstance(type(root).count, AttributeDescriptor)
    assert type(root).count.expanded_name == "count"
    assert root.count == 7
    assert isinstance(root.label, AlternativeValue)
    assert {candidate.contract.kind for candidate in root.label.candidates} == {"text"}
    assert str(root.label) == "hi"
    assert root.tags == ["a", "b"]
    assert root.maybe is None


def test_attribute_scalar_alternatives_wrap_for_every_instance_branch():
    schema = compile_body(
        '<attribute name="value"><choice>'
        + data("integer")
        + data("string")
        + "</choice></attribute>"
    )
    integer_document = typed(schema, '<doc value="12"/>')
    string_document = typed(schema, '<doc value="words"/>')

    assert type(integer_document.root) is type(string_document.root)
    integer_value = integer_document.root.value
    string_value = string_document.root.value
    assert isinstance(integer_value, AlternativeValue)
    assert isinstance(string_value, AlternativeValue)
    assert integer_value.as_type("xsd:integer") == 12
    assert string_value.as_type("xsd:string") == "words"


def test_child_descriptors_match_cardinality_and_return_lexical_nodes():
    schema = compile_body(
        "<optional>" + element("single", "<text/>") + "</optional>"
        "<zeroOrMore>"
        + element("many", "<text/>")
        + "</zeroOrMore>"
        + element("req", "<empty/>")
        + "<optional><element name='extra'><empty/></element></optional>"
    )
    document = typed(schema, "<doc><many>1</many><many>2</many><req/></doc>")
    root = document.root
    route = root_route(schema)

    assert isinstance(type(root).many, ChildDescriptor)
    assert isinstance(type(root).single, ChildDescriptor)
    assert type(root).many.expanded_name == "many"
    assert root.single is None
    assert root.extra is None
    assert isinstance(root.many, ChildSequence)
    assert root.many.expanded_name == "many"
    assert repr(root.many) == "ChildSequence(expanded_name='many', count=2)"
    assert len(root.many) == 2
    assert [child.text_value() for child in root.many] == ["1", "2"]
    assert root.many[0] is root.children[0]
    assert root.many[1] is root.children[1]
    assert root.many[-1] is root.children[1]
    assert root.many[0:2] == (root.children[0], root.children[1])
    assert root.req is root.children[2]
    assert type(root.req) is schema.projection_classes.class_for(
        route.children["req"].child_route_id
    )


def test_repeatable_descriptor_is_a_live_view_of_the_authoritative_children():
    schema = compile_body("<zeroOrMore>" + element("many", "<text/>") + "</zeroOrMore>")
    document = typed(schema, "<doc><many>1</many></doc>")
    root = document.root

    view = root.many
    added = RelaxNGElement._new("many", root.revision, text="2")
    root.append_child(added)

    assert len(view) == 2
    assert view[-1] is added
    assert list(view) == [root.children[0], added]
    assert root.many is not view


def test_repeated_same_name_children_keep_order_and_merged_contracts():
    schema = compile_body(
        '<element name="a"><attribute name="x">' + data("integer") + "</attribute></element>"
        '<element name="a"><attribute name="y">' + data("integer") + "</attribute></element>"
    )
    document = typed(schema, '<doc><a x="1"/><a y="2"/></doc>')
    root = document.root
    field = root_route(schema).children["a"]

    assert field.cardinality.minimum == 2
    assert field.cardinality.maximum == 2
    assert field.cardinality.repeatable
    assert isinstance(root.a, ChildSequence)
    assert len(root.a) == 2
    assert [node.attribute_lexical("x") for node in root.a] == ["1", None]
    assert [node.attribute_lexical("y") for node in root.a] == [None, "2"]
    assert type(root.a[0]) is type(root.a[1])
    assert root.a[0].x == 1
    assert root.a[0].y is None
    assert root.a[1].x is None
    assert root.a[1].y == 2
    assert root.a[0] is root.children[0]
    assert root.a[1] is root.children[1]


def test_optional_choice_descriptors_do_not_follow_the_instance_branch():
    schema = compile_body(
        "<choice>" + element("a", "<empty/>") + element("b", "<empty/>") + "</choice>"
    )
    plan = schema.projection
    route = plan.routes[plan.roots[0]]

    first = typed(schema, "<doc><a/></doc>")
    second = typed(schema, "<doc><b/></doc>")

    assert type(first.root) is type(second.root)
    assert type(first.root)._relaxng_route_id_ == route.route_id
    assert first.root.a is not None
    assert first.root.b is None
    assert second.root.a is None
    assert second.root.b is not None


def test_scalar_descriptor_uses_the_generated_class_and_static_wrapper():
    schema = compile_body("<choice>" + data("integer") + data("string") + "</choice>")
    integer_document = typed(schema, "<doc>12</doc>")
    string_document = typed(schema, "<doc>words</doc>")
    root = integer_document.root

    assert isinstance(root, RelaxNGElement)
    assert type(root) is type(string_document.root)
    assert issubclass(type(root), RelaxNGElement)
    assert isinstance(root.scalar_value(), AlternativeValue)
    assert isinstance(string_document.root.scalar_value(), AlternativeValue)
    assert root.scalar_value().as_type("xsd:integer") == 12
    assert string_document.root.scalar_value().as_type("xsd:string") == "words"


def test_list_scalar_descriptor_returns_plain_lists_or_wrappers():
    schema = compile_body(
        '<attribute name="tags"><list><oneOrMore>'
        + data("token")
        + "</oneOrMore></list></attribute>"
        '<element name="values"><list><oneOrMore>'
        + data("integer")
        + "</oneOrMore></list></element>"
    )
    document = typed(schema, '<doc tags="a b"><values>1 2</values></doc>')
    root = document.root

    assert root.tags == ["a", "b"]
    assert root.values.scalar_value() == [1, 2]


def test_mixed_scalar_and_list_site_wraps_both_candidates():
    schema = compile_body(
        '<attribute name="a"><choice><list><oneOrMore>'
        + data("token")
        + "</oneOrMore></list><text/></choice></attribute>"
    )
    document = typed(schema, '<doc a="x y"/>')

    value = document.root.a
    assert isinstance(value, AlternativeValue)
    assert {candidate.contract.kind for candidate in value.candidates} == {"list", "text"}


def test_generic_descendant_preserves_stable_parent_and_sibling_accessors():
    schema = compile_body(
        '<element name="bad"><choice>'
        + data("integer")
        + element("nested", "<empty/>")
        + "</choice></element>"
        + element("good", "<text/>")
    )
    plan = schema.projection
    route = plan.routes[plan.roots[0]]
    bad_route = plan.routes[route.children["bad"].child_route_id]
    good_field = route.children["good"]
    nested_route = plan.routes[bad_route.children["nested"].child_route_id]
    assert bad_route.generic_reason is not None

    document = typed(schema, "<doc><bad><nested/></bad><good>hi</good></doc>")
    root = document.root
    factory = schema.projection_classes

    assert type(root) is factory.class_for(route.route_id)
    assert type(root.bad) is RelaxNGElement
    assert type(root.good) is factory.class_for(good_field.child_route_id)
    assert type(root.bad.children[0]) is factory.class_for(nested_route.route_id)
    assert root.bad.parent is root
    assert root.good.parent is root
    assert root.bad.children[0].parent is root.bad
    assert root.bad.to_etree().find("nested") is not None
    assert isinstance(root.good.scalar_value(), AlternativeValue)
    assert not hasattr(root.bad, "nested")


def test_expanded_name_lookup_and_wildcard_view_are_ordered_and_alias_free():
    schema = compile_body(
        element("known", "<empty/>")
        + "<zeroOrMore><element><anyName><except><name>known</name></except></anyName>"
        '<attribute name="kind"><text/></attribute></element></zeroOrMore>'
    )
    document = typed(schema, '<doc><known/><other kind="x"/><third kind="y"/></doc>')
    root = document.root
    route = root_route(schema)
    (wildcard,) = route.wildcards
    wildcard_class = schema.projection_classes.class_for(wildcard.child_route_id)
    generated = type(root)

    assert isinstance(root["known"], ChildSequence)
    assert len(root["known"]) == 1
    assert root["known"][0] is root.children[0]
    assert root["other"][0] is root.children[1]
    assert list(root["missing"]) == []
    assert isinstance(root[WILDCARD_CHILDREN], WildcardChildren)
    assert [node._name_ for node in root[WILDCARD_CHILDREN]] == ["other", "third"]
    assert list(root[WILDCARD_CHILDREN]) == [root.children[1], root.children[2]]
    assert root[WILDCARD_CHILDREN][0] is root.children[1]
    assert root[WILDCARD_CHILDREN][0:1] == (root.children[1],)
    assert repr(root[WILDCARD_CHILDREN]) == "WildcardChildren(count=2)"
    assert type(root.children[1]) is wildcard_class
    assert "known" in vars(generated)
    assert "other" not in vars(generated)
    assert "third" not in vars(generated)
    assert not hasattr(root, "other")
    assert not hasattr(root, "kind")


def test_wildcard_attribute_names_never_become_fixed_descriptors():
    schema = compile_body("<zeroOrMore><attribute><anyName/></attribute></zeroOrMore>")
    document = typed(schema, '<doc anything="v" other="w"/>')
    root = document.root

    assert root.attributes == {"anything": "v", "other": "w"}
    assert not hasattr(root, "anything")
    assert not hasattr(root, "other")


def test_expanded_name_views_do_not_depend_on_escaped_python_aliases():
    schema = compile_body(element("children", "<text/>") + element("attributes", "<empty/>"))
    document = typed(schema, "<doc><children>one</children><attributes/></doc>")
    root = document.root

    assert root.children_2 is root.children[0]
    assert root.attributes_2 is root.children[1]
    assert root["children"][0] is root.children_2
    assert root["attributes"][0] is root.attributes_2
    assert root.children[0] is root["children"][0]
    assert root.attributes == {}


def test_expanded_name_lookup_uses_exact_namespaces_and_wildcard_classes():
    schema = compile_body(
        '<element name="item" ns="urn:one"><text/></element>'
        "<zeroOrMore><element><nsName ns='urn:two'/><text/></element></zeroOrMore>"
    )
    document = typed(
        schema,
        '<doc xmlns:p="urn:one" xmlns:q="urn:two"><p:item>one</p:item><q:other>two</q:other></doc>',
    )
    root = document.root

    assert len(root["{urn:one}item"]) == 1
    assert root["{urn:one}item"][0] is root.item
    assert [node._name_ for node in root[WILDCARD_CHILDREN]] == ["{urn:two}other"]
    assert list(root["{urn:two}other"]) == list(root[WILDCARD_CHILDREN])
    assert list(root["item"]) == []


def test_empty_wildcard_view_and_invalid_lookup_key_are_explicit():
    schema = compile_body(element("known", "<empty/>"))
    document = typed(schema, "<doc><known/></doc>")

    view = document.root[WILDCARD_CHILDREN]

    assert repr(WILDCARD_CHILDREN) == "WILDCARD_CHILDREN"
    assert isinstance(view, WildcardChildren)
    assert len(view) == 0
    assert list(view) == []
    with pytest.raises(TypeError, match="expanded name"):
        document.root[0]  # type: ignore[index]


def test_invalid_typed_and_validation_only_documents_stay_generic():
    schema = compile_body(element("a", "<text/>"))

    invalid = schema.parse(io.StringIO("<doc><wrong/></doc>"), typed=True)
    assert not invalid.is_valid
    assert invalid.binding_evidence is None
    assert type(invalid.root) is RelaxNGElement
    assert type(invalid.root.children[0]) is RelaxNGElement

    validation = schema.parse(io.StringIO("<doc><a>x</a></doc>"), typed=False)
    assert validation.is_valid
    assert validation.binding_evidence is None
    assert type(validation.root) is RelaxNGElement
    assert type(validation.root.children[0]) is RelaxNGElement
    with pytest.raises(PyXSDError):
        validation.root.scalar_value()


def test_capture_off_and_on_share_classes_and_typed_values():
    schema = compile_body(
        '<attribute name="count">'
        + data("integer")
        + "</attribute>"
        + element("item", data("integer"))
    )
    plain = typed(schema, '<doc count="1"><item>2</item></doc>')
    captured = typed(schema, '<doc count="1"><item>2</item></doc>', capture="patterns")

    assert type(plain.root) is type(captured.root)
    assert type(plain.root.item) is type(captured.root.item)
    assert plain.root.count == captured.root.count == 1
    assert plain.root.item.scalar_value() == captured.root.item.scalar_value() == 2
    assert captured.annotations is not None


def test_stale_typed_reads_raise_while_structural_views_stay_current():
    schema = compile_body(
        '<attribute name="count">' + data("integer") + "</attribute>"
        "<zeroOrMore>" + element("item", "<text/>") + "</zeroOrMore>"
    )
    document = typed(schema, '<doc count="1"><item>one</item></doc>')
    root = document.root

    root.item[0].set_text("changed")

    assert not document.interpretations_current
    with pytest.raises(StaleInterpretationError):
        _ = root.count
    with pytest.raises(StaleInterpretationError):
        root.item[0].scalar_value()
    assert root.item[0].text_value() == "changed"
    assert root.item[0] is root.children[0]
    assert len(root["item"]) == 1

    added = RelaxNGElement._new("item", root.revision, text="two")
    root.append_child(added)
    assert len(root.item) == 2
    assert root.item[1] is added


def test_binding_reuses_the_lexical_tree_and_never_inherits_schema_base():
    schema = compile_body(element("item", "<text/>"))
    document = typed(schema, "<doc><item>one</item></doc>")
    root = document.root

    assert root.item is root.children[0]
    assert root["item"][0] is root.item
    assert root.item.parent is root
    assert not hasattr(root, "__dict__")
    assert not isinstance(root, SchemaBase)
    assert not issubclass(type(root), SchemaBase)


def test_cloning_a_projection_tree_preserves_its_generated_classes():
    from pyxsd.relaxng.xml_writer import clone_relaxng

    schema = compile_body(element("item", "<text/>"))
    document = typed(schema, "<doc><item>one</item></doc>")
    root = document.root

    cloned = clone_relaxng(root)

    assert type(cloned) is type(root)
    assert type(cloned.children[0]) is type(root.children[0])
    assert cloned is not root
    assert cloned.children[0] is not root.children[0]


def test_bind_projection_accepts_hand_built_evidence_without_a_document():
    schema = compile_body(element("item", "<text/>"))
    root = RelaxNGElement.from_etree(ET.fromstring("<doc><item>one</item></doc>"))
    evidence = build_binding_evidence(
        root,
        schema.grammar.start,
        datatype_context=DatatypeContext(None, {}),
        limits=None,
    )

    bound = bind_projection(root, schema.projection, evidence)

    assert bound is root
    assert type(root) is schema.projection_classes.class_for(schema.projection.roots[0])
    assert type(root.item) is schema.projection_classes.class_for(
        root_route(schema).children["item"].child_route_id
    )
    with pytest.raises(PyXSDError):
        root.item.scalar_value()


def test_bind_projection_is_idempotent_and_rejects_foreign_evidence():
    schema = compile_body(element("item", "<text/>"))
    first = typed(schema, "<doc><item>one</item></doc>")
    second = typed(schema, "<doc><item>two</item></doc>")
    root = first.root
    generated = type(root)

    bound = bind_projection(root, schema.projection, first.binding_evidence)

    assert bound is root
    assert type(root) is generated
    with pytest.raises(ValueError, match="evidence"):
        bind_projection(root, schema.projection, second.binding_evidence)
    with pytest.raises(TypeError, match="RelaxNGElement"):
        bind_projection("root", schema.projection, first.binding_evidence)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ProjectionPlan"):
        bind_projection(root, object(), first.binding_evidence)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="EvidenceGraph"):
        bind_projection(root, schema.projection, object())  # type: ignore[arg-type]


def test_bind_projection_rejects_a_foreign_factory():
    schema = compile_body(element("item", "<text/>"))
    equivalent = compile_body(element("item", "<text/>"))
    document = typed(schema, "<doc><item>one</item></doc>")

    with pytest.raises(ValueError, match="factory"):
        bind_projection(
            document.root,
            schema.projection,
            document.binding_evidence,
            factory=equivalent.projection_classes,
        )


def test_bind_projection_requires_owned_typed_evidence():
    schema = compile_body(element("item", "<text/>"))
    validation = schema.parse(io.StringIO("<doc><item>one</item></doc>"), typed=False)
    typed_document = typed(schema, "<doc><item>one</item></doc>")

    with pytest.raises(ValueError, match="typed evidence"):
        bind_projection(validation.root, schema.projection, typed_document.binding_evidence)


def test_bind_projection_requires_one_document_site_and_one_revision():
    schema = compile_body(element("item", "<text/>"))
    document = typed(schema, "<doc><item>one</item></doc>")
    root = document.root

    with pytest.raises(ValueError, match="typed evidence"):
        bind_projection(
            root,
            schema.projection,
            EvidenceGraph((), (), (), ()),
        )

    foreign = RelaxNGElement.from_etree(ET.fromstring("<other/>"))
    mixed = EvidenceGraph(
        (),
        (),
        (),
        (
            EvidenceOccurrence(root, "document"),
            EvidenceOccurrence(foreign, "text"),
        ),
    )
    with pytest.raises(ValueError, match="lexical revision"):
        bind_projection(root, schema.projection, mixed)


def test_unmatched_or_ambiguous_roots_never_guess_a_route():
    schema = compile_body(element("item", "<text/>"))
    document = typed(schema, "<doc><item>one</item></doc>")
    root = document.root
    plan = schema.projection

    nothing = ProjectionPlan((), {}, plan.grammar_fingerprint)
    bind_projection(root, nothing, document.binding_evidence)
    assert type(root) is RelaxNGElement
    assert type(root.children[0]) is RelaxNGElement

    route = root_route(schema)
    duplicate = replace(route, route_id="duplicate")
    ambiguous = ProjectionPlan(
        (route.route_id, duplicate.route_id),
        {route.route_id: route, duplicate.route_id: duplicate},
        plan.grammar_fingerprint,
    )
    bind_projection(root, ambiguous, document.binding_evidence)
    assert type(root) is RelaxNGElement


def test_bind_projection_falls_back_when_a_child_view_has_no_route():
    schema = compile_body(element("item", "<text/>"))
    document = typed(schema, "<doc><item>one</item></doc>")
    root = document.root
    route = root_route(schema)
    stripped = replace(route, children={})
    plan = ProjectionPlan(
        (stripped.route_id,),
        {stripped.route_id: stripped},
        route.occurrence.grammar_fingerprint,
    )
    factory = ProjectionClassFactory(plan)

    bind_projection(root, plan, document.binding_evidence, factory=factory)

    assert type(root) is factory.class_for(route.route_id)
    assert type(root.children[0]) is RelaxNGElement


def test_bind_projection_falls_back_when_a_wildcard_name_does_not_match():
    other = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="doc"><zeroOrMore><element>'
            '<nsName ns="urn:w"/><empty/></element></zeroOrMore></element>'
        )
    )
    other.require_valid()
    source = compile_body("<zeroOrMore>" + element("plain", "<empty/>") + "</zeroOrMore>")
    document = typed(source, "<doc><plain/></doc>")
    root = document.root
    plan = other.projection
    route = plan.routes[plan.roots[0]]
    factory = ProjectionClassFactory(plan)

    bind_projection(root, plan, document.binding_evidence, factory=factory)

    assert type(root) is factory.class_for(route.route_id)
    assert type(root.children[0]) is RelaxNGElement


def test_projection_factory_rejects_non_plan_values_and_unknown_routes():
    with pytest.raises(TypeError, match="ProjectionPlan"):
        ProjectionClassFactory(object())  # type: ignore[arg-type]
    schema = compile_body(element("item", "<text/>"))
    factory = schema.projection_classes
    with pytest.raises(KeyError):
        factory.class_for("pyxsd.relaxng.route/1:missing")


def test_generic_routes_missing_from_a_plan_are_not_fabricated():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="bad"><choice>'
            + data("integer")
            + element("nested", "<empty/>")
            + "</choice></element>"
        )
    )
    schema.require_valid()
    route = root_route(schema)
    assert route.generic_reason is not None

    generated = schema.projection_classes.class_for(route.route_id)

    assert generated is RelaxNGElement
