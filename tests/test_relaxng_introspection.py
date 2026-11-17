"""RELAX NG grammar introspection: inventory, stable IDs, and traversal.

The inventory describes the *compiled* RELAX NG graph.  Authored ``define``
names are not retained by composition: every definition component is named by
the composer-generated ``elementN`` key and is marked ``synthetic``.  The
authored sites are still evidence through each component's origins.
"""

from __future__ import annotations

import io
from dataclasses import FrozenInstanceError, fields, replace
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

import pytest
from tests.test_relaxng_values import shared_interior_schema

from pyxsd.exceptions import ValidationError
from pyxsd.locations import IssueLocation
from pyxsd.namespaces import NamespaceContext
from pyxsd.relaxng import (
    RelaxNGComponentDescription,
    RelaxNGSchemaDescription,
    describe_component,
    describe_schema,
    find_components,
    related_components,
)
from pyxsd.relaxng.contracts import grammar_fingerprint, pattern_identity, scalar_contract
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    DatatypeParameter,
    UnsupportedDatatypeOperation,
)
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
from pyxsd.relaxng.patterns import Data, DataExcept, Empty, Group, PatternPool, Ref, Value
from pyxsd.relaxng.resources import ResourceDependency, ResourceReference
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.simplify import CompiledGrammar
from pyxsd.validation import ValidationReport

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"

COMPONENT_FIELDS = (
    "id",
    "kind",
    "name",
    "namespace",
    "location",
    "origins",
    "owner_id",
    "pattern_kind",
    "name_class",
    "datatype_library",
    "datatype_name",
    "datatype_parameters",
    "synthetic",
)


def grammar(body: str) -> str:
    return f'<grammar xmlns="{NS}">{body}</grammar>'


def element(body: str, name: str = "doc") -> str:
    return f'<element xmlns="{NS}" name="{name}">{body}</element>'


def compile_xml(xml: str, *, path: Path | None = None) -> RelaxNGSchema:
    if path is not None:
        path.write_text(xml, encoding="utf-8")
        return RelaxNGSchema.compile(path)
    return RelaxNGSchema.compile(io.StringIO(xml))


def compiled(body: str) -> RelaxNGSchema:
    schema = compile_xml(grammar(body))
    schema.require_valid()
    return schema


def compiled_element(body: str) -> RelaxNGSchema:
    schema = compile_xml(element(body))
    schema.require_valid()
    return schema


def rich_body() -> str:
    return (
        '<start><ref name="doc"/></start>'
        '<define name="doc">'
        '<element name="doc">'
        '<attribute name="id"><text/></attribute>'
        f'<attribute name="q" ns="urn:one"><data type="token" datatypeLibrary="{XSD}"/></attribute>'
        "<zeroOrMore><attribute><anyName><except><choice>"
        "<name>id</name><name>q</name><name>skip</name>"
        '<nsName ns="urn:one"/><nsName ns="urn:two"/>'
        "</choice></except></anyName><text/></attribute></zeroOrMore>"
        "<interleave>"
        '<element name="leaf"><text/></element>'
        "<group>"
        f'<element name="num"><data type="integer" datatypeLibrary="{XSD}">'
        '<param name="minInclusive">1</param>'
        '<param name="maxInclusive">9</param>'
        "<except><value>5</value></except></data></element>"
        '<element name="fixed"><value>ok</value></element>'
        "</group>"
        "</interleave>"
        f'<oneOrMore><element name="item"><list><data type="integer" '
        f'datatypeLibrary="{XSD}"/></list></element></oneOrMore>'
        '<optional><element name="maybe"><empty/></element></optional>'
        "</element>"
        "</define>"
    )


def rich_schema() -> RelaxNGSchema:
    return compiled(rich_body())


def component_map(schema: RelaxNGSchema) -> dict[str, RelaxNGComponentDescription]:
    return {component.id: component for component in describe_schema(schema).components}


def ids(schema: RelaxNGSchema, *, kind: str | None = None) -> set[str]:
    return {component.id for component in find_components(schema, kind=kind)}


def resource_xml(directory: Path) -> tuple[str, str]:
    directory.mkdir()
    external = f'<element xmlns="{NS}" name="external"><empty/></element>'
    (directory / "a.rng").write_text(external, encoding="utf-8")
    (directory / "b.rng").write_text(external, encoding="utf-8")
    return (
        grammar(
            '<start><choice><externalRef href="a.rng"/><externalRef href="b.rng"/></choice></start>'
        ),
        external,
    )


def test_inventory_describes_compiled_patterns_routes_and_resources():
    schema = rich_schema()

    description = describe_schema(schema)

    assert isinstance(description, RelaxNGSchemaDescription)
    assert description.dependencies is schema.grammar.dependencies
    assert description.start_ids == ("pattern:" + pattern_identity(schema.grammar.start),)
    assert tuple(field.name for field in fields(RelaxNGComponentDescription)) == COMPONENT_FIELDS
    component = description.components[0]
    assert not hasattr(component, "global_declaration")
    assert not hasattr(component, "python_name")
    assert not hasattr(component, "particle")

    by_kind: dict[str, list[RelaxNGComponentDescription]] = {}
    for item in description.components:
        by_kind.setdefault(item.kind, []).append(item)
    assert {item.kind for item in description.components} == {
        "grammar",
        "define",
        "pattern",
        "name-class",
        "datatype",
        "route",
        "resource",
    }
    assert len(by_kind["grammar"]) == 1
    assert by_kind["grammar"][0].id == "grammar"
    assert by_kind["grammar"][0].owner_id is None
    assert by_kind["grammar"][0].location is None
    assert len(by_kind["route"]) == len(schema.projection.routes)

    pattern_kinds = {item.pattern_kind for item in by_kind["pattern"]}
    assert {
        "empty",
        "text",
        "choice",
        "group",
        "interleave",
        "oneOrMore",
        "element",
        "attribute",
        "list",
        "data",
        "dataExcept",
        "value",
        "ref",
    } <= pattern_kinds

    element = next(
        item for item in by_kind["pattern"] if item.pattern_kind == "element" and item.name == "doc"
    )
    assert element.namespace is None
    assert element.name_class == 'name(null, "doc")'
    assert element.location is not None
    assert element.origins
    assert element.owner_id == "grammar"
    assert element.synthetic is False

    attribute = next(
        item
        for item in by_kind["pattern"]
        if item.pattern_kind == "attribute" and item.name == "id"
    )
    assert attribute.namespace is None
    assert attribute.name_class == 'name(null, "id")'

    qualified = next(
        item for item in by_kind["pattern"] if item.pattern_kind == "attribute" and item.name == "q"
    )
    assert qualified.namespace == "urn:one"

    datatypes = by_kind["datatype"]
    integer = next(
        item for item in datatypes if item.name == "integer" and not item.datatype_parameters
    )
    assert integer.datatype_library == XSD
    assert integer.datatype_name == "integer"
    assert integer.datatype_parameters == ()
    assert integer.namespace is None

    definitions = {item.name: item for item in by_kind["define"]}
    assert set(definitions) == set(schema.grammar.definitions)
    assert all(item.synthetic for item in definitions.values())
    assert all(item.id == "define:" + item.name for item in definitions.values())
    assert all(item.name.startswith("element") for item in definitions.values())

    routes = {item.id: item for item in by_kind["route"]}
    assert set(routes) == set(schema.projection.routes)
    root_route = schema.projection.routes[schema.projection.roots[0]]
    described = routes[root_route.route_id]
    assert described.name == root_route.expanded_name == "doc"
    assert described.namespace is None
    assert described.name_class == 'name(null, "doc")'
    assert described.origins == root_route.origins
    assert described.owner_id == "grammar"

    resources = by_kind["resource"]
    assert resources
    assert all(item.id.startswith("resource:") for item in resources)
    assert all(item.owner_id == "grammar" for item in resources)


def test_components_are_sorted_by_kind_rank_then_id():
    description = describe_schema(rich_schema())
    rank = {
        kind: index
        for index, kind in enumerate(
            ("grammar", "define", "pattern", "name-class", "datatype", "route", "resource")
        )
    }
    expected = sorted(description.components, key=lambda item: (rank[item.kind], item.id))
    assert list(description.components) == expected
    assert len({item.id for item in description.components}) == len(description.components)


def test_authored_define_names_do_not_survive_compilation():
    schema = rich_schema()
    description = describe_schema(schema)

    assert [item.name for item in find_components(schema, kind="define")] == sorted(
        schema.grammar.definitions
    )
    assert "define:doc" not in {item.id for item in description.components}
    assert "doc" not in {item.name for item in find_components(schema, kind="define")}
    for name, definition in schema.grammar.definitions.items():
        assert name.startswith("element")
        assert definition.origins
        described = describe_component(schema, f"define:{name}")
        assert described.synthetic is True
        assert described.name == name
        assert described.origins == definition.origins
        assert described.location is not None


def test_stable_ids_survive_relocation_and_equivalent_recompilation(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    xml = grammar(rich_body())
    first = compile_xml(xml, path=first_dir / "schema.rng")
    second = compile_xml(xml, path=second_dir / "schema.rng")
    first.require_valid()
    second.require_valid()

    first_description = describe_schema(first)
    second_description = describe_schema(second)
    first_ids = {item.id for item in first_description.components}
    second_ids = {item.id for item in second_description.components}

    assert first_ids == second_ids
    assert first_description.start_ids == second_description.start_ids
    assert ids(first, kind="pattern") == ids(second, kind="pattern")
    assert ids(first, kind="define") == ids(second, kind="define")
    assert ids(first, kind="name-class") == ids(second, kind="name-class")
    assert ids(first, kind="datatype") == ids(second, kind="datatype")
    assert ids(first, kind="route") == ids(second, kind="route")
    assert ids(first, kind="resource") == ids(second, kind="resource")

    first_element = next(
        item
        for item in find_components(first, kind="pattern", text="doc")
        if item.pattern_kind == "element"
    )
    second_element = describe_component(second, first_element.id)
    assert first_element.location is not None and second_element.location is not None
    assert first_element.location.source != second_element.location.source
    assert first_element.name == second_element.name

    recompiled = describe_schema(compile_xml(xml))
    assert {item.id for item in recompiled.components} == first_ids


def test_name_classes_use_order_independent_canonical_presentation():
    first = compiled_element(
        "<attribute><choice><name>zeta</name><name>alpha</name></choice><text/></attribute>"
    )
    second = compiled_element(
        "<attribute><choice><name>alpha</name><name>zeta</name></choice><text/></attribute>"
    )

    first_choice = next(
        item
        for item in find_components(first, kind="name-class")
        if item.name_class is not None and item.name_class.startswith("choice(")
    )
    second_choice = describe_component(second, first_choice.id)
    assert first_choice.name_class == 'choice(name(null, "alpha"), name(null, "zeta"))'
    assert second_choice.name_class == first_choice.name_class
    assert (
        first_choice.id
        == "name-class:" + sha256(first_choice.name_class.encode("utf-8")).hexdigest()
    )

    except_schema = compiled_element(
        "<zeroOrMore><attribute><anyName>"
        "<except><name>skip</name></except>"
        "</anyName><text/></attribute></zeroOrMore>"
    )
    except_class = next(
        item
        for item in find_components(except_schema, kind="name-class")
        if item.name_class is not None and item.name_class.startswith("except(")
    )
    assert except_class.name_class == 'except(anyName, name(null, "skip"))'

    ns_name_schema = compiled('<start><element><nsName ns="urn:any"/><text/></element></start>')
    ns_name = next(
        item
        for item in find_components(ns_name_schema, kind="name-class")
        if item.name_class == 'nsName("urn:any")'
    )
    assert ns_name.namespace == "urn:any"
    assert ns_name.name is None


def test_nested_choice_and_except_name_classes_have_components_and_edges():
    schema = compiled_element(
        "<zeroOrMore><attribute><anyName><except>"
        "<choice><name>a</name><name>b</name></choice>"
        "</except></anyName><text/></attribute></zeroOrMore>"
    )
    inner = next(
        item
        for item in find_components(schema, kind="name-class")
        if item.name_class == 'choice(name(null, "a"), name(null, "b"))'
    )
    outer = next(
        item
        for item in find_components(schema, kind="name-class")
        if item.name_class == "except(anyName, " + inner.name_class + ")"
    )
    assert outer.namespace is None
    assert outer in related_components(schema, inner.id, direction="used_by", depth=1)
    assert inner in related_components(schema, outer.id, direction="uses", depth=1)


def test_datatype_components_normalize_parameters_and_fail_without_metadata():
    first = compiled_element(
        '<data type="integer" datatypeLibrary="' + XSD + '"><param name="minInclusive">1</param>'
        '<param name="maxInclusive">9</param><except><value>5</value></except>'
        "</data>"
    )
    second = compiled_element(
        '<data type="integer" datatypeLibrary="' + XSD + '"><param name="maxInclusive">9</param>'
        '<param name="minInclusive">1</param><except><value>5</value></except>'
        "</data>"
    )
    datatype = next(
        item for item in find_components(first, kind="datatype") if item.name == "integer"
    )
    assert datatype.name == "integer"
    assert datatype.datatype_library == XSD
    assert datatype.datatype_name == "integer"
    assert datatype.datatype_parameters == (("maxInclusive", "9"), ("minInclusive", "1"))
    described_data = next(
        item for item in find_components(first, kind="pattern") if item.pattern_kind == "dataExcept"
    )
    assert described_data.datatype_name is None
    assert described_data.datatype_library is None
    assert (
        describe_component(second, datatype.id).datatype_parameters == datatype.datatype_parameters
    )
    assert datatype.origins == described_data.origins
    assert datatype.location == described_data.location

    datatype_node = next(
        node
        for node in _patterns(first.grammar.start)
        if isinstance(node, (Data, DataExcept, Value))
        and getattr(node.datatype, "type_name", None) == "integer"
        and getattr(node.datatype, "parameters", ())
    )
    assert (
        datatype.id == "datatype:" + scalar_contract(Data(datatype_node.datatype)).pattern_identity
    )

    class NamelessDatatype(Datatype):
        def allows(self, lexical: str, context: DatatypeContext) -> bool:
            return lexical == "ok"

        def equal(
            self,
            left: str,
            left_context: DatatypeContext,
            right: str,
            right_context: DatatypeContext,
        ) -> bool:
            return left == right

    class NamelessFactory(DatatypeFactory):
        def create(
            self,
            type_name: str,
            parameters: tuple[DatatypeParameter, ...],
            context: DatatypeContext,
        ) -> Datatype:
            del type_name, parameters, context
            return NamelessDatatype()

    opaque = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="doc">'
            '<data type="thing" datatypeLibrary="urn:nameless"/></element>'
        ),
        datatype_libraries={
            "urn:nameless": DatatypeLibraryRegistration(NamelessFactory(), "nameless/1")
        },
    )
    assert not opaque.report.has_errors
    with pytest.raises(UnsupportedDatatypeOperation):
        describe_schema(opaque)


def _patterns(start):
    from pyxsd.relaxng.patterns import Pattern, Ref

    pending = [start]
    seen = set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        if isinstance(node, Ref):
            pending.append(node.target)
            continue
        for field in fields(node):
            value = getattr(node, field.name)
            if isinstance(value, Pattern):
                pending.append(value)


def test_route_edges_reach_children_scalars_and_wildcards():
    schema = compiled_element(
        '<attribute name="id"><text/></attribute>'
        "<zeroOrMore><attribute><anyName><except><name>id</name></except>"
        "</anyName>"
        f'<data type="integer" datatypeLibrary="{XSD}"/></attribute></zeroOrMore>'
        "<choice>"
        '<element name="a"><text/></element>'
        f'<element name="b"><data type="boolean" datatypeLibrary="{XSD}"/></element>'
        "</choice>"
    )
    plan = schema.projection
    root = plan.routes[plan.roots[0]]
    root_component = describe_component(schema, root.route_id)
    assert root_component.name == "doc"

    child_ids = {field.child_route_id for field in root.children.values() if field.child_route_id}
    assert child_ids == {root.children["a"].child_route_id, root.children["b"].child_route_id}
    uses = related_components(schema, root.route_id, direction="uses", depth=1)
    uses_ids = {component.id for component in uses}
    assert child_ids <= uses_ids

    text_ids = {
        component.id
        for component in find_components(schema, kind="pattern", text="text")
        if component.pattern_kind == "text"
    }
    assert text_ids and text_ids <= uses_ids
    assert root.wildcards
    for wildcard in root.wildcards:
        assert wildcard.child_route_id is None
        assert wildcard.scalar_contracts
        for contract in wildcard.scalar_contracts:
            assert all(
                contract not in field.scalar_contracts
                for field in (*root.attributes.values(), *root.children.values(), root.content)
            )
            encoding = plan.encoding_patterns[contract.pattern_identity]
            assert "pattern:" + pattern_identity(encoding) in uses_ids
    assert len(uses_ids) == len({component.id for component in uses})


def test_resources_deduplicate_content_but_keep_every_dependency(tmp_path):
    root_xml, external = resource_xml(tmp_path / "schemas")
    schema = compile_xml(root_xml, path=tmp_path / "schemas" / "root.rng")
    schema.require_valid()
    description = describe_schema(schema)

    assert len(schema.dependencies) == 3
    assert description.dependencies == schema.grammar.dependencies
    resources = find_components(schema, kind="resource")
    assert len(resources) == 2
    digest = sha256(external.encode("utf-8")).hexdigest()
    external_component = describe_component(schema, f"resource:schema:{digest}")
    assert external_component.name == str((tmp_path / "schemas" / "a.rng").resolve())
    assert external_component.location is not None
    assert external_component.location.source.endswith("root.rng")
    assert len(external_component.origins) == 2
    references = [
        reference
        for dependency in description.dependencies
        if dependency.content_sha256 == digest
        for reference in dependency.references
    ]
    assert {origin.location for origin in external_component.origins} == {
        reference.location for reference in references
    }
    assert external_component.location == references[0].location
    root_component = next(
        component for component in resources if component.id != external_component.id
    )
    assert root_component.location is None
    assert len(root_component.origins) == 0
    dependency_ids = {dependency.canonical_id for dependency in description.dependencies}
    assert str((tmp_path / "schemas" / "a.rng").resolve()) in dependency_ids
    assert str((tmp_path / "schemas" / "b.rng").resolve()) in dependency_ids
    grammar_component = describe_component(schema, "grammar")
    assert external_component.id in {
        component.id
        for component in related_components(schema, grammar_component.id, direction="uses", depth=1)
    }


def test_resource_ids_are_stable_after_relocation(tmp_path):
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    first_xml, _ = resource_xml(first_dir)
    second_xml, _ = resource_xml(second_dir)
    first = compile_xml(first_xml, path=first_dir / "root.rng")
    second = compile_xml(second_xml, path=second_dir / "root.rng")
    first.require_valid()
    second.require_valid()

    first_ids = ids(first, kind="resource")
    second_ids = ids(second, kind="resource")
    assert first_ids == second_ids
    assert ids(first, kind="pattern") == ids(second, kind="pattern")
    first_resource = next(
        component
        for component in find_components(first, kind="resource")
        if component.location is not None
    )
    second_resource = describe_component(second, first_resource.id)
    assert first_resource.location is not None and second_resource.location is not None
    assert first_resource.location.source != second_resource.location.source
    assert second_resource.name != first_resource.name


@pytest.mark.parametrize("first_location", [None, IssueLocation(source="z-location", path=())])
def test_resource_origins_include_unlocated_sites_and_keep_first_reference_location(first_location):
    schema = compiled_element("<empty/>")
    references = (
        ResourceReference("target", "container", "a", None, None, location=first_location),
        ResourceReference(
            "target",
            "container",
            "b",
            None,
            None,
            location=IssueLocation(source="a-location", path=()),
        ),
        ResourceReference("target", "other", "c", None, None),
    )
    dependency = ResourceDependency("target", "target", "schema", "digest", references)
    schema = replace(schema, grammar=replace(schema.grammar, dependencies=(dependency,)))
    (resource,) = find_components(schema, kind="resource")
    assert resource.location == first_location
    assert resource.origins == OriginTable(
        frozenset(
            SourceOrigin(reference.containing_id, reference.location) for reference in references
        )
    )
    assert resource.synthetic is False


def test_names_distinguish_local_pattern_names_from_expanded_route_names():
    schema = compile_xml(f'<element xmlns="{NS}" name="doc" ns="urn:doc"><empty/></element>')
    schema.require_valid()
    (route,) = find_components(schema, kind="route")
    pattern = next(
        component
        for component in find_components(schema, kind="pattern")
        if component.pattern_kind == "element"
    )
    (name_class,) = find_components(schema, kind="name-class")
    assert route.name == "{urn:doc}doc"
    assert pattern.name == name_class.name == "doc"
    assert route.namespace == pattern.namespace == name_class.namespace == "urn:doc"
    assert route.owner_id == pattern.owner_id == name_class.owner_id == "grammar"
    assert route.synthetic is pattern.synthetic is name_class.synthetic is False


def test_invalid_schema_describes_grammar_patterns_and_dependencies_without_routes():
    schema = compile_xml(grammar('<start><ref name="missing"/></start>'))
    assert schema.report.has_errors
    before = tuple((issue.code, issue.message, issue.location) for issue in schema.report.errors)

    description = describe_schema(schema)

    assert (
        tuple((issue.code, issue.message, issue.location) for issue in schema.report.errors)
        == before
    )
    assert schema.report.has_errors
    assert description.start_ids == ("pattern:" + pattern_identity(schema.grammar.start),)
    assert find_components(schema, kind="route") == ()
    assert find_components(schema, kind="grammar")
    assert any(
        component.pattern_kind == "notAllowed"
        for component in find_components(schema, kind="pattern")
    )
    assert description.dependencies == schema.dependencies
    with pytest.raises(ValidationError):
        _ = schema.projection

    composed = compile_xml(
        grammar(
            '<start><element name="doc">'
            '<attribute name="a"><text/></attribute>'
            '<attribute name="a"><text/></attribute>'
            "<empty/></element></start>"
        )
    )
    assert composed.report.has_errors
    assert composed.grammar.definitions
    composed_description = describe_schema(composed)
    assert find_components(composed, kind="route") == ()
    assert find_components(composed, kind="define")
    assert any(component.pattern_kind == "element" for component in composed_description.components)
    with pytest.raises(ValidationError):
        _ = composed.projection


def test_find_components_filters_by_literal_text_kind_and_namespace():
    schema = rich_schema()
    description = describe_schema(schema)

    assert find_components(schema) == description.components
    assert find_components(schema, kind="grammar") == (describe_component(schema, "grammar"),)
    assert find_components(schema, kind="absent") == ()
    assert find_components(schema, text="INTEGER")
    assert all(
        "integer"
        in (
            component.id
            + " "
            + (component.name or "")
            + " "
            + (component.namespace or "")
            + " "
            + (component.pattern_kind or "")
            + " "
            + (component.name_class or "")
            + " "
            + (component.datatype_library or "")
            + " "
            + (component.datatype_name or "")
        ).casefold()
        for component in find_components(schema, text="integer")
    )
    assert find_components(schema, text=".*") == ()
    assert find_components(schema, text="[") == ()

    unqualified = find_components(schema, namespace="")
    assert unqualified
    assert all((component.namespace or "") == "" for component in unqualified)
    assert any(component.pattern_kind == "element" for component in unqualified)
    qualified = find_components(schema, namespace="urn:one")
    assert qualified
    assert all(component.namespace == "urn:one" for component in qualified)
    assert {component.pattern_kind for component in qualified} <= {"attribute", None}
    assert find_components(schema, namespace="urn:absent") == ()


def test_illegal_reference_chain_fails_explicitly():
    first = Ref()
    second = Ref()
    first.bind(second)
    second.bind(first)
    schema = RelaxNGSchema(
        report=ValidationReport(),
        grammar=CompiledGrammar(first, MappingProxyType({}), PatternPool(), ()),
        namespace_context=NamespaceContext(),
        limits=RelaxNGLimits(),
        datatype_fingerprint="pyxsd.relaxng.datatypes/1:test",
    )
    with pytest.raises(ValueError, match="reference chain"):
        describe_schema(schema)

    cycle = Group(Empty(), Empty())
    object.__setattr__(cycle, "first", cycle)
    schema = replace(schema, grammar=replace(schema.grammar, start=cycle))
    with pytest.raises(ValueError, match="cyclic RELAX NG pattern outside a bound reference"):
        describe_schema(schema)


def test_describe_component_accepts_only_exact_component_ids():
    schema = rich_schema()
    root_route = schema.projection.routes[schema.projection.roots[0]]
    component = describe_component(schema, root_route.route_id)
    assert component.id == root_route.route_id
    with pytest.raises(KeyError):
        describe_component(schema, "doc")
    with pytest.raises(KeyError):
        describe_component(schema, "pattern:" + pattern_identity(schema.grammar.start) + "x")


def test_related_components_is_breadth_first_and_depth_bounded():
    schema = rich_schema()
    start_id = describe_schema(schema).start_ids[0]
    grammar_id = "grammar"

    assert related_components(schema, grammar_id, direction="uses", depth=0) == ()
    one = related_components(schema, grammar_id, direction="uses", depth=1)
    two = related_components(schema, grammar_id, direction="uses", depth=2)
    assert start_id in {component.id for component in one}
    start_component = describe_component(schema, start_id)
    assert start_component.pattern_kind == "ref"
    target = related_components(schema, start_id, direction="uses", depth=1)
    target_ids = {component.id for component in target}
    assert target_ids
    assert {component.id for component in one} <= {component.id for component in two}
    assert target_ids <= {component.id for component in two}

    definition_ids = ids(schema, kind="define")
    assert definition_ids <= {component.id for component in one}
    root = schema.projection.routes[schema.projection.roots[0]]
    child = schema.projection.routes[root.children["leaf"].child_route_id]
    uses = related_components(schema, root.route_id, direction="uses", depth=1)
    assert child.route_id in {component.id for component in uses}
    reverse = related_components(schema, child.route_id, direction="used_by", depth=1)
    assert root.route_id in {component.id for component in reverse}
    assert all(
        len([component.id for component in result]) == len({component.id for component in result})
        for result in (one, two, uses, reverse)
    )


def test_related_components_terminates_on_recursive_grammars(tmp_path):
    body = (
        '<start><ref name="node"/></start>'
        '<define name="node"><element name="node">'
        '<optional><ref name="node"/></optional>'
        "</element></define>"
    )
    schema = compiled(body)
    start_id = describe_schema(schema).start_ids[0]
    assert describe_component(schema, start_id).pattern_kind == "ref"
    uses = related_components(schema, start_id, direction="uses", depth=100)
    ids_seen = [component.id for component in uses]
    assert start_id not in ids_seen
    assert len(ids_seen) == len(set(ids_seen))
    assert any(component.pattern_kind == "element" for component in uses)
    assert any(component.pattern_kind == "choice" for component in uses)
    reverse = related_components(schema, start_id, direction="used_by", depth=100)
    reverse_ids = [component.id for component in reverse]
    assert len(reverse_ids) == len(set(reverse_ids))
    assert start_id not in reverse_ids

    first_dir = tmp_path / "recursive-one"
    second_dir = tmp_path / "recursive-two"
    first_dir.mkdir()
    second_dir.mkdir()
    xml = grammar(body)
    first = compile_xml(xml, path=first_dir / "schema.rng")
    second = compile_xml(xml, path=second_dir / "schema.rng")
    first.require_valid()
    second.require_valid()
    assert ids(first, kind="pattern") == ids(second, kind="pattern")
    assert ids(first, kind="pattern") == {
        "pattern:" + pattern_identity(node) for node in _patterns(first.grammar.start)
    }


@pytest.mark.parametrize("multi_anchor", [False, True])
def test_shared_interior_recursion_uses_one_content_component(tmp_path, multi_anchor):
    snapshots = []
    for index, reverse in enumerate((False, True, False)):
        schema = compile_xml(
            shared_interior_schema(multi_anchor=multi_anchor, reverse=reverse),
            path=tmp_path / f"relocated-{index}.rng",
        )
        schema.require_valid()
        elements = [definition.pattern for definition in schema.grammar.definitions.values()]
        shared = elements[0].pattern
        assert all(element.pattern is shared for element in elements)
        shared_id = "pattern:" + pattern_identity(shared)
        description = describe_schema(schema)
        matches = find_components(schema, kind="pattern", text=shared_id)
        assert len(matches) == 1
        assert describe_component(schema, shared_id) == matches[0]
        assert matches[0].origins == schema.grammar.graph.origins(shared)
        reordered = replace(
            schema,
            grammar=replace(
                schema.grammar,
                definitions=MappingProxyType(
                    dict(reversed(tuple(schema.grammar.definitions.items())))
                ),
            ),
        )
        assert describe_schema(reordered) == description
        assert grammar_fingerprint(schema.grammar, schema.datatype_fingerprint) == (
            grammar_fingerprint(reordered.grammar, reordered.datatype_fingerprint)
        )
        assert {
            component.id for component in description.components if component.kind == "pattern"
        } == {"pattern:" + pattern_identity(node) for node in _patterns(schema.grammar.start)}
        for direction in ("uses", "used_by"):
            related = related_components(schema, shared_id, direction=direction, depth=100)
            assert related
            assert shared_id not in {component.id for component in related}
            assert len(related) == len({component.id for component in related})
            assert related == related_components(schema, shared_id, direction=direction, depth=101)
        assert schema.parse(io.StringIO("<a><c><c/></c></a>"), typed=True).is_valid
        snapshots.append(tuple(component.id for component in description.components))
    assert snapshots[0] == snapshots[2]

    # Source-content digests intentionally change for reordered XML, so
    # resources and grammar-fingerprint-based routes are not semantic IDs.
    def semantic(snapshot):
        return {
            component_id
            for component_id in snapshot
            if not component_id.startswith(("resource:", "pyxsd.relaxng.route/"))
        }

    assert semantic(snapshots[0]) == semantic(snapshots[1])


def test_datatype_origins_union_data_value_and_except_sites():
    schema = compiled_element(
        '<choice><element name="a"><data type="token"/></element>'
        '<element name="b"><value>fixed</value></element>'
        '<element name="c"><data type="token"><except><value>no</value></except>'
        "</data></element></choice>"
    )
    expected = schema.grammar.graph.origins(
        next(node for node in _patterns(schema.grammar.start) if isinstance(node, Data))
    )
    for node in _patterns(schema.grammar.start):
        if isinstance(node, (Data, Value, DataExcept)):
            expected = expected.union(schema.grammar.graph.origins(node))
    (datatype,) = find_components(schema, kind="datatype")
    assert datatype.origins == expected
    assert len(expected) >= 4
    located = [origin for origin in expected if origin.location is not None]
    primary = min(
        located,
        key=lambda origin: (
            origin.source_id,
            origin.location.source or "",
            tuple((step.name, step.index) for step in origin.location.path),
            origin.location.attribute or "",
        ),
    )
    assert datatype.location == primary.location


def test_related_components_matches_xsd_errors():
    schema = rich_schema()
    with pytest.raises(ValueError, match="direction must be uses or used_by"):
        related_components(schema, "grammar", direction="other", depth=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="depth must be non-negative"):
        related_components(schema, "grammar", direction="uses", depth=-1)
    with pytest.raises(KeyError):
        related_components(schema, "absent", direction="uses", depth=1)
    with pytest.raises(KeyError):
        related_components(schema, "absent", direction="uses", depth=0)


def test_descriptions_are_immutable_and_stable_across_instance_operations():
    schema = compiled_element("<text/>")
    first = describe_schema(schema)
    second = describe_schema(schema)
    assert first == second
    with pytest.raises(FrozenInstanceError):
        first.start_ids = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.components[0].id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.dependencies = ()  # type: ignore[misc]

    document = schema.parse(io.StringIO("<doc>hello</doc>"))
    assert not document.report.has_errors
    after = describe_schema(schema)
    assert after == first
    assert {component.id for component in after.components} == {
        component.id for component in first.components
    }
    assert describe_component(schema, after.start_ids[0]).pattern_kind == "ref"
