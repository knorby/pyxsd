"""Schema-static projections: merging, finite routes, and lossless content views."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest

from pyxsd.relaxng.errors import RelaxNGLimitError
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.schema import RelaxNGSchema

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


def compile_body(body, *, limits=None):
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="doc">{body}</element>'),
        limits=limits,
    )
    schema.require_valid()
    return schema


def root(schema):
    return schema.projection.routes[schema.projection.roots[0]]


def child(schema, name):
    return schema.projection.routes[root(schema).children[name].child_route_id]


def test_equivalent_alternatives_have_one_public_contract_and_immutable_keys():
    schema = compile_body(
        "<choice><element name='a'><text/></element><element name='a'><text/></element></choice>"
    )
    plan = schema.projection
    route = root(schema)
    assert len(plan.routes) == 2
    assert route.generic_reason is None
    assert route.children["a"].cardinality.minimum == 1
    assert route.children["a"].cardinality.maximum == 1
    assert plan.route_for_occurrence(route.occurrence) == route.route_id
    assert plan.resolve_route(route.route_id) is route
    assert plan.resolve_route("doc") is route
    with pytest.raises(TypeError):
        plan.routes["new"] = route
    with pytest.raises(TypeError):
        route.children["new"] = route.children["a"]
    with pytest.raises(FrozenInstanceError):
        route.occurrence.assignment_ids = ()
    with pytest.raises(TypeError, match="ProjectionOccurrence"):
        plan.route_for_occurrence(object())
    with pytest.raises(KeyError):
        plan.resolve_route("absent")


def test_scalar_alternatives_merge_at_attribute_and_child_positions():
    body = (
        "<choice><attribute name='a'><data type='integer' datatypeLibrary='"
        + XSD
        + "'/></attribute><attribute name='a'><text/></attribute></choice>"
        "<choice><element name='b'><data type='integer' datatypeLibrary='"
        + XSD
        + "'/></element><element name='b'><text/></element></choice>"
    )
    schema = compile_body(body)
    route = root(schema)
    assert route.generic_reason is None
    assert {c.kind for c in route.attributes["a"].scalar_contracts} == {"text", "atomic"}
    assert {c.kind for c in route.children["b"].scalar_contracts} == {"text", "atomic"}
    assert len(child(schema, "b").content.scalar_contracts) == 2
    assert child(schema, "b").generic_reason is None


def test_exclusive_choice_keeps_optional_fields_and_the_choice_constraint():
    schema = compile_body(
        "<choice><element name='a'><empty/></element><element name='b'><empty/></element></choice>"
    )
    route = root(schema)
    assert set(route.children) == {"a", "b"}
    assert all(f.cardinality.minimum == 0 for f in route.children.values())
    assert all(f.cardinality.maximum == 1 for f in route.children.values())
    assert route.constraints[0].kind == "choice"
    assert {op.fields[0].expanded_name for op in route.constraints[0].operands} == {"a", "b"}


def test_interleave_preserves_named_access_and_original_order_contract():
    schema = compile_body(
        "<interleave><element name='a'><empty/></element>"
        "<element name='b'><empty/></element></interleave>"
    )
    route = root(schema)
    assert set(route.children) == {"a", "b"}
    assert route.content.ordered
    assert not route.content.mixed
    assert route.content.model.kind == "interleave"
    assert all(f.cardinality.minimum == 1 for f in route.children.values())


def test_recursive_routes_are_finite_and_independent_of_document_depth():
    schema = RelaxNGSchema.compile(
        io.StringIO(f"""<grammar xmlns="{NS}">
      <start><ref name="node"/></start>
      <define name="node"><element name="node"><zeroOrMore>
        <ref name="node"/></zeroOrMore></element></define></grammar>""")
    )
    schema.require_valid()
    route = root(schema)
    assert len(schema.projection.routes) == 1
    assert route.children["node"].child_route_id == route.route_id
    assert route.children["node"].cardinality.repeatable
    assert route.children["node"].cardinality.minimum == 0
    for xml in ("<node/>", "<node><node><node/></node></node>"):
        assert not schema.parse(io.StringIO(xml), typed=False).report.has_errors
        assert root(schema) is route


def test_repeated_same_name_assignments_merge_without_position_identity():
    schema = compile_body(
        "<element name='a'><attribute name='x'><text/></attribute></element>"
        "<element name='a'><attribute name='y'><text/></attribute></element>"
    )
    field = root(schema).children["a"]
    assert (field.cardinality.minimum, field.cardinality.maximum) == (2, 2)
    assert field.cardinality.repeatable
    merged = child(schema, "a")
    assert len(schema.projection.routes) == 2
    assert set(merged.attributes) == {"x", "y"}
    assert all(f.cardinality.minimum == 0 for f in merged.attributes.values())
    assert merged.generic_reason is None
    assert merged.constraints[0].kind == "choice"
    first, second = root(schema).content.model.operands
    assert first.assignment_id != second.assignment_id
    assert merged.assignments[first.assignment_id].content.fields[0].expanded_name == "x"
    assert merged.assignments[second.assignment_id].content.fields[0].expanded_name == "y"
    with pytest.raises(TypeError):
        merged.assignments[first.assignment_id] = merged.content.model


def test_named_children_coexist_with_an_ordered_expanded_name_wildcard():
    schema = compile_body(
        "<element name='known'><text/></element><zeroOrMore><element>"
        "<anyName><except><name>known</name></except></anyName><text/>"
        "</element></zeroOrMore>"
    )
    route = root(schema)
    assert set(route.children) == {"known"}
    (wildcard,) = route.wildcards
    assert wildcard.role == "child"
    assert wildcard.ordered
    assert wildcard.name_class.contains("urn:other", "anything")
    assert not wildcard.name_class.contains(None, "known")
    assert wildcard.cardinality.minimum == 0
    assert wildcard.cardinality.maximum is None
    assert wildcard.child_route_id in schema.projection.routes
    assert route.generic_reason is None


def test_mixed_content_has_no_concatenated_scalar_descriptor():
    schema = compile_body(
        "<mixed><zeroOrMore><element name='em'><text/></element></zeroOrMore></mixed>"
    )
    route = root(schema)
    assert route.content.ordered and route.content.mixed
    assert route.content.scalar_contracts == ()
    assert route.content.model.kind == "interleave"
    assert route.generic_reason is None


def test_generic_descendant_does_not_poison_stable_parent_or_sibling():
    schema = compile_body(
        "<element name='bad'><choice><data type='integer' datatypeLibrary='"
        + XSD
        + "'/><element name='nested'><empty/></element></choice></element>"
        "<element name='good'><text/></element>"
    )
    assert root(schema).generic_reason is None
    assert child(schema, "good").generic_reason is None
    assert "whole-element scalar" in child(schema, "bad").generic_reason
    assert "nested" in child(schema, "bad").children


def test_optional_singular_is_distinct_from_repeatable_empty():
    schema = compile_body(
        "<optional><element name='single'><empty/></element></optional>"
        "<zeroOrMore><element name='many'><empty/></element></zeroOrMore>"
    )
    fields = root(schema).children
    assert (fields["single"].cardinality.minimum, fields["single"].cardinality.maximum) == (0, 1)
    assert not fields["single"].cardinality.repeatable
    assert (fields["many"].cardinality.minimum, fields["many"].cardinality.maximum) == (0, None)
    assert fields["many"].cardinality.repeatable


def test_alias_collisions_keywords_namespaces_and_tree_members_are_escaped():
    schema = compile_body(
        "<attribute name='a'><text/></attribute>"
        "<element name='a'><empty/></element>"
        "<element name='a' ns='urn:other'><empty/></element>"
        "<element name='class'><empty/></element>"
        "<element name='children'><empty/></element>"
        "<element name='a-b'><empty/></element>"
        "<element name='a_x2d_b'><empty/></element>"
    )
    route = root(schema)
    aliases = [f.alias for f in (*route.attributes.values(), *route.children.values())]
    assert len(aliases) == len(set(aliases))
    assert all(a.isidentifier() for a in aliases)
    assert route.children["class"].alias != "class"
    assert route.children["children"].alias != "children"
    assert route.attributes["a"].alias != route.children["a"].alias
    assert route.children["{urn:other}a"].expanded_name == "{urn:other}a"
    assert route.children["a-b"].alias != route.children["a_x2d_b"].alias
    assert {n: f.alias for n, f in route.children.items()} == {
        "a": "a_2",
        "a-b": "a_x2d_b",
        "a_x2d_b": "a_x2d_b_2",
        "children": "children_2",
        "class": "field_class",
        "{urn:other}a": "a_3",
    }


def test_distinct_roots_and_conflicting_same_name_roots_are_not_fabricated():
    schema = RelaxNGSchema.compile(
        io.StringIO(f"""<choice xmlns="{NS}">
      <element name="a"><attribute name="x"><text/></attribute></element>
      <element name="a"><attribute name="y"><text/></attribute></element>
      <element name="b"><empty/></element></choice>""")
    )
    schema.require_valid()
    plan = schema.projection
    assert {plan.routes[r].expanded_name for r in plan.roots} == {"a", "b"}
    assert set(plan.resolve_route("a").attributes) == {"x", "y"}
    assert plan.resolve_route("a").generic_reason is None


def test_same_name_in_distinct_contexts_keeps_distinct_structural_routes():
    schema = compile_body(
        "<element name='left'><element name='a'><text/></element></element>"
        "<element name='right'><element name='a'><empty/></element></element>"
    )
    plan = schema.projection
    left = plan.routes[child(schema, "left").children["a"].child_route_id]
    right = plan.routes[child(schema, "right").children["a"].child_route_id]
    assert left.route_id != right.route_id
    assert len(left.content.scalar_contracts) == 1
    assert right.content.scalar_contracts == ()
    with pytest.raises(ValueError, match="ambiguous"):
        plan.resolve_route("a")


def test_route_limit_zero_and_exact_boundary_raise_not_fallback():
    for limit, observed in ((0, 1), (1, 2)):
        with pytest.raises(RelaxNGLimitError) as caught:
            compile_body(
                "<element name='a'><text/></element>",
                limits=RelaxNGLimits(max_projection_routes=limit),
            )
        assert caught.value.stage == "projection"
        assert caught.value.budget == "max_projection_routes"
        assert caught.value.observed == observed
    schema = compile_body(
        "<element name='a'><text/></element>", limits=RelaxNGLimits(max_projection_routes=2)
    )
    assert len(schema.projection.routes) == 2
    assert all(r.generic_reason is None for r in schema.projection.routes.values())


def test_rebuild_and_relocation_preserve_ids(tmp_path):
    from pyxsd.relaxng.projection import plan_projections

    text = f'<element xmlns="{NS}" name="doc"><element name="a"><text/></element></element>'
    schemas = []
    for name in ("one.rng", "two.rng"):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        schemas.append(RelaxNGSchema.compile(path))
    first, second = (s.projection for s in schemas)
    assert first.grammar_fingerprint == second.grammar_fingerprint
    assert tuple(first.routes) == tuple(second.routes)
    assert plan_projections(schemas[0].grammar) == first


def test_hash_seed_does_not_change_the_finite_route_graph():
    script = f"""import io
from pyxsd.relaxng.schema import RelaxNGSchema
s = RelaxNGSchema.compile(io.StringIO('''<element xmlns="{NS}" name="doc">
<choice><element name="a"><text/></element><element name="b"><empty/></element></choice>
</element>'''))
print(s.projection.grammar_fingerprint, tuple(s.projection.routes))
"""
    results = [
        subprocess.check_output(
            [sys.executable, "-c", script], env={**os.environ, "PYTHONHASHSEED": seed}, text=True
        )
        for seed in ("1", "981")
    ]
    assert results[0] == results[1]


def test_planning_never_builds_evidence_or_decodes_values(monkeypatch):
    import pyxsd.relaxng.datatypes as datatypes
    import pyxsd.relaxng.schema as schema_module

    def forbidden(*args, **kwargs):
        pytest.fail("projection planning touched instance evidence or decoded a value")

    monkeypatch.setattr(schema_module, "build_binding_evidence", forbidden)
    monkeypatch.setattr(datatypes.ScalarDatatype, "decode", forbidden)
    schema = compile_body("<data type='integer' datatypeLibrary='" + XSD + "'/>")
    (contract,) = root(schema).content.scalar_contracts
    assert contract.type_name == "integer"


def test_merged_route_retains_all_authored_assignment_origins():
    schema = compile_body(
        "<choice><element name='a'><text/></element><element name='a'><text/></element></choice>"
    )
    locations = {
        o.location
        for o in child(schema, "a").origins
        if o.location.attribute is None and o.location.path[-1].name == f"{{{NS}}}element"
    }
    assert len(locations) == 2


def test_finite_name_choice_creates_optional_named_views_not_a_wildcard():
    schema = compile_body("<element><choice><name>a</name><name>b</name></choice><text/></element>")
    route = root(schema)
    assert set(route.children) == {"a", "b"}
    assert route.wildcards == ()
    assert all(f.cardinality.minimum == 0 for f in route.children.values())
    assert all(f.cardinality.maximum == 1 for f in route.children.values())
    assert route.content.model.kind == "child"
    assert {f.expanded_name for f in route.content.model.fields} == {"a", "b"}
    assert route.constraints == (route.content.model,)
    assert route.children["a"].child_route_id != route.children["b"].child_route_id


def test_wildcard_overlapping_named_view_merges_all_reachable_assignments():
    schema = compile_body(
        "<choice><element name='a'><attribute name='fixed'><text/></attribute></element>"
        "<element><anyName/><attribute name='open'><text/></attribute></element></choice>"
    )
    route = root(schema)
    assert route.children["a"].cardinality.minimum == 0
    assert set(child(schema, "a").attributes) == {"fixed", "open"}
    (wildcard,) = route.wildcards
    assert not wildcard.name_class.contains(None, "a")
    wildcard_route = schema.projection.routes[wildcard.child_route_id]
    assert set(wildcard_route.attributes) == {"open"}
    assert wildcard_route.expanded_name is None


def test_wildcard_attributes_and_scalar_restrictions_have_static_contracts():
    schema = compile_body(
        "<zeroOrMore><attribute><anyName/><choice><value>yes</value>"
        "<data type='token'><except><value>no</value></except></data></choice></attribute></zeroOrMore>"
        "<element name='values'><list><oneOrMore>"
        "<data type='integer' datatypeLibrary='" + XSD + "'/>"
        "</oneOrMore></list></element>"
    )
    route = root(schema)
    (wildcard,) = route.wildcards
    assert wildcard.role == "attribute"
    assert not wildcard.ordered
    assert wildcard.child_route_id is None
    assert {r.kind for c in wildcard.scalar_contracts for r in c.restrictions} == {
        "value",
        "except",
    }
    assert child(schema, "values").content.scalar_contracts[0].kind == "list"
    assert route.generic_reason is None


def test_open_name_choice_keeps_one_finite_wildcard_route():
    schema = compile_body(
        "<element><choice><name>a</name><nsName ns='urn:other'/></choice><text/></element>"
    )
    route = root(schema)
    assert route.children == {}
    (wildcard,) = route.wildcards
    assert wildcard.name_class.contains(None, "a")
    assert wildcard.name_class.contains("urn:other", "b")
    assert not wildcard.name_class.contains(None, "b")
    assert len(schema.projection.routes) == 2


def test_unknown_schema_occurrence_is_not_a_runtime_route_guess():
    from dataclasses import replace

    schema = compile_body("<empty/>")
    key = root(schema).occurrence
    with pytest.raises(KeyError):
        schema.projection.route_for_occurrence(replace(key, assignment_ids=(999,)))
    with pytest.raises(KeyError):
        schema.projection.route_for_occurrence(replace(key, grammar_fingerprint="other"))


def test_opaque_validation_only_datatypes_remain_compilable_without_fake_contracts():
    from pyxsd.relaxng.datatypes import Datatype, DatatypeLibraryRegistration

    class Opaque(Datatype):
        def allows(self, lexical, context):
            return True

        def equal(self, left, left_context, right, right_context):
            return left == right

    class Factory:
        def create(self, type_name, parameters, context):
            return Opaque()

    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="doc"><attribute name="opaque">'
            '<data type="thing" datatypeLibrary="urn:opaque"/></attribute></element>'
        ),
        datatype_libraries={"urn:opaque": DatatypeLibraryRegistration(Factory(), "opaque/1")},
    )
    schema.require_valid()
    assert "stable RELAX NG library and type metadata" in root(schema).generic_reason
    assert root(schema).attributes["opaque"].scalar_contracts == ()
    assert not schema.parse(io.StringIO('<doc opaque="anything"/>'), typed=False).report.has_errors


def test_incorrect_schema_is_reported_before_zero_projection_budget():
    from pyxsd.exceptions import ValidationError

    schema = RelaxNGSchema.compile(
        io.StringIO(f'<grammar xmlns="{NS}"><start><ref name="missing"/></start></grammar>'),
        limits=RelaxNGLimits(max_projection_routes=0),
    )
    assert schema.report.has_errors
    with pytest.raises(ValidationError):
        _ = schema.projection


def test_not_allowed_start_does_not_fabricate_a_root_or_consume_a_route():
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<notAllowed xmlns="{NS}"/>'),
        limits=RelaxNGLimits(max_projection_routes=0),
    )
    schema.require_valid()
    assert schema.projection.roots == ()
    assert schema.projection.routes == {}


def test_open_root_assignments_keep_name_and_content_correlations():
    schema = RelaxNGSchema.compile(
        io.StringIO(f"""<choice xmlns="{NS}">
          <element><nsName ns="urn:u"/><attribute name="x"><text/></attribute></element>
          <element><nsName ns="urn:v"/><attribute name="y"><text/></attribute></element>
        </choice>""")
    )
    schema.require_valid()
    route = root(schema)
    assert set(route.attributes) == {"x", "y"}
    assert route.name_class.contains("urn:u", "r")
    assert route.name_class.contains("urn:v", "r")
    assignments = {a.content.fields[0].expanded_name: a for a in route.assignments.values()}
    assert assignments["x"].name_class.contains("urn:u", "r")
    assert not assignments["x"].name_class.contains("urn:v", "r")
    assert assignments["y"].name_class.contains("urn:v", "r")
    assert not assignments["y"].name_class.contains("urn:u", "r")


def test_aliases_are_valid_python_identifiers_after_unicode_normalization():
    import unicodedata

    schema = compile_body(
        "<element name='a·b'><empty/></element>"
        "<element name='K'><empty/></element>"
        "<element name='\N{KELVIN SIGN}'><empty/></element>"
        "<element name='_private'><empty/></element>"
    )
    aliases = {n: f.alias for n, f in root(schema).children.items()}
    assert all(a.isidentifier() for a in aliases.values())
    assert all(unicodedata.normalize("NFKC", a) == a for a in aliases.values())
    assert len(set(aliases.values())) == 4
    assert aliases["K"] != aliases["\N{KELVIN SIGN}"]
    assert not aliases["_private"].startswith("_")
