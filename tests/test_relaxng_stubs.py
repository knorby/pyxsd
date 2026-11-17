"""RELAX NG stub emission: exact goldens, runtime agreement, and filtering."""

from __future__ import annotations

import ast
import decimal
import io
import os
import re
import subprocess
import sys
from collections.abc import MutableSequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pyxsd import xsd_data_types
from pyxsd.facets import FacetConstraints
from pyxsd.relaxng import AlternativeValue, RelaxNGElement
from pyxsd.relaxng import stubs as stub_module
from pyxsd.relaxng.binding import (
    WILDCARD_CHILDREN,
    AttributeDescriptor,
    ChildDescriptor,
    ScalarDescriptor,
)
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeLibraryRegistration,
    ScalarDatatype,
)
from pyxsd.relaxng.introspection import describe_schema
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.stubs import emit_relaxng_stub

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
FIXTURES = Path(__file__).parent / "relaxng" / "fixtures" / "stubs"
NAMES = ("stable", "recursive", "wildcard", "generic", "alternatives", "cardinality")

INSTANCES = {
    "stable": '<doc count="1" label="hi"><note>n</note><item id="2"/><item id="3"/></doc>',
    "recursive": '<node name="a"><node name="b"><node/></node></node>',
    "wildcard": '<doc id="1"><known/><extra>hello</extra><other>world</other></doc>',
    "generic": "<doc><bad><nested/></bad><good>text</good></doc>",
    "alternatives": (
        '<doc value="12" maybe="word"><numbers>1 2</numbers>'
        "<fixed>3 x</fixed><loose>4</loose></doc>"
    ),
    "cardinality": '<doc maybe="true"><single/><many/><many/><requiredMany/></doc>',
}


def compile_fixture(name: str) -> RelaxNGSchema:
    return RelaxNGSchema.compile(FIXTURES / f"{name}.rng")


def compile_body(body: str, *, libraries: dict[str, DatatypeLibraryRegistration] | None = None):
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="doc">{body}</element>'),
        datatype_libraries=libraries,
    )
    schema.require_valid()
    return schema


def classes_by_name(schema: RelaxNGSchema) -> dict[str, tuple[str, type[Any]]]:
    return {
        schema.projection_classes.class_for(route_id).__name__: (
            route_id,
            schema.projection_classes.class_for(route_id),
        )
        for route_id, route in schema.projection.routes.items()
        if route.generic_reason is None
    }


# ---------------------------------------------------------------------------
# Exact goldens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize(("compact", "suffix"), [(False, ".pyi"), (True, ".compact")])
def test_exact_golden(name, compact, suffix):
    schema = compile_fixture(name)
    expected = (FIXTURES / f"{name}{suffix}").read_text(encoding="utf-8")
    assert emit_relaxng_stub(schema, compact=compact) == expected


def _assert_full_annotation_spelling(text: str) -> None:
    tree = ast.parse(text)
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    module_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            module_names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                module_names.add(target.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            fields = {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
            for item in node.body:
                if not isinstance(item, ast.AnnAssign):
                    continue
                names = {
                    child.id for child in ast.walk(item.annotation) if isinstance(child, ast.Name)
                }
                assert not (names & fields)
                assert all(name.startswith("_") for name in names)
                assert names <= module_names
        elif isinstance(node, ast.AnnAssign):
            names = {child.id for child in ast.walk(node.annotation) if isinstance(child, ast.Name)}
            assert all(name.startswith("_") for name in names)
            assert names <= module_names


@pytest.mark.parametrize("name", NAMES)
def test_full_stub_is_structurally_valid_python(name):
    text = emit_relaxng_stub(compile_fixture(name))
    tree = ast.parse(text)
    assert any(isinstance(node, ast.ClassDef) for node in tree.body)
    _assert_full_annotation_spelling(text)
    namespace: dict[str, object] = {}
    exec(
        compile(text, f"<relaxng-stub:{name}>", "exec", dont_inherit=True),
        namespace,
    )
    assert any(value is RelaxNGElement for value in namespace.values())


# ---------------------------------------------------------------------------
# Runtime agreement
# ---------------------------------------------------------------------------


def _split_annotation(annotation: str) -> list[str]:
    arms: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(annotation):
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif character == "|" and depth == 0:
            arms.append(annotation[start:index].strip())
            start = index + 1
    arms.append(annotation[start:].strip())
    return arms


def _private(annotation: str, stem: str) -> bool:
    return annotation == stem or annotation.startswith(f"{stem}_")


def _matches(value: Any, annotation: str, generated: dict[str, type[Any]]) -> bool:
    arms = _split_annotation(annotation)
    if len(arms) > 1:
        return any(_matches(value, arm, generated) for arm in arms)
    if annotation == "None":
        return value is None
    if _private(annotation, "_any"):
        return True
    if _private(annotation, "_alt"):
        return isinstance(value, AlternativeValue)
    if _private(annotation, "_rng"):
        return isinstance(value, RelaxNGElement)
    builtin_match = re_match_builtin(annotation, "str")
    if builtin_match:
        return isinstance(value, str)
    builtin_match = re_match_builtin(annotation, "bool")
    if builtin_match:
        return isinstance(value, bool)
    builtin_match = re_match_builtin(annotation, "bytes")
    if builtin_match:
        return isinstance(value, bytes)
    builtin_match = re_match_builtin(annotation, "int")
    if builtin_match:
        return isinstance(value, int)
    builtin_match = re_match_builtin(annotation, "float")
    if builtin_match:
        return isinstance(value, float)
    if re_match_private_module(annotation, "decimal") == "Decimal":
        return isinstance(value, decimal.Decimal)
    if annotation.startswith("_xsd."):
        xsd_class = getattr(xsd_data_types, annotation.removeprefix("_xsd."))
        return isinstance(value, xsd_class)
    item_annotation = _private_sequence_inner(annotation, "_mutable")
    if item_annotation is not None:
        return isinstance(value, MutableSequence) and all(
            _matches(item, item_annotation, generated) for item in value
        )
    item_annotation = _builtin_sequence_inner(annotation, "list")
    if item_annotation is not None:
        return isinstance(value, list) and all(
            _matches(item, item_annotation, generated) for item in value
        )
    tuple_inner = _builtin_sequence_inner(annotation, "tuple")
    if tuple_inner is not None:
        return _matches_tuple(value, tuple_inner, generated)
    if annotation in generated:
        return isinstance(value, generated[annotation])
    raise AssertionError(f"unhandled stub annotation {annotation!r}")


def re_match_builtin(annotation: str, name: str) -> bool:
    return re.fullmatch(rf"_builtins(?:_\d+)?\.{re.escape(name)}", annotation) is not None


def re_match_private_module(annotation: str, module: str) -> str | None:
    match = re.fullmatch(rf"_{re.escape(module)}(?:_\d+)?\.([A-Za-z_][A-Za-z0-9_]*)", annotation)
    return match.group(1) if match is not None else None


def _private_sequence_inner(annotation: str, stem: str) -> str | None:
    match = re.fullmatch(rf"{re.escape(stem)}(?:_\d+)?\[(.*)\]", annotation)
    return match.group(1) if match is not None else None


def _builtin_sequence_inner(annotation: str, name: str) -> str | None:
    match = re.fullmatch(rf"_builtins(?:_\d+)?\.{re.escape(name)}\[(.*)\]", annotation)
    return match.group(1) if match is not None else None


def _sequence_inner(annotation: str, name: str) -> str | None:
    prefix = f"{name}["
    if not annotation.startswith(prefix) or not annotation.endswith("]"):
        return None
    return annotation[len(prefix) : -1]


def _split_commas(annotation: str) -> list[str]:
    arms: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(annotation):
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif character == "," and depth == 0:
            arms.append(annotation[start:index].strip())
            start = index + 1
    arms.append(annotation[start:].strip())
    return arms


def _matches_tuple(value: Any, inner: str, generated: dict[str, type[Any]]) -> bool:
    if inner == "()":
        return value == ()
    if not isinstance(value, tuple):
        return False
    parts = _split_commas(inner)
    return len(parts) == len(value) and all(
        _matches(item, part, generated) for item, part in zip(value, parts, strict=True)
    )


def _members_from_stub(text: str) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for node in ast.parse(text).body:
        if not isinstance(node, ast.ClassDef):
            continue
        members: dict[str, str] = {}
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                members[statement.target.id] = ast.unparse(statement.annotation)
        result[node.name] = members
    return result


def _type_aliases_from_stub(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in ast.parse(text).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Name)
        ):
            result[node.targets[0].id] = node.value.id
    return result


def _walk(root: RelaxNGElement):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


@pytest.mark.parametrize("name", NAMES)
def test_emitted_surface_matches_runtime_descriptors(name):
    schema = compile_fixture(name)
    text = emit_relaxng_stub(schema)
    emitted = _members_from_stub(text)
    actual = classes_by_name(schema)
    assert set(emitted) == set(actual)

    for class_name, (route_id, cls) in actual.items():
        route = schema.projection.routes[route_id]
        expected = {field.alias for field in (*route.attributes.values(), *route.children.values())}
        scalar_field = cls.__dict__.get("_relaxng_scalar_field_")
        if scalar_field is not None:
            expected.add(scalar_field)
        assert set(emitted[class_name]) == expected
        for field in route.attributes.values():
            assert isinstance(cls.__dict__[field.alias], AttributeDescriptor)
        for field in route.children.values():
            assert isinstance(cls.__dict__[field.alias], ChildDescriptor)
        if scalar_field is not None:
            assert isinstance(cls.__dict__[scalar_field], ScalarDescriptor)

    document = schema.parse(io.StringIO(INSTANCES[name]))
    assert not document.report.has_errors
    generated = {cls.__name__: cls for cls in (item[1] for item in actual.values())}
    for alias, class_name in _type_aliases_from_stub(text).items():
        generated[alias] = actual[class_name][1]
    for node in _walk(document.root):
        for class_name, members in emitted.items():
            if type(node).__name__ != class_name:
                continue
            for member, annotation in members.items():
                value = getattr(node, member)
                assert _matches(value, annotation, generated), (
                    f"{name}.{class_name}.{member}: {value!r} does not match {annotation}"
                )


@pytest.mark.parametrize("name", NAMES)
def test_deterministic_across_repeats_recompiles_and_relocated_paths(name, tmp_path):
    text = (FIXTURES / f"{name}.rng").read_text(encoding="utf-8")
    first = emit_relaxng_stub(compile_fixture(name))
    assert first == emit_relaxng_stub(compile_fixture(name))
    assert first == emit_relaxng_stub(RelaxNGSchema.compile(io.StringIO(text)))
    for directory in ("a", "nested/b"):
        target = tmp_path / directory / "schema.rng"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        assert emit_relaxng_stub(RelaxNGSchema.compile(target)) == first


def test_deterministic_across_hash_seeds():
    script = (
        "import sys\n"
        "from pyxsd.relaxng.schema import RelaxNGSchema\n"
        "from pyxsd.relaxng.stubs import emit_relaxng_stub\n"
        "schema = RelaxNGSchema.compile(sys.argv[1])\n"
        "sys.stdout.write(emit_relaxng_stub(schema))\n"
        "sys.stdout.write('\\0')\n"
        "sys.stdout.write(emit_relaxng_stub(schema, compact=True))\n"
    )
    outputs = []
    for seed in ("0", "1", "12345"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", script, str(FIXTURES / "alternatives.rng")],
            check=True,
            text=True,
            capture_output=True,
            env=environment,
        )
        outputs.append(result.stdout)
    assert len(set(outputs)) == 1


def test_relaxng_stubs_do_not_import_xsd_stub_inventory():
    script = (
        "import sys\n"
        "import pyxsd.relaxng.stubs\n"
        "assert 'pyxsd.stubs' not in sys.modules\n"
        "assert 'pyxsd.introspection' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_public_stub_method_matches_module_and_is_keyword_only():
    schema = compile_fixture("stable")
    assert schema.stub() == emit_relaxng_stub(schema)
    assert schema.stub(compact=True) == emit_relaxng_stub(schema, compact=True)
    selected = (schema.projection.roots[0],)
    assert schema.stub(component_ids=selected, depth=0) == emit_relaxng_stub(
        schema, component_ids=selected, depth=0
    )
    with pytest.raises(TypeError):
        schema.stub(True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Grammar shape and annotation decisions
# ---------------------------------------------------------------------------


def test_one_class_per_stable_route_not_per_define():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f"""<grammar xmlns="{NS}">
            <start><ref name="a"/></start>
            <define name="a"><element name="node"><empty/></element></define>
            <define name="b"><element name="node"><empty/></element></define>
            </grammar>"""
        )
    )
    schema.require_valid()
    assert len(schema.projection.routes) == 1
    text = emit_relaxng_stub(schema)
    assert text.count("class ") == 1
    assert "define" not in text
    assert emit_relaxng_stub(schema, compact=True).count("class ") == 1


def test_recursive_class_is_finite_and_depth_independent():
    schema = compile_fixture("recursive")
    plan = schema.projection
    assert len(plan.routes) == 1
    text = emit_relaxng_stub(schema)
    assert text.count("class ") == 1
    assert "node: _mutable[_t0]" in text
    shallow = schema.parse(io.StringIO("<node/>"))
    deep = schema.parse(io.StringIO("<node><node><node/></node></node>"))
    assert type(shallow.root) is type(deep.root)
    assert type(deep.root.children[0]) is type(deep.root)


def test_wildcard_names_are_not_fabricated():
    schema = compile_fixture("wildcard")
    route = schema.projection.routes[schema.projection.roots[0]]
    assert route.wildcards
    text = emit_relaxng_stub(schema)
    for fabricated in ("extra", "other", "anything"):
        assert f"    {fabricated}:" not in text
    assert "WILDCARD_CHILDREN" in text
    assert "ordered MutableSequence[RelaxNGElement]" in text
    document = schema.parse(io.StringIO(INSTANCES["wildcard"]))
    assert not document.report.has_errors
    wildcard = document.root[WILDCARD_CHILDREN]
    assert len(wildcard) == 2
    assert len({type(child).__name__ for child in wildcard}) == 1


def test_generic_route_is_a_comment_not_a_class():
    schema = compile_fixture("generic")
    generic_route = next(
        route for route in schema.projection.routes.values() if route.generic_reason is not None
    )
    text = emit_relaxng_stub(schema)
    assert f"class {generic_route.expanded_name}" not in text
    assert generic_route.generic_reason in text
    assert generic_route.route_id in text
    assert "bad: _rng" in text


def test_scalar_and_list_annotations_follow_runtime_shapes():
    schema = compile_fixture("alternatives")
    text = emit_relaxng_stub(schema)
    assert "maybe: _alt | None" in text
    assert "value: _alt" in text
    assert "typed_value: _builtins.list[_xsd.Int] | None" in text
    assert "typed_value: _builtins.tuple[_xsd.Int, _xsd.String]" in text
    loose = next(
        route for route in schema.projection.routes.values() if route.expanded_name == "loose"
    )
    assert "the whole-list shape is not a statically plain token sequence" in text
    assert loose.route_id in text
    root = schema.projection.routes[schema.projection.roots[0]]
    root_class = schema.projection_classes.class_for(root.route_id)
    assert root_class.__dict__.get("_relaxng_scalar_field_") is None


def test_attribute_without_scalar_content_is_none():
    schema = compile_body('<attribute name="empty"><empty/></attribute>')
    text = emit_relaxng_stub(schema)
    assert "empty: None" in text
    document = schema.parse(io.StringIO('<doc empty=""/>'))
    assert not document.report.has_errors
    assert document.root.empty is None


def test_empty_plain_list_is_an_empty_tuple():
    schema = compile_body("<element name='items'><list><empty/></list></element>")
    text = emit_relaxng_stub(schema)
    assert "typed_value: _builtins.tuple[()]" in text


def test_scalar_descriptor_includes_none_only_when_absent_text_is_valid():
    string_schema = compile_body(f'<data type="string" datatypeLibrary="{XSD}"/>')
    assert "typed_value: _xsd.String | None" in emit_relaxng_stub(string_schema)
    integer_schema = compile_body(f'<data type="integer" datatypeLibrary="{XSD}"/>')
    integer_text = emit_relaxng_stub(integer_schema)
    assert "typed_value: _xsd.Integer" in integer_text
    assert "typed_value: _xsd.Integer | None" not in integer_text
    optional = compile_body(f'<optional><data type="integer" datatypeLibrary="{XSD}"/></optional>')
    assert "typed_value: _xsd.Integer | None" in emit_relaxng_stub(optional)
    text = compile_body("<text/>")
    assert "typed_value: _alt | None" in emit_relaxng_stub(text)


def test_scalar_optionality_helpers_cover_model_and_list_shapes():
    from pyxsd.relaxng.patterns import Data as DataPattern
    from pyxsd.relaxng.patterns import Ref
    from pyxsd.relaxng.patterns import Text as TextPattern

    schema = compile_fixture("alternatives")
    plan = schema.projection
    patterns = [
        pattern
        for pattern in plan.encoding_patterns.values()
        if isinstance(pattern, stub_module.List)
    ]
    pattern = next(
        item
        for item in patterns
        if (items := stub_module._list_items(item)) is not None and items.homogeneous
    )
    assert stub_module._list_allows_empty(pattern.pattern)
    empty = stub_module.Empty()
    text = TextPattern()
    data = next(
        value for value in plan.encoding_patterns.values() if isinstance(value, DataPattern)
    )
    assert stub_module._list_allows_empty(stub_module.Choice(empty, data))
    assert not stub_module._list_allows_empty(data)
    assert stub_module._list_allows_empty(stub_module.OneOrMore(empty))
    assert stub_module._list_allows_empty(stub_module.Group(empty, empty))
    assert not stub_module._list_allows_empty(stub_module.Group(empty, data))
    assert stub_module._list_allows_empty(text) is False
    ref = Ref()
    ref.bind(empty)
    assert stub_module._list_allows_empty(ref)


# ---------------------------------------------------------------------------
# Filtering and dependency depth
# ---------------------------------------------------------------------------


def test_route_filter_depth_zero_excludes_child_classes_truthfully():
    schema = compile_fixture("stable")
    root_id = schema.projection.roots[0]
    root = schema.projection.routes[root_id]
    note_id = root.children["note"].child_route_id
    assert note_id is not None
    text = emit_relaxng_stub(schema, component_ids=(root_id,), depth=0)
    assert "class Doc(" in text and "class Note(" not in text
    assert "note: _any" in text
    reason = next(line for line in text.splitlines() if note_id in line and "depth=0" in line)
    assert "component selection" in reason


def test_route_filter_depth_one_and_all_follow_uses():
    schema = compile_fixture("stable")
    root_id = schema.projection.roots[0]
    root = schema.projection.routes[root_id]
    note_id = root.children["note"].child_route_id
    item_id = root.children["item"].child_route_id
    depth_one = emit_relaxng_stub(schema, component_ids=(root_id,), depth=1)
    assert "class Note(" in depth_one and "class Item(" in depth_one
    all_text = emit_relaxng_stub(schema, component_ids=(root_id,), depth=None)
    assert all_text == emit_relaxng_stub(schema)
    assert note_id in all_text and item_id in all_text


def test_non_route_seed_is_not_a_class():
    schema = compile_fixture("recursive")
    start_id = describe_schema(schema).start_ids[0]
    text = emit_relaxng_stub(schema, component_ids=(start_id,), depth=None)
    assert "class " not in text
    assert "no projection routes" in text
    define_id = next(
        component.id
        for component in describe_schema(schema).components
        if component.kind == "define"
    )
    define_text = emit_relaxng_stub(schema, component_ids=(define_id,), depth=None)
    assert "class " not in define_text


def test_grammar_seed_reaches_routes_without_inventing_a_class():
    schema = compile_fixture("stable")
    text = emit_relaxng_stub(schema, component_ids=("grammar",), depth=None)
    assert "class Doc(" in text and "class Note(" in text
    assert "class Grammar" not in text


def test_unknown_ids_negative_depth_and_empty_selection():
    schema = compile_fixture("stable")
    with pytest.raises(KeyError):
        emit_relaxng_stub(schema, component_ids=("route:missing",))
    with pytest.raises(ValueError, match="depth"):
        emit_relaxng_stub(schema, depth=-1)
    empty = emit_relaxng_stub(schema, component_ids=())
    assert "class " not in empty
    assert "doc: type[" not in empty
    assert schema.projection.roots[0] in empty
    assert "no projection routes" in empty
    compact = emit_relaxng_stub(schema, compact=True, component_ids=())
    assert "class " not in compact
    assert "type[" not in compact
    assert "aliases: none" in compact


def test_filtering_is_order_independent_and_never_mutates_plan():
    schema = compile_fixture("stable")
    root = schema.projection.routes[schema.projection.roots[0]]
    ids = tuple(field.child_route_id for field in root.children.values())
    assert all(item is not None for item in ids)
    before = tuple(schema.projection.routes)
    first = emit_relaxng_stub(schema, component_ids=ids, depth=None)  # type: ignore[arg-type]
    second = emit_relaxng_stub(schema, component_ids=tuple(reversed(ids)), depth=None)  # type: ignore[arg-type]
    assert first == second
    assert tuple(schema.projection.routes) == before


def test_recursive_filter_terminates_and_keeps_one_class():
    schema = compile_fixture("recursive")
    route_id = schema.projection.roots[0]
    for depth in (0, 1, None):
        text = emit_relaxng_stub(schema, component_ids=(route_id,), depth=depth)
        assert text.count("class ") == 1
        assert "class Node(_rng)" in text


def test_selected_generic_route_emits_only_a_comment():
    schema = compile_fixture("generic")
    generic_route = next(
        route for route in schema.projection.routes.values() if route.generic_reason is not None
    )
    text = emit_relaxng_stub(schema, component_ids=(generic_route.route_id,), depth=0)
    assert "class " not in text
    assert generic_route.route_id in text
    assert generic_route.generic_reason in text
    assert "only generic-route comments" in text


def test_filtered_roots_are_commented_when_their_class_is_not_selected():
    schema = compile_fixture("stable")
    root = schema.projection.routes[schema.projection.roots[0]]
    note_id = root.children["note"].child_route_id
    assert note_id is not None
    text = emit_relaxng_stub(schema, component_ids=(note_id,), depth=0)
    assert "class Note(" in text and "class Doc(" not in text
    assert "outside the component selection" in text
    assert schema.projection.roots[0] in text
    compact = emit_relaxng_stub(schema, compact=True, component_ids=(note_id,), depth=0)
    assert "class Note(RNG)" in compact
    assert schema.projection.roots[0] in compact and ": ?" in compact


# ---------------------------------------------------------------------------
# Invalid schemas and defensive surface checks
# ---------------------------------------------------------------------------


def test_invalid_schema_emits_deterministic_text_without_projection(monkeypatch):
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<grammar xmlns="{NS}"><start><ref name="missing"/></start></grammar>')
    )
    assert schema.report.has_errors
    monkeypatch.setattr(
        RelaxNGSchema,
        "projection",
        property(lambda self: pytest.fail("projection must not be read for an invalid schema")),
    )
    monkeypatch.setattr(
        RelaxNGSchema,
        "projection_classes",
        property(lambda self: pytest.fail("projection classes must not be read")),
    )
    full = emit_relaxng_stub(schema)
    assert "schema report has errors" in full
    assert "class " not in full
    assert full == emit_relaxng_stub(schema)
    assert schema.stub() == full
    compact = emit_relaxng_stub(schema, compact=True)
    assert "aliases: none" in compact
    assert "schema report has errors" in compact
    assert emit_relaxng_stub(schema, component_ids=()) == full


def test_scalar_datatype_uses_its_declared_value_class():
    class Factory:
        def create(self, type_name, parameters, context):
            return ScalarDatatype(
                library_uri="urn:custom",
                type_name="thing",
                xsd_type=xsd_data_types.String,
                parameters=(),
                context=context,
                constraints=FacetConstraints(),
                white_space="preserve",
            )

    schema = compile_body(
        '<attribute name="value"><data type="thing" datatypeLibrary="urn:custom"/></attribute>',
        libraries={"urn:custom": DatatypeLibraryRegistration(Factory(), "custom/1")},
    )
    assert "value: _xsd.String" in emit_relaxng_stub(schema)


def test_opaque_datatype_uses_any_with_a_reason():
    class Opaque(Datatype):
        library_uri = "urn:opaque"
        type_name = "thing"
        supports_decode = True

        def allows(self, lexical, context):
            return True

        def equal(self, left, left_context, right, right_context):
            return left == right

        def decode(self, lexical, context):
            return lexical

    class Factory:
        def create(self, type_name, parameters, context):
            return Opaque()

    schema = compile_body(
        '<attribute name="value"><data type="thing" datatypeLibrary="urn:opaque"/></attribute>',
        libraries={"urn:opaque": DatatypeLibraryRegistration(Factory(), "opaque/1")},
    )
    text = emit_relaxng_stub(schema)
    assert "value: _any" in text
    assert "has no static Python value class" in text
    compact = emit_relaxng_stub(schema, compact=True)
    assert "value: Any" in compact
    assert "has no static Python value class" in compact
    assert "Any=typing.Any" in compact


def test_nondecodable_datatype_reads_alternative_value():
    class Nondecodable(Datatype):
        library_uri = "urn:none"
        type_name = "thing"

        def allows(self, lexical, context):
            return True

        def equal(self, left, left_context, right, right_context):
            return left == right

    class Factory:
        def create(self, type_name, parameters, context):
            return Nondecodable()

    schema = compile_body(
        '<attribute name="value"><data type="thing" datatypeLibrary="urn:none"/></attribute>',
        libraries={"urn:none": DatatypeLibraryRegistration(Factory(), "none/1")},
    )
    text = emit_relaxng_stub(schema)
    assert "value: _alt" in text
    assert "declares no decoder" in text


def test_compact_aliases_avoid_generated_class_names():
    class Opaque(Datatype):
        library_uri = "urn:opaque"
        type_name = "thing"
        supports_decode = True

        def allows(self, lexical, context):
            return True

        def equal(self, left, left_context, right, right_context):
            return left == right

        def decode(self, lexical, context):
            return lexical

    class Factory:
        def create(self, type_name, parameters, context):
            return Opaque()

    schema = compile_body(
        '<attribute name="wild"><data type="thing" datatypeLibrary="urn:opaque"/></attribute>'
        f'<attribute name="count"><data type="int" datatypeLibrary="{XSD}"/></attribute>'
        '<optional><element name="int"><text/></element></optional>'
        '<optional><element name="any"><text/></element></optional>',
        libraries={"urn:opaque": DatatypeLibraryRegistration(Factory(), "opaque/1")},
    )
    compact = emit_relaxng_stub(schema, compact=True)
    assert "wild: _Any" in compact
    assert "count: _Int" in compact
    assert " _Any=typing.Any" in compact
    assert "_Int=pyxsd.xsd_data_types.Int" in compact
    generated = classes_by_name(schema)
    assert {"Int", "Any"} <= set(generated)


def test_internal_consistency_failures_are_explicit():
    schema = compile_fixture("stable")
    plan = schema.projection
    note_route = next(route for route in plan.routes.values() if route.content.scalar_contracts)
    without_scalar = type("WithoutScalar", (), {"__name__": "WithoutScalar"})
    with pytest.raises(RuntimeError, match="no scalar descriptor"):
        stub_module._class_block(plan, note_route, without_scalar, {}, None, set())
    item_route = next(route for route in plan.routes.values() if not route.content.scalar_contracts)
    with_scalar = type(
        "WithScalar", (), {"__name__": "WithScalar", "_relaxng_scalar_field_": "typed_value"}
    )
    with pytest.raises(RuntimeError, match="has a scalar descriptor"):
        stub_module._class_block(plan, item_route, with_scalar, {}, None, set())


def test_missing_plan_inputs_degrade_to_any_or_alternative_value():
    schema = compile_fixture("alternatives")
    plan = schema.projection
    root = plan.routes[plan.roots[0]]
    field = next(item for item in root.attributes.values() if item.scalar_contracts)
    contract = field.scalar_contracts[0]
    empty_plan = replace(plan, encoding_patterns={})
    used: set[str] = set()
    ref = stub_module._atomic_ref(empty_plan, contract, used)
    assert ref.text == "_Any" and "no compiled datatype" in (ref.reason or "")
    list_field = next(
        route.content.scalar_contracts[0]
        for route in plan.routes.values()
        if route.content.scalar_contracts and route.content.scalar_contracts[0].kind == "list"
    )
    used.clear()
    list_ref = stub_module._scalar_ref(empty_plan, (list_field,), used, empty_reason="unused")
    assert list_ref.text == "_Any" and "no registered compiled list pattern" in (
        list_ref.reason or ""
    )
    used.clear()
    ref = stub_module._child_node_ref(plan, None, {}, None, used)
    assert ref.text == "_Any" and "no projected route" in (ref.reason or "")
    ref = stub_module._child_node_ref(plan, "route:missing", {}, None, used)
    assert ref.text == "_Any" and "absent from the projection plan" in (ref.reason or "")


def test_mixed_content_documents_the_ordered_view_without_a_scalar():
    schema = compile_body(
        '<mixed><zeroOrMore><element name="item"><empty/></element></zeroOrMore></mixed>'
    )
    text = emit_relaxng_stub(schema)
    assert "mixed content: ordered text and child views" in text
    assert "typed_value" not in text
    route = next(
        route for route in schema.projection.routes.values() if route.expanded_name == "item"
    )
    assert route.generic_reason is None


def test_wildcard_attributes_are_documented_not_fabricated():
    schema = compile_body(
        "<zeroOrMore><attribute><anyName/><choice><value>yes</value>"
        "<data type='token'><except><value>no</value></except></data></choice></attribute>"
        "</zeroOrMore><element name='values'><list><oneOrMore>"
        f"<data type='integer' datatypeLibrary='{XSD}'/>"
        "</oneOrMore></list></element>"
    )
    text = emit_relaxng_stub(schema)
    assert "wildcard attributes: expanded-name lookup only" in text
    assert "yes" not in text.split("# Starts:")[0].split("class ")[-1]
    compact = emit_relaxng_stub(schema, compact=True)
    assert "wildcard attributes" in compact


def test_start_aliases_are_sanitized_and_collisions_are_comments():
    keyword = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="class"><empty/></element>')
    )
    keyword.require_valid()
    keyword_text = emit_relaxng_stub(keyword)
    assert "class_: _builtins.type[_t0]" in keyword_text

    hyphen = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="doc-name"><empty/></element>')
    )
    hyphen.require_valid()
    assert "doc_x2d_name: _builtins.type[_t0]" in emit_relaxng_stub(hyphen)

    same_class = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="Doc"><empty/></element>')
    )
    same_class.require_valid()
    assert "collides with class 'Doc'" in emit_relaxng_stub(same_class)


def test_duplicate_start_alias_is_commented_once():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<grammar xmlns="{NS}">'
            '<start combine="choice"><ref name="a"/></start>'
            '<start combine="choice"><ref name="b"/></start>'
            '<define name="a"><element name="doc" ns="urn:a"><empty/></element></define>'
            '<define name="b"><element name="doc" ns="urn:b"><empty/></element></define>'
            "</grammar>"
        )
    )
    schema.require_valid()
    text = emit_relaxng_stub(schema)
    assert text.count("doc: _builtins.type[_t0]") == 1
    assert "repeats an earlier start alias" in text
    compact = emit_relaxng_stub(schema, compact=True)
    assert compact.count("doc: type[Doc]") == 1
    assert "{urn:b}doc" in compact or "urn:b" in compact


def test_same_local_name_across_namespaces_gets_distinct_classes():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="doc"><group>'
            '<element name="item" ns="urn:a"><empty/></element>'
            '<element name="item" ns="urn:b"><empty/></element>'
            "</group></element>"
        )
    )
    schema.require_valid()
    text = emit_relaxng_stub(schema)
    assert "class Item(_rng)" in text
    assert "class Item_2(_rng)" in text


def test_generic_root_has_a_start_comment_and_no_alias():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="doc"><choice>'
            f'<data type="integer" datatypeLibrary="{XSD}"/>'
            '<element name="nested"><empty/></element>'
            "</choice></element>"
        )
    )
    schema.require_valid()
    text = emit_relaxng_stub(schema)
    route_id = schema.projection.roots[0]
    assert "class Doc" not in text
    assert f"start route {route_id}" in text
    assert "is generic" in text
    compact = emit_relaxng_stub(schema, compact=True)
    assert f"# starts: {route_id}: ?" in compact


def test_open_start_name_class_has_a_comment():
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<grammar xmlns="{NS}"><start><element><anyName/><empty/></element></start></grammar>'
        )
    )
    schema.require_valid()
    text = emit_relaxng_stub(schema)
    assert "has an open element name class" in text
    assert "type[" not in text.split("# Starts:")[1]
    compact = emit_relaxng_stub(schema, compact=True)
    assert "open element name class" in compact


def test_empty_selection_has_no_imports_or_aliases():
    schema = compile_fixture("stable")
    full = emit_relaxng_stub(schema, component_ids=())
    assert "from __future__ import annotations" in full
    assert not any(
        line.startswith("from ") and "__future__" not in line for line in full.splitlines()
    )
    assert "class " not in full
    assert "This selection emits no projection classes" in full
    compact = emit_relaxng_stub(schema, compact=True, component_ids=())
    assert "aliases: none" in compact
    assert "import " not in compact


def test_invalid_schema_still_validates_unknown_component_ids():
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<grammar xmlns="{NS}"><start><ref name="missing"/></start></grammar>')
    )
    assert schema.report.has_errors
    with pytest.raises(KeyError):
        emit_relaxng_stub(schema, component_ids=("route:missing",))


def test_atomic_value_classes_cover_qname_temporal_binary_and_builtin_types():
    schema = compile_body(
        f'<attribute name="q"><data type="QName" datatypeLibrary="{XSD}"/></attribute>'
        f'<attribute name="d"><data type="date" datatypeLibrary="{XSD}"/></attribute>'
        f'<attribute name="h"><data type="hexBinary" datatypeLibrary="{XSD}"/></attribute>'
        '<attribute name="raw"><data type="token"/></attribute>'
    )
    text = emit_relaxng_stub(schema)
    assert "q: _xsd.QName" in text
    assert "d: _builtins.str" in text
    assert "h: _builtins.bytes" in text
    assert "raw: _xsd.Token" in text


def test_list_item_shapes_degrade_when_they_are_not_plain():
    group_homogeneous = compile_body(
        f'<element name="vals"><list><group><oneOrMore>'
        f'<data type="int" datatypeLibrary="{XSD}"/></oneOrMore>'
        f'<data type="string" datatypeLibrary="{XSD}"/></group></list></element>'
    )
    assert "typed_value: _alt" in emit_relaxng_stub(group_homogeneous)

    one_or_more_group = compile_body(
        f'<element name="vals"><list><oneOrMore><group>'
        f'<data type="int" datatypeLibrary="{XSD}"/>'
        f'<data type="string" datatypeLibrary="{XSD}"/></group></oneOrMore></list></element>'
    )
    assert "typed_value: _alt" in emit_relaxng_stub(one_or_more_group)


def test_nondecodable_list_items_are_alternative_values():
    class Nondecodable(Datatype):
        library_uri = "urn:none"
        type_name = "thing"

        def allows(self, lexical, context):
            return True

        def equal(self, left, left_context, right, right_context):
            return left == right

    class Factory:
        def create(self, type_name, parameters, context):
            return Nondecodable()

    schema = compile_body(
        '<element name="vals"><list><oneOrMore>'
        '<data type="thing" datatypeLibrary="urn:none"/>'
        "</oneOrMore></list></element>",
        libraries={"urn:none": DatatypeLibraryRegistration(Factory(), "none/1")},
    )
    text = emit_relaxng_stub(schema)
    assert "typed_value: _alt" in text
    assert "a list item datatype declares no decoder" in text
    assert "not a statically plain token sequence" not in text


def test_list_analysis_handles_refs_cycles_and_shared_nodes():
    from pyxsd.relaxng.patterns import Empty, Group, Ref
    from pyxsd.relaxng.patterns import List as ListPattern

    schema = compile_fixture("stable")
    data = next(
        pattern
        for pattern in schema.projection.encoding_patterns.values()
        if isinstance(pattern, stub_module.Data)
    )
    shared = ListPattern(Group(data, data))
    assert stub_module._list_items(shared) is not None
    assert stub_module._list_items(Empty()) is None

    target = Ref()
    group = Group(target, Empty())
    target.bind(group)
    cyclic = ListPattern(group)
    assert stub_module._list_items(cyclic) is None


def test_list_combiner_rejects_unproven_choice_and_group_shapes():
    from pyxsd.relaxng.patterns import Empty, Group, OneOrMore, Text

    schema = compile_fixture("stable")
    data = next(
        pattern
        for pattern in schema.projection.encoding_patterns.values()
        if isinstance(pattern, stub_module.Data)
    )
    contract = stub_module.scalar_contract(data)
    fixed = stub_module._ListItems(False, (contract,))
    empty = stub_module._ListItems(False, ())
    homogeneous = stub_module._ListItems(True, (contract,))
    one = OneOrMore(data)
    results = {
        id(data): fixed,
        id(one): homogeneous,
        id(Empty()): empty,
    }
    # Build fresh Empty nodes so each has its own result entry, as the
    # iterative walk records them by identity.
    left_empty = Empty()
    right_empty = Empty()
    results[id(Text())] = None
    results[id(left_empty)] = empty
    results[id(right_empty)] = empty
    assert stub_module._combine_list(stub_module.Choice(Text(), data), results) is None
    assert stub_module._combine_list(stub_module.Choice(one, right_empty), results) == homogeneous
    assert stub_module._combine_list(stub_module.Choice(left_empty, one), results) == homogeneous
    assert stub_module._combine_list(Group(one, data), results) is None


def test_optional_none_and_alias_helpers_cover_pathological_names():
    assert stub_module._optional(stub_module._TypeRef("None")).text == "None"
    assert stub_module._alias_name("{}") == "_"
    assert stub_module._alias_name("{}1doc") == "_1doc"


def test_empty_start_sections_and_no_route_notice_are_total():
    schema = compile_fixture("stable")
    plan = schema.projection
    assert stub_module._compact_starts(()) == (
        "# starts: none (the compiled start pattern declares no element route)"
    )
    empty_plan = replace(plan, roots=(), routes={})
    assert (
        stub_module._empty_notice(empty_plan, {}, (), False)
        == "The projection plan has no routes; no classes or aliases are emitted."
    )
    all_generic = stub_module._empty_notice(plan, {}, tuple(plan.routes), False)
    assert all_generic == (
        "No stable projection routes were planned; every route uses the "
        "generic RelaxNGElement view."
    )
    names = stub_module._full_names(plan, tuple(plan.routes), {}, {})
    text = stub_module._render_full(schema, (), [], (), None, names, {})
    assert "# Starts: none" in text


def test_list_combiner_handles_refs_unproven_choices_and_reused_children():
    from pyxsd.relaxng.patterns import Group, Ref, Text

    schema = compile_fixture("stable")
    data = next(
        pattern
        for pattern in schema.projection.encoding_patterns.values()
        if isinstance(pattern, stub_module.Data)
    )
    second = next(
        pattern
        for pattern in schema.projection.encoding_patterns.values()
        if isinstance(pattern, stub_module.Data) and pattern is not data
    )
    contract = stub_module.scalar_contract(data)
    other = stub_module.scalar_contract(second)
    results = {
        id(data): stub_module._ListItems(False, (contract,)),
        id(second): stub_module._ListItems(False, (other,)),
    }
    ref = Ref()
    ref.bind(data)
    assert stub_module._combine_list(ref, results) == results[id(data)]
    assert stub_module._combine_list(Text(), {}) is None
    assert stub_module._combine_list(stub_module.Choice(data, second), results) is None
    assert stub_module._list_items(stub_module.List(Group(Group(data, data), data))) is not None


def test_start_alias_named_like_the_public_base_class_remains_usable():
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="RelaxNGElement"><empty/></element>')
    )
    schema.require_valid()
    text = emit_relaxng_stub(schema)
    assert "RelaxNGElement: _builtins.type[_t0]" in text
    _assert_full_annotation_spelling(text)


def test_scalar_descriptor_uses_the_runtime_collision_suffixed_name():
    schema = compile_body(
        '<attribute name="typed_value"><text/></attribute>'
        '<attribute name="typed_value_"><text/></attribute>'
        f'<data type="integer" datatypeLibrary="{XSD}"/>'
    )
    root_class = schema.projection_classes.class_for(schema.projection.roots[0])
    assert root_class.__dict__["_relaxng_scalar_field_"] == "typed_value__"
    text = emit_relaxng_stub(schema)
    assert "typed_value: _alt" in text
    assert "typed_value_: _alt" in text
    assert "typed_value__: _xsd.Integer" in text


def test_scalar_emptiness_handles_values_lists_custom_datatypes_and_models():
    from pyxsd.relaxng.patterns import Interleave
    from pyxsd.relaxng.projection import ContentContract

    class Exploding(Datatype):
        library_uri = "urn:boom"
        type_name = "thing"
        supports_decode = True

        def allows(self, lexical, context):
            if lexical == "":
                raise ValueError("no empty value")
            return True

        def equal(self, left, left_context, right, right_context):
            return left == right

    class Factory:
        def create(self, type_name, parameters, context):
            return Exploding()

    custom = compile_body(
        '<element name="value"><data type="thing" datatypeLibrary="urn:boom"/></element>',
        libraries={"urn:boom": DatatypeLibraryRegistration(Factory(), "boom/1")},
    )
    text = emit_relaxng_stub(custom)
    assert "typed_value: _any | None" in text
    assert "typed_value: _any" not in text.replace("typed_value: _any | None", "")

    empty_value = compile_body("<value></value>")
    assert "typed_value: _xsd.Token | None" in emit_relaxng_stub(empty_value)
    nonempty_value = compile_body("<value>fixed</value>")
    assert "typed_value: _xsd.Token" in emit_relaxng_stub(nonempty_value)

    repeated = compile_body("<oneOrMore><text/></oneOrMore>")
    assert "typed_value: _alt | None" in emit_relaxng_stub(repeated)

    assert stub_module._list_allows_empty(Interleave(stub_module.Empty(), stub_module.Empty()))
    schema = compile_fixture("alternatives")
    plan = schema.projection
    contract = next(
        contract
        for route in plan.routes.values()
        for contract in route.content.scalar_contracts
        if contract.kind == "list"
    )
    empty_plan = replace(plan, encoding_patterns={})
    assert stub_module._scalar_allows_empty(empty_plan, contract)
    assert stub_module._model_allows_empty(plan, ContentContract("notAllowed")) is False
    assert stub_module._model_allows_empty(plan, ContentContract("child")) is True
    assert stub_module._model_allows_empty(
        plan, ContentContract("repeat", (ContentContract("empty"),))
    )


def test_custom_scalar_value_classes_are_annotated_only_when_importable():
    class CustomValue:
        pass

    def factory_for(value_type):
        class Factory:
            def create(self, type_name, parameters, context):
                return ScalarDatatype(
                    library_uri="urn:custom",
                    type_name="thing",
                    xsd_type=value_type,
                    parameters=(),
                    context=context,
                    constraints=FacetConstraints(),
                    white_space="preserve",
                )

        return Factory()

    builtin_value = compile_body(
        '<attribute name="value"><data type="thing" datatypeLibrary="urn:builtin"/></attribute>',
        libraries={"urn:builtin": DatatypeLibraryRegistration(factory_for(int), "builtin/1")},
    )
    assert "value: _builtins.int" in emit_relaxng_stub(builtin_value)

    opaque_value = compile_body(
        '<attribute name="value"><data type="thing" datatypeLibrary="urn:value"/></attribute>',
        libraries={"urn:value": DatatypeLibraryRegistration(factory_for(CustomValue), "value/1")},
    )
    text = emit_relaxng_stub(opaque_value)
    assert "value: _any" in text
    assert "has no stable import" in text


def _compile_root(root_name: str, body: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="{root_name}">{body}</element>')
    )
    schema.require_valid()
    return schema


def test_full_annotations_use_private_names_for_colliding_xml_names(tmp_path):
    scalar_root = RelaxNGSchema.compile(FIXTURES / "collisions-scalars.rng")
    child_root = RelaxNGSchema.compile(FIXTURES / "collisions-elements.rng")
    scalar_root.require_valid()
    child_root.require_valid()
    private_root = _compile_root("_builtins", "<empty/>")
    alias_root = _compile_root("_t0", "<empty/>")

    texts = [
        emit_relaxng_stub(scalar_root),
        emit_relaxng_stub(child_root),
        emit_relaxng_stub(private_root),
        emit_relaxng_stub(alias_root),
    ]
    for text in texts:
        _assert_full_annotation_spelling(text)
        compile(text, "<collision-stub>", "exec", dont_inherit=True)
    assert "list: _alt" in texts[0]
    assert "typed_value: _builtins.list[_xsd.Int]" in texts[0]
    assert "type: _builtins.type[_t0]" in texts[0]
    assert "Item: _t1" in texts[1]
    assert "RelaxNGElement: _t2" in texts[1]
    assert "generic: _rng" in texts[1]
    assert "_builtins: _builtins_2.type[_t0]" in texts[2]
    assert "_t0: _builtins.type[_t0_2]" in texts[3]

    paths = []
    for index, text in enumerate(texts):
        path = tmp_path / f"collision_{index}.pyi"
        path.write_text(text, encoding="utf-8")
        paths.append(str(path))
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", *paths],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_decimal_imports_and_private_alias_collision_binding():
    schema = compile_body(
        f'<attribute name="value"><data type="decimal" datatypeLibrary="{XSD}"/></attribute>'
    )
    text = emit_relaxng_stub(schema)
    assert "value: _decimal.Decimal" in text
    assert "import decimal as _decimal" in text
    assert (
        stub_module._collision_binding("_builtins", {}, frozenset({"_builtins"}))
        == "a private stub alias"
    )


def test_compact_start_note_keeps_the_sanitized_xml_name():
    schema = _compile_root("doc-name", "<empty/>")
    compact = emit_relaxng_stub(schema, compact=True)
    assert "doc_x2d_name: type[Doc_x2d_name]" in compact
    assert "start element doc-name" in compact


def test_header_marks_an_empty_or_generic_selection():
    schema = compile_fixture("stable")
    empty = emit_relaxng_stub(schema, component_ids=())
    assert "#\n# import it for type checking:" in empty
    assert "This selection emits no projection classes" in empty
    generic_schema = compile_fixture("generic")
    route = next(
        route
        for route in generic_schema.projection.routes.values()
        if route.generic_reason is not None
    )
    generic_selection = emit_relaxng_stub(generic_schema, component_ids=(route.route_id,), depth=0)
    assert "This selection emits no projection classes" in generic_selection
