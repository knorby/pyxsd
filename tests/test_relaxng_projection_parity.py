"""Phase 4 projection parity gate: typed API, adversarial routes, and stubs.

This module consumes the public RELAX NG projection, mutation, introspection,
and stub workflows.  It is deliberately an integration gate rather than a
second unit-test layer: every matrix row below is one authored grammar with a
real typed parse, runtime descriptor checks, stub inspection, introspection
lookup, and a representative mutation/revalidation round trip.  None of these
assertions may choose an instance layout, rank derivations, or fall back to a
generic route without a semantic reason.

Pydantic, Arrow, and JSON adapters are Phase 5 work and are deliberately absent
here.  This gate proves the shared projection and evidence contracts those
adapters will consume: route constraints, cardinality envelopes, ordered content,
wildcards, scalar contracts, encoding patterns, and projection fingerprints.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import pyxsd
from pyxsd.exceptions import ValidationError
from pyxsd.relaxng import (
    AlternativeValue,
    RelaxNGDocument,
    RelaxNGElement,
    StaleInterpretationError,
    describe_component,
    describe_schema,
)
from pyxsd.relaxng.binding import (
    WILDCARD_CHILDREN,
    AttributeDescriptor,
    ChildDescriptor,
    ChildSequence,
    ScalarDescriptor,
)
from pyxsd.relaxng.schema import RelaxNGSchema

NS = "http://relaxng.org/ns/structure/1.0"
FIXTURES = Path(__file__).parent / "relaxng" / "fixtures" / "parity"

MATRIX_FIXTURES = (
    "duplicate-alternatives",
    "scalar-alternative",
    "child-choice",
    "interleave",
    "recursive",
    "repeated-same-name",
    "wildcard",
    "mixed-content",
)


# ---------------------------------------------------------------------------
# Public-workflow helpers
# ---------------------------------------------------------------------------


def compile_fixture(name: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(FIXTURES / f"{name}.rng")
    schema.require_valid()
    assert not schema.report.has_errors
    return schema


def typed(schema: RelaxNGSchema, xml: str) -> RelaxNGDocument:
    document = schema.parse(io.StringIO(xml), typed=True)
    assert document.typed
    assert document.is_valid
    assert all(not issue.is_error for issue in document.report)
    assert document.binding_evidence is not None
    assert document.interpretations_current
    return document


def root_route(schema: RelaxNGSchema) -> Any:
    return schema.projection.routes[schema.projection.roots[0]]


def child_route(schema: RelaxNGSchema, name: str) -> Any:
    route = root_route(schema)
    return schema.projection.routes[route.children[name].child_route_id]


def _content_field_names(contract: Any) -> set[str]:
    names = {field.expanded_name for field in contract.fields if field.expanded_name is not None}
    for operand in contract.operands:
        names.update(_content_field_names(operand))
    return names


def _descriptor_aliases(cls: type[Any]) -> tuple[str, ...]:
    return tuple(
        name
        for name, value in cls.__dict__.items()
        if not name.startswith("_")
        and isinstance(value, (AttributeDescriptor, ChildDescriptor, ScalarDescriptor))
    )


def _route_identity(schema: RelaxNGSchema) -> tuple[Any, ...]:
    """Return every deterministic public identity one compilation exposes."""
    plan = schema.projection
    factory = schema.projection_classes
    classes = tuple(
        (route_id, factory.class_for(route_id).__module__, factory.class_for(route_id).__qualname__)
        for route_id in plan.routes
    )
    components = tuple(
        component.id
        for component in describe_schema(schema).components
        if component.kind == "route"
    )
    return (
        plan.grammar_fingerprint,
        tuple(plan.routes),
        tuple((route_id, plan.routes[route_id].generic_reason) for route_id in plan.routes),
        classes,
        components,
    )


def _assert_stub_surface(schema: RelaxNGSchema) -> None:
    """Full and compact stubs must describe exactly the runtime route classes."""
    plan = schema.projection
    factory = schema.projection_classes
    full = schema.stub()
    compact = schema.stub(compact=True)
    ast.parse(full)

    for route_id, route in plan.routes.items():
        cls = factory.class_for(route_id)
        if route.generic_reason is None:
            assert cls is not RelaxNGElement
            assert cls.__dict__["_relaxng_route_id_"] == route_id
            assert f"class {cls.__name__}(" in full
            assert f"class {cls.__name__}(" in compact
            assert f"# route: {route_id}" in full
            for alias in _descriptor_aliases(cls):
                assert f"    {alias}:" in full, (route_id, alias)
                assert f"  {alias}:" in compact, (route_id, alias)
        else:
            assert cls is RelaxNGElement
            assert route_id in full and route_id in compact
            assert f"# route: {route_id}" not in full
            assert any(
                route_id in line and "generic" in line.casefold() for line in full.splitlines()
            )
            assert any(
                route_id in line and "generic" in line.casefold() for line in compact.splitlines()
            )


def assert_case_surface(schema: RelaxNGSchema, document: RelaxNGDocument) -> None:
    """Assert the shared projection/evidence surface every matrix row consumes."""
    plan = schema.projection
    factory = schema.projection_classes
    evidence = document.binding_evidence
    assert evidence is not None

    assert not schema.report.has_errors
    assert plan.roots
    root = plan.routes[plan.roots[0]]
    assert root.generic_reason is None
    assert type(document.root) is factory.class_for(root.route_id)
    assert evidence.projection_fingerprint == plan.grammar_fingerprint
    assert evidence.accepting_roots

    route_components = {
        component.id: component
        for component in describe_schema(schema).components
        if component.kind == "route"
    }
    assert set(route_components) == set(plan.routes)

    for route_id, route in plan.routes.items():
        assert plan.route_for_occurrence(route.occurrence) == route_id
        assert plan.resolve_route(route_id) is route
        assert route.occurrence.grammar_fingerprint == plan.grammar_fingerprint
        assert route.content.ordered
        assert route.content.model.kind
        component = describe_component(schema, route_id)
        assert component.id == route_id
        assert component.kind == "route"
        assert component.name == route.expanded_name
        assert route_components[route_id] == component
        cls = factory.class_for(route_id)
        if route.generic_reason is None:
            assert cls is not RelaxNGElement
            assert cls.__dict__["_relaxng_route_id_"] == route_id
            assert cls.__dict__["_relaxng_plan_fingerprint_"] == plan.grammar_fingerprint
        else:
            assert cls is RelaxNGElement
            assert route.generic_reason.strip()
            assert "not implemented" not in route.generic_reason.casefold()
        for field in (*route.attributes.values(), *route.children.values()):
            for contract in field.scalar_contracts:
                assert contract.pattern_identity in plan.encoding_patterns
        for contract in route.content.scalar_contracts:
            assert contract.pattern_identity in plan.encoding_patterns
        for wildcard in route.wildcards:
            for contract in wildcard.scalar_contracts:
                assert contract.pattern_identity in plan.encoding_patterns

    # No runtime node may use a generated class for a generic or unknown route.
    for node in _walk(document.root):
        node_class = type(node)
        if node_class is RelaxNGElement:
            continue
        node_route_id = node_class.__dict__["_relaxng_route_id_"]
        assert node_route_id in plan.routes
        assert plan.routes[node_route_id].generic_reason is None
        assert node_class is factory.class_for(node_route_id)

    _assert_stub_surface(schema)


def _walk(root: RelaxNGElement) -> Iterator[RelaxNGElement]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node._children_))


# ---------------------------------------------------------------------------
# Matrix row 1: duplicate equivalent alternatives
# ---------------------------------------------------------------------------


def test_duplicate_equivalent_alternatives_merge_one_class_and_keep_origins():
    schema = compile_fixture("duplicate-alternatives")
    document = typed(schema, "<doc><a>one</a></doc>")
    assert_case_surface(schema, document)

    plan = schema.projection
    route = root_route(schema)
    field = route.children["a"]
    assert (field.cardinality.minimum, field.cardinality.maximum) == (1, 1)
    assert len(plan.routes) == 2
    assert route.generic_reason is None
    selected = child_route(schema, "a")
    assert selected.generic_reason is None
    assert selected.content.scalar_contracts
    assert {contract.kind for contract in selected.content.scalar_contracts} == {"text"}

    element_origins = {
        origin.location
        for origin in selected.origins
        if origin.location is not None
        and origin.location.attribute is None
        and origin.location.path
        and origin.location.path[-1].name == f"{{{NS}}}element"
    }
    assert len(element_origins) == 2

    assert type(document.root) is schema.projection_classes.class_for(route.route_id)
    assert type(document.root.a) is schema.projection_classes.class_for(selected.route_id)
    assert document.root.a.typed_value.lexical == "one"

    # The duplicate derivations do not become separate generated classes.
    class_names = {
        schema.projection_classes.class_for(route_id).__name__
        for route_id in plan.routes
        if plan.routes[route_id].generic_reason is None
    }
    assert len(class_names) == 2


# ---------------------------------------------------------------------------
# Matrix row 2: one scalar position with integer/string alternatives
# ---------------------------------------------------------------------------


def test_scalar_alternatives_keep_one_descriptor_and_alternative_value():
    schema = compile_fixture("scalar-alternative")
    document = typed(schema, '<doc a="12"/>')
    assert_case_surface(schema, document)

    route = root_route(schema)
    field = route.attributes["a"]
    assert (field.cardinality.minimum, field.cardinality.maximum) == (1, 1)
    assert {contract.kind for contract in field.scalar_contracts} == {"atomic"}
    assert {contract.type_name for contract in field.scalar_contracts} == {
        "integer",
        "string",
    }
    assert route.generic_reason is None

    value = document.root.a
    assert isinstance(value, AlternativeValue)
    assert value.lexical == "12"
    assert {candidate.contract.type_name for candidate in value.candidates} == {
        "integer",
        "string",
    }
    candidate_ids = tuple(candidate.candidate_id for candidate in value.candidates)
    assert candidate_ids == tuple(sorted(candidate_ids))

    word = typed(schema, '<doc a="word"/>')
    assert type(word.root) is type(document.root)
    assert isinstance(word.root.a, AlternativeValue)
    assert word.root.a.lexical == "word"
    assert len(word.root.a.candidates) == 1
    assert word.root.a.candidates[0].candidate_id in candidate_ids
    assert word.root.a.candidates[0].contract.type_name == "string"


# ---------------------------------------------------------------------------
# Matrix row 3: exclusive choice between child a and child b
# ---------------------------------------------------------------------------


def test_child_choice_has_optional_exclusive_descriptors_and_constraint():
    schema = compile_fixture("child-choice")
    document = typed(schema, "<doc><a/></doc>")
    assert_case_surface(schema, document)

    route = root_route(schema)
    assert set(route.children) == {"a", "b"}
    assert all(
        (field.cardinality.minimum, field.cardinality.maximum) == (0, 1)
        for field in route.children.values()
    )
    assert route.constraints
    choice = next(constraint for constraint in route.constraints if constraint.kind == "choice")
    assert _content_field_names(choice) == {"a", "b"}
    assert document.root.a is not None
    assert document.root.b is None
    assert type(document.root.a) is schema.projection_classes.class_for(
        route.children["a"].child_route_id
    )

    other = typed(schema, "<doc><b/></doc>")
    assert type(other.root) is type(document.root)
    assert other.root.a is None
    assert other.root.b is not None
    assert type(other.root.b) is schema.projection_classes.class_for(
        route.children["b"].child_route_id
    )
    assert type(document.root.a) is not type(other.root.b)


# ---------------------------------------------------------------------------
# Matrix row 4: interleave of distinct named children
# ---------------------------------------------------------------------------


def test_interleave_keeps_named_descriptors_and_original_child_order():
    schema = compile_fixture("interleave")
    document = typed(schema, '<doc><b id="7"/><a>hello</a></doc>')
    assert_case_surface(schema, document)

    route = root_route(schema)
    assert route.content.model.kind == "interleave"
    assert route.content.ordered and not route.content.mixed
    assert set(route.children) == {"a", "b"}
    assert all(
        (field.cardinality.minimum, field.cardinality.maximum) == (1, 1)
        for field in route.children.values()
    )
    assert [child._name_ for child in document.root.children] == ["b", "a"]
    assert document.root.children[0] is document.root.b
    assert document.root.children[1] is document.root.a
    assert document.root.a.typed_value.lexical == "hello"
    assert int(document.root.b.id) == 7
    assert any(constraint.kind == "interleave" for constraint in route.constraints)


# ---------------------------------------------------------------------------
# Matrix row 5: recursive named elements
# ---------------------------------------------------------------------------


def _recursive_xml(depth: int) -> str:
    opening = "".join(f'<node name="n{index}">' for index in range(depth))
    return opening + "</node>" * depth


def test_recursive_routes_reuse_one_finite_class_at_every_depth():
    schema = compile_fixture("recursive")
    plan = schema.projection
    assert len(plan.routes) == 1
    route = root_route(schema)
    assert route.generic_reason is None
    assert route.children["node"].child_route_id == route.route_id
    assert route.children["node"].cardinality.repeatable
    route_id = route.route_id
    generated = schema.projection_classes.class_for(route_id)
    generated_fqn = (generated.__module__, generated.__qualname__)

    for depth in (1, 8, 32):
        document = typed(schema, _recursive_xml(depth))
        assert_case_surface(schema, document)
        assert type(document.root) is generated
        nodes = list(_walk(document.root))
        assert len(nodes) == depth
        for index, node in enumerate(nodes):
            assert type(node) is generated
            assert node._relaxng_route_id_ == route_id
            assert str(node.name) == f"n{index}"
        assert (generated.__module__, generated.__qualname__) == generated_fqn

    assert tuple(plan.routes) == (route_id,)
    assert schema.projection_classes.class_for(route_id) is generated


# ---------------------------------------------------------------------------
# Matrix row 6: repeated same-name children with differing contracts
# ---------------------------------------------------------------------------


def test_repeated_same_name_children_keep_order_and_merged_contract():
    schema = compile_fixture("repeated-same-name")
    document = typed(schema, '<doc><a x="1"/><a y="2"/></doc>')
    assert_case_surface(schema, document)

    plan = schema.projection
    route = root_route(schema)
    field = route.children["a"]
    assert field.cardinality.repeatable
    assert (field.cardinality.minimum, field.cardinality.maximum) == (2, 2)
    merged = plan.routes[field.child_route_id]
    assert merged.generic_reason is None
    assert set(merged.attributes) == {"x", "y"}
    assert all(
        (attribute.cardinality.minimum, attribute.cardinality.maximum) == (0, 1)
        for attribute in merged.attributes.values()
    )
    assert any(constraint.kind == "choice" for constraint in merged.constraints)

    sequence = document.root.a
    assert isinstance(sequence, ChildSequence)
    assert sequence is document.root.a
    assert len(sequence) == 2
    assert [child._name_ for child in sequence] == ["a", "a"]
    assert type(sequence[0]) is type(sequence[1])
    assert type(sequence[0]) is schema.projection_classes.class_for(merged.route_id)
    assert sequence[0]._relaxng_route_id_ == sequence[1]._relaxng_route_id_ == merged.route_id
    assert sequence[0].x.lexical == "1"
    assert sequence[0].y is None
    assert sequence[1].x is None
    assert sequence[1].y.lexical == "2"

    # Both structural assignment leaves remain available to later adapters.
    assert len(merged.assignments) == 2
    assigned = {
        assignment.content.fields[0].expanded_name for assignment in merged.assignments.values()
    }
    assert assigned == {"x", "y"}


# ---------------------------------------------------------------------------
# Matrix row 7: stable named field with an open wildcard
# ---------------------------------------------------------------------------


def test_named_field_and_ordered_wildcard_partition_without_fabrication():
    schema = compile_fixture("wildcard")
    document = typed(schema, "<doc><known>k</known><extra>e</extra><other>o</other></doc>")
    assert_case_surface(schema, document)

    route = root_route(schema)
    assert set(route.children) == {"known"}
    assert len(route.wildcards) == 1
    wildcard = route.wildcards[0]
    assert wildcard.role == "child"
    assert wildcard.ordered
    assert (wildcard.cardinality.minimum, wildcard.cardinality.maximum) == (0, None)
    assert wildcard.name_class.contains(None, "extra")
    assert wildcard.name_class.contains("urn:other", "anything")
    assert not wildcard.name_class.contains(None, "known")
    assert document.root.known.typed_value.lexical == "k"

    wildcard_children = document.root[WILDCARD_CHILDREN]
    assert [child._name_ for child in wildcard_children] == ["extra", "other"]
    assert all(
        type(child) is schema.projection_classes.class_for(wildcard.child_route_id)
        for child in wildcard_children
    )
    assert all(child._name_ != "known" for child in wildcard_children)
    assert "extra" not in route.children
    assert "other" not in route.children
    assert not hasattr(type(document.root), "extra")
    assert not hasattr(type(document.root), "other")

    full = schema.stub()
    for fabricated in ("extra", "other", "anything"):
        assert f"    {fabricated}:" not in full
    assert "WILDCARD_CHILDREN" in full


# ---------------------------------------------------------------------------
# Matrix row 8: mixed text and elements
# ---------------------------------------------------------------------------


def test_mixed_content_keeps_ordered_text_and_child_view_without_scalar():
    schema = compile_fixture("mixed-content")
    document = typed(schema, "<doc>hello<em>one</em> tail<em>two</em> end</doc>")
    assert_case_surface(schema, document)

    route = root_route(schema)
    assert route.content.ordered and route.content.mixed
    assert route.content.scalar_contracts == ()
    assert route.children["em"].cardinality.repeatable
    root_class = type(document.root)
    assert root_class.__dict__["_relaxng_scalar_field_"] is None
    assert not hasattr(document.root, "typed_value")

    content = list(document.root.iter_content())
    assert content[0] == "hello"
    assert content[1] is document.root.children[0]
    assert content[2] == " tail"
    assert content[3] is document.root.children[1]
    assert content[4] == " end"
    assert [child._name_ for child in document.root.children] == ["em", "em"]
    assert isinstance(document.root.em, ChildSequence)
    assert [child.typed_value.lexical for child in document.root.em] == ["one", "two"]

    for text in (schema.stub(), schema.stub(compact=True)):
        assert "mixed content: ordered text and child views" in text


# ---------------------------------------------------------------------------
# Route-local generic descendant
# ---------------------------------------------------------------------------


def test_generic_descendant_has_semantic_reason_and_keeps_stable_siblings():
    schema = compile_fixture("generic-descendant")
    plan = schema.projection
    route = root_route(schema)
    bad_route = plan.routes[route.children["bad"].child_route_id]
    good_route = plan.routes[route.children["good"].child_route_id]
    nested_route = plan.routes[bad_route.children["nested"].child_route_id]
    assert route.generic_reason is None
    assert good_route.generic_reason is None
    assert nested_route.generic_reason is None
    assert bad_route.generic_reason is not None
    assert "whole-element scalar" in bad_route.generic_reason
    assert "not implemented" not in bad_route.generic_reason.casefold()

    for xml in (
        "<doc><bad>12</bad><good>text</good></doc>",
        "<doc><bad><nested/></bad><good>text</good></doc>",
    ):
        document = typed(schema, xml)
        assert_case_surface(schema, document)
        assert type(document.root) is schema.projection_classes.class_for(route.route_id)
        assert type(document.root.bad) is RelaxNGElement
        assert type(document.root.good) is schema.projection_classes.class_for(good_route.route_id)
        assert not hasattr(document.root.bad, "nested")
        if document.root.bad.children:
            assert type(document.root.bad.children[0]) is (
                schema.projection_classes.class_for(nested_route.route_id)
            )

    full = schema.stub()
    compact = schema.stub(compact=True)
    assert "class Doc(" in full and "class Good(" in full
    assert "class Bad(" not in full
    assert "bad: _rng" in full
    assert bad_route.route_id in full and bad_route.generic_reason in full
    assert "bad: RNG" in compact
    assert bad_route.route_id in compact

    stable = [
        route_id for route_id, contract in plan.routes.items() if contract.generic_reason is None
    ]
    assert stable
    assert all(
        schema.projection_classes.class_for(route_id) is not RelaxNGElement for route_id in stable
    )


# ---------------------------------------------------------------------------
# Classification, determinism, and filtered stub behavior
# ---------------------------------------------------------------------------


def test_stable_or_generic_classification_ignores_instance_values():
    alternatives = compile_fixture("scalar-alternative")
    integer = typed(alternatives, '<doc a="12"/>')
    text = typed(alternatives, '<doc a="word"/>')
    assert type(integer.root) is type(text.root)
    assert type(integer.root) is alternatives.projection_classes.class_for(
        alternatives.projection.roots[0]
    )
    assert root_route(alternatives).generic_reason is None
    assert isinstance(integer.root.a, AlternativeValue)
    assert isinstance(text.root.a, AlternativeValue)

    generic = compile_fixture("generic-descendant")
    scalar_branch = typed(generic, "<doc><bad>12</bad><good>text</good></doc>")
    child_branch = typed(generic, "<doc><bad><nested/></bad><good>text</good></doc>")
    route = root_route(generic)
    bad_route = generic.projection.routes[route.children["bad"].child_route_id]
    assert bad_route.generic_reason is not None
    assert type(scalar_branch.root) is type(child_branch.root)
    assert type(scalar_branch.root.bad) is type(child_branch.root.bad) is RelaxNGElement


def test_ambiguous_candidate_ids_survive_values_paths_and_recompiles(tmp_path):
    source = FIXTURES / "scalar-alternative.rng"
    first = compile_fixture("scalar-alternative")
    identity = _route_identity(first)
    two_candidates = tuple(
        candidate.candidate_id for candidate in typed(first, '<doc a="12"/>').root.a.candidates
    )
    one_candidate = tuple(
        candidate.candidate_id for candidate in typed(first, '<doc a="word"/>').root.a.candidates
    )
    assert len(two_candidates) == 2
    assert tuple(sorted(two_candidates)) == two_candidates
    assert len(one_candidate) == 1
    assert one_candidate[0] in two_candidates

    relocated_path = tmp_path / "relocated-scalar-alternative.rng"
    relocated_path.write_bytes(source.read_bytes())
    relocated = RelaxNGSchema.compile(relocated_path)
    relocated.require_valid()
    assert _route_identity(relocated) == identity
    assert (
        tuple(
            candidate.candidate_id
            for candidate in typed(relocated, '<doc a="12"/>').root.a.candidates
        )
        == two_candidates
    )

    recompiled = RelaxNGSchema.compile(source)
    recompiled.require_valid()
    assert _route_identity(recompiled) == identity
    assert (
        tuple(
            candidate.candidate_id
            for candidate in typed(recompiled, '<doc a="12"/>').root.a.candidates
        )
        == two_candidates
    )
    assert (
        tuple(
            candidate.candidate_id for candidate in typed(first, '<doc a="12"/>').root.a.candidates
        )
        == two_candidates
    )


def test_ids_survive_relocation_recompilation_and_repeated_calls(tmp_path):
    for name in MATRIX_FIXTURES:
        source = FIXTURES / f"{name}.rng"
        first = compile_fixture(name)
        identity = _route_identity(first)
        assert _route_identity(first) == identity

        relocated_path = tmp_path / f"relocated-{name}.rng"
        relocated_path.write_bytes(source.read_bytes())
        relocated = RelaxNGSchema.compile(relocated_path)
        relocated.require_valid()
        assert _route_identity(relocated) == identity

        recompiled = RelaxNGSchema.compile(source)
        recompiled.require_valid()
        assert _route_identity(recompiled) == identity


def test_ids_are_hash_seed_independent_across_depth_and_paths(tmp_path):
    alternative = FIXTURES / "scalar-alternative.rng"
    recursive = FIXTURES / "recursive.rng"
    alternative_copy = tmp_path / "alternative-copy.rng"
    recursive_copy = tmp_path / "recursive-copy.rng"
    alternative_copy.write_bytes(alternative.read_bytes())
    recursive_copy.write_bytes(recursive.read_bytes())

    script = """
import io
import json
import sys
from pathlib import Path

from pyxsd.relaxng import describe_schema
from pyxsd.relaxng.schema import RelaxNGSchema


def alternative_snapshot(path):
    schema = RelaxNGSchema.compile(Path(path))
    document = schema.parse(io.StringIO('<doc a="12"/>'))
    plan = schema.projection
    return {
        "fingerprint": plan.grammar_fingerprint,
        "routes": list(plan.routes),
        "components": [
            component.id
            for component in describe_schema(schema).components
            if component.kind == "route"
        ],
        "classes": [
            [
                schema.projection_classes.class_for(route_id).__module__,
                schema.projection_classes.class_for(route_id).__qualname__,
            ]
            for route_id in plan.routes
        ],
        "candidates": [
            candidate.candidate_id for candidate in document.root.a.candidates
        ],
    }


def recursive_snapshot(path):
    schema = RelaxNGSchema.compile(Path(path))
    plan = schema.projection
    classes = []
    for depth in (1, 8, 32):
        opening = "".join(f'<node name="n{index}">' for index in range(depth))
        document = schema.parse(io.StringIO(opening + "</node>" * depth))
        classes.append(
            [type(document.root).__module__, type(document.root).__qualname__]
        )
    root_id = plan.roots[0]
    return {
        "fingerprint": plan.grammar_fingerprint,
        "routes": list(plan.routes),
        "components": [
            component.id
            for component in describe_schema(schema).components
            if component.kind == "route"
        ],
        "classes": classes,
        "factory": [
            schema.projection_classes.class_for(root_id).__module__,
            schema.projection_classes.class_for(root_id).__qualname__,
        ],
    }


print(json.dumps([
    alternative_snapshot(sys.argv[1]),
    alternative_snapshot(sys.argv[2]),
    recursive_snapshot(sys.argv[3]),
    recursive_snapshot(sys.argv[4]),
], sort_keys=True))
"""
    outputs = []
    for seed in ("1", "981"):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(alternative),
                str(alternative_copy),
                str(recursive),
                str(recursive_copy),
            ],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    payload = json.loads(outputs[0])
    assert payload[0] == payload[1]
    assert payload[2] == payload[3]
    assert len(payload[0]["candidates"]) == 2
    assert len(payload[2]["classes"]) == 3
    assert len({tuple(item) for item in payload[2]["classes"]}) == 1
    assert tuple(payload[2]["classes"][0]) == tuple(payload[2]["factory"])


def test_filtered_stubs_select_routes_and_never_name_undefined_classes():
    schema = compile_fixture("stub-filter")
    plan = schema.projection
    root_id = plan.roots[0]
    route = plan.routes[root_id]
    outer_id = route.children["outer"].child_route_id
    outer = plan.routes[outer_id]
    inner_id = outer.children["inner"].child_route_id

    depth_zero = schema.stub(component_ids=(root_id,), depth=0)
    depth_one = schema.stub(component_ids=(root_id,), depth=1)
    depth_all = schema.stub(component_ids=(root_id,), depth=None)
    compact_zero = schema.stub(compact=True, component_ids=(root_id,), depth=0)
    compact_one = schema.stub(compact=True, component_ids=(root_id,), depth=1)

    assert "class Doc(" in depth_zero and "class Outer(" not in depth_zero
    assert "class Inner(" not in depth_zero
    assert "outer: _any" in depth_zero
    assert "depth=0" in depth_zero and outer_id in depth_zero
    assert "class Doc(" in depth_one and "class Outer(" in depth_one
    assert "class Inner(" not in depth_one
    assert "inner: _any" in depth_one
    assert "depth=1" in depth_one and inner_id in depth_one
    assert "class Doc(" in depth_all and "class Outer(" in depth_all
    assert "class Inner(" in depth_all
    assert depth_all == schema.stub()

    assert "class Doc(RNG)" in compact_zero and "class Outer(RNG)" not in compact_zero
    assert "outer: Any" in compact_zero
    assert "class Outer(RNG)" in compact_one and "class Inner(RNG)" not in compact_one
    assert "inner: Any" in compact_one

    class_counts = tuple(
        sum(isinstance(node, ast.ClassDef) for node in ast.walk(ast.parse(text)))
        for text in (depth_zero, depth_one, depth_all)
    )
    assert class_counts == (1, 2, 3)
    for text, expected in (
        (depth_zero, {"_t0"}),
        (depth_one, {"_t0", "_t1"}),
        (depth_all, {"_t0", "_t1", "_t2"}),
    ):
        used = {
            node.id
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Name) and node.id.startswith("_t")
        }
        defined = {
            target.id
            for node in ast.parse(text).body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id.startswith("_t")
        }
        assert defined == expected
        assert used <= defined

    with pytest.raises(KeyError):
        schema.stub(component_ids=("route:missing",))
    with pytest.raises(ValueError, match="depth"):
        schema.stub(depth=-1)
    start_id = describe_schema(schema).start_ids[0]
    seed_only = schema.stub(component_ids=(start_id,), depth=None)
    assert "class " not in seed_only
    assert "no projection routes" in seed_only
    empty = schema.stub(component_ids=())
    assert "class " not in empty
    assert "no projection routes" in empty
    assert schema.stub(component_ids=()) == empty

    generic_schema = compile_fixture("generic-descendant")
    generic_root = root_route(generic_schema)
    generic_id = generic_root.children["bad"].child_route_id
    generic = generic_schema.projection.routes[generic_id]
    generic_text = generic_schema.stub(component_ids=(generic_id,), depth=0)
    assert "class " not in generic_text
    assert generic_id in generic_text
    assert generic.generic_reason in generic_text
    assert generic.generic_reason is not None


def test_invalid_schema_is_introspection_and_stub_safe_without_projection(monkeypatch):
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<grammar xmlns="{NS}"><start><ref name="missing"/></start></grammar>')
    )
    assert schema.report.has_errors
    description = describe_schema(schema)
    assert "route" not in {component.kind for component in description.components}
    full = schema.stub()
    compact = schema.stub(compact=True)
    assert "schema report has errors" in full
    assert "class " not in full
    assert "schema report has errors" in compact
    assert schema.stub() == full
    with pytest.raises(ValidationError):
        _ = schema.projection

    monkeypatch.setattr(
        RelaxNGSchema,
        "projection",
        property(lambda self: pytest.fail("invalid schema stub/introspection read projection")),
    )
    assert schema.stub() == full
    assert schema.stub(compact=True) == compact
    assert describe_schema(schema) == description


# ---------------------------------------------------------------------------
# Mutation, serialization, and revalidation on every matrix grammar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MutationCase:
    xml: str
    edit: Callable[[RelaxNGDocument], None]
    fragment: str
    check: Callable[[RelaxNGDocument], None]
    snapshot: Callable[[RelaxNGDocument], AlternativeValue] | None = None


def _edit_duplicate(document: RelaxNGDocument) -> None:
    document.root.a.typed_value = "changed"


def _check_duplicate(document: RelaxNGDocument) -> None:
    assert document.root.a.typed_value.lexical == "changed"


def _edit_scalar_alternative(document: RelaxNGDocument) -> None:
    other = typed(document.schema, '<doc a="7"/>')
    document.root.a = other.root.a


def _check_scalar_alternative(document: RelaxNGDocument) -> None:
    value = document.root.a
    assert isinstance(value, AlternativeValue)
    assert value.lexical == "7"
    assert {candidate.contract.type_name for candidate in value.candidates} == {
        "integer",
        "string",
    }


def _edit_child_choice(document: RelaxNGDocument) -> None:
    document.root.a.set_name("b")


def _check_child_choice(document: RelaxNGDocument) -> None:
    assert document.root.a is None
    assert document.root.b is not None
    assert type(document.root.b) is document.schema.projection_classes.class_for(
        root_route(document.schema).children["b"].child_route_id
    )


def _edit_interleave(document: RelaxNGDocument) -> None:
    document.root.a.typed_value = "changed"


def _check_interleave(document: RelaxNGDocument) -> None:
    assert document.root.a.typed_value.lexical == "changed"
    assert int(document.root.b.id) == 7


def _edit_recursive(document: RelaxNGDocument) -> None:
    document.root.name = "renamed"


def _check_recursive(document: RelaxNGDocument) -> None:
    assert str(document.root.name) == "renamed"
    assert type(document.root) is document.schema.projection_classes.class_for(
        document.schema.projection.roots[0]
    )


def _edit_repeated(document: RelaxNGDocument) -> None:
    document.root.a[0].x = "9"


def _check_repeated(document: RelaxNGDocument) -> None:
    assert document.root.a[0].x.lexical == "9"
    assert document.root.a[1].y.lexical == "2"


def _edit_wildcard(document: RelaxNGDocument) -> None:
    document.root.known.typed_value = "changed"


def _check_wildcard(document: RelaxNGDocument) -> None:
    assert document.root.known.typed_value.lexical == "changed"
    assert [child._name_ for child in document.root[WILDCARD_CHILDREN]] == ["extra"]


def _edit_mixed(document: RelaxNGDocument) -> None:
    document.root.em[0].typed_value = "changed"


def _check_mixed(document: RelaxNGDocument) -> None:
    assert [child.typed_value.lexical for child in document.root.em] == ["changed", "two"]


def _edit_generic(document: RelaxNGDocument) -> None:
    document.root.good.typed_value = "changed"


def _check_generic(document: RelaxNGDocument) -> None:
    assert document.root.good.typed_value.lexical == "changed"
    assert type(document.root.bad) is RelaxNGElement


def _snapshot_duplicate(document: RelaxNGDocument) -> AlternativeValue:
    return document.root.a.typed_value


def _snapshot_scalar_alternative(document: RelaxNGDocument) -> AlternativeValue:
    return document.root.a


MUTATION_CASES = {
    "duplicate-alternatives": _MutationCase(
        "<doc><a>one</a></doc>",
        _edit_duplicate,
        "changed",
        _check_duplicate,
        _snapshot_duplicate,
    ),
    "scalar-alternative": _MutationCase(
        '<doc a="12"/>',
        _edit_scalar_alternative,
        'a="7"',
        _check_scalar_alternative,
        _snapshot_scalar_alternative,
    ),
    "child-choice": _MutationCase(
        "<doc><a/></doc>",
        _edit_child_choice,
        "<b",
        _check_child_choice,
    ),
    "interleave": _MutationCase(
        '<doc><b id="7"/><a>hello</a></doc>',
        _edit_interleave,
        "changed",
        _check_interleave,
    ),
    "recursive": _MutationCase(
        _recursive_xml(2),
        _edit_recursive,
        'name="renamed"',
        _check_recursive,
    ),
    "repeated-same-name": _MutationCase(
        '<doc><a x="1"/><a y="2"/></doc>',
        _edit_repeated,
        'x="9"',
        _check_repeated,
    ),
    "wildcard": _MutationCase(
        "<doc><known>k</known><extra>e</extra></doc>",
        _edit_wildcard,
        "<known>changed</known>",
        _check_wildcard,
    ),
    "mixed-content": _MutationCase(
        "<doc>hello<em>one</em> tail<em>two</em> end</doc>",
        _edit_mixed,
        "<em>changed</em>",
        _check_mixed,
    ),
    "generic-descendant": _MutationCase(
        "<doc><bad><nested/></bad><good>text</good></doc>",
        _edit_generic,
        "<good>changed</good>",
        _check_generic,
    ),
}


@pytest.mark.parametrize("name", tuple(MUTATION_CASES))
def test_mutation_stales_old_snapshot_and_revalidation_builds_fresh_typed_document(name):
    case = MUTATION_CASES[name]
    schema = compile_fixture(name)
    document = typed(schema, case.xml)
    old_report = document.report
    old_evidence = document.binding_evidence
    old_root_type = type(document.root)
    old_snapshot = None if case.snapshot is None else case.snapshot(document)
    old_lexical = None if old_snapshot is None else old_snapshot.lexical
    old_candidates = None if old_snapshot is None else old_snapshot.candidates

    case.edit(document)

    assert not document.interpretations_current
    assert document.report is old_report
    assert not old_report.has_errors
    assert document.binding_evidence is old_evidence
    assert case.fragment in document.to_string()
    with pytest.raises(StaleInterpretationError):
        document.root.scalar_value()

    fresh = document.revalidate()
    assert fresh is not document
    assert fresh.typed and fresh.is_valid
    assert not fresh.report.has_errors
    assert fresh.interpretations_current
    assert fresh.report is not old_report
    assert fresh.binding_evidence is not old_evidence
    assert type(fresh.root) is old_root_type
    assert type(fresh.root) is schema.projection_classes.class_for(schema.projection.roots[0])
    assert not old_report.has_errors
    assert document.report is old_report
    assert document.binding_evidence is old_evidence
    case.check(fresh)

    if old_snapshot is not None:
        assert old_snapshot.lexical == old_lexical
        assert old_snapshot.candidates == old_candidates


# ---------------------------------------------------------------------------
# Existing XSD surface remains unchanged
# ---------------------------------------------------------------------------

XSD_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="note" type="Note"/>
  <xs:complexType name="Note">
    <xs:sequence>
      <xs:element name="to" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>
"""


def test_matched_xsd_compile_parse_and_stub_regression():
    schema = pyxsd.Schema.compile(io.StringIO(XSD_SCHEMA))
    document = schema.parse(io.StringIO("<note><to>you</to></note>"))
    assert document.is_valid
    assert type(document.root).__qualname__ == "Note"
    assert document.root.to == "you"
    assert type(document.root.to).__name__ == "String"

    full = schema.stub()
    compact = schema.stub(compact=True)
    assert "class Note(SchemaBase):" in full
    assert "to: String" in full
    assert "note: type[Note]" in full
    assert "class Note:" in compact
    assert "to: str" in compact
    assert "roots: note: Note" in compact
    assert schema.stub() == full
    assert schema.stub(compact=True) == compact
