"""Evaluate JSON Schema without replacing the authoritative JSON value."""

import sys
from collections import UserDict
from decimal import Decimal
from io import StringIO

import pytest
from referencing import Registry
from referencing.jsonschema import DRAFT202012

import pyxsd.schemaless as schemaless
from pyxsd import ValidationError
from pyxsd.remote import RemoteResourcePolicy
from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.resource_refs import resolve_reference
from pyxsd.schemaless import from_data, from_json
from pyxsd.schemaless.json_schema import SchemaGuidance
from pyxsd.schemaless.json_values import JsonLimits
from pyxsd.validation import ValidationReport
from pyxsd.xdm import XdmAtomic

_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_REMOTE = "https://schemas.example/types/integer.json"


# fmt: off
def test_schema_validation_reports_instance_and_schema_pointers():
    doc = from_json('{"id":"bad"}', json_schema={
        "type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]
    })
    assert not doc.is_valid
    issue = doc.report.issues[0]
    assert issue.location.pointer == "/id"
    assert issue.location.schema_pointer == "/properties/id/type"
    assert doc.to_data() == {"id": "bad"}


def test_refs_boolean_schemas_and_applicators_use_real_evaluation():
    schema = {"$defs": {"i": {"type": "integer"}}, "allOf": [{"$ref": "#/$defs/i"}]}
    assert from_json("1", json_schema=schema).is_valid
    assert not from_json("false", json_schema=False).is_valid


def test_format_and_content_annotations_are_not_asserted_by_default():
    doc = from_json('"not-a-date"', json_schema={"type": "string", "format": "date"})
    assert doc.is_valid
    encoded = from_json('"YQ=="', json_schema={"contentEncoding": "base64"})
    assert encoded.to_data() == "YQ=="
# fmt: on


def test_format_assertion_checks_the_value_without_casting_it():
    schema = {"$schema": _DRAFT, "format": "date", "contentEncoding": "base64"}
    invalid = from_data("not-a-date", json_schema=schema, format_assertion=True)
    assert not invalid.is_valid
    assert invalid.report.issues[0].location.schema_pointer == "/format"
    valid = from_data("2026-11-23", json_schema=schema, format_assertion=True)
    assert valid.is_valid
    assert valid.to_xdm() == (XdmAtomic.string("2026-11-23"),)
    assert from_data("not base64!", json_schema={"contentEncoding": "base64"}).is_valid


@pytest.mark.parametrize("schema", ['{"type": "integer"}', UserDict({"type": "integer"}), "true"])
def test_accepts_json_schema_text_and_non_dict_mappings(schema):
    assert from_data(1, json_schema=schema).is_valid
    assert not from_data(1, json_schema="false").is_valid


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"properties": {"x": {"type": "int"}}}, "/properties/x/type"),
        ({"$schema": []}, r"/\$schema"),
        ('{"type":"string","type":"integer"}', "Invalid JSON Schema input"),
        ('{"maximum":NaN}', "Invalid JSON Schema input"),
        ({"const": object()}, "Invalid JSON Schema input"),
        ([], "must be a boolean or object"),
        ("null", "must be a boolean or object"),
    ],
)
def test_invalid_schemas_raise_before_evaluating_even_a_valid_instance(schema, message):
    with pytest.raises(schemaless.JsonSchemaError, match=message):
        from_json("null", json_schema=schema)


def test_deep_schema_meta_validation_reports_compilation_recursion():
    schema = {"type": "integer"}
    # Meta-validation consumes several frames per nested applicator, more
    # than the initial native-number normalization.
    for _ in range(sys.getrecursionlimit() // 3):
        schema = {"not": schema}

    with pytest.raises(schemaless.JsonSchemaError, match=r"compilation.*recursion") as exc:
        from_data(1, json_schema=schema)
    assert isinstance(exc.value.__cause__, RecursionError)


def test_deep_registered_schema_reports_compilation_recursion():
    schema = {"type": "integer"}
    for _ in range(sys.getrecursionlimit() + 1):
        schema = {"not": schema}
    registry = Registry().with_resource(_REMOTE, DRAFT202012.create_resource(schema))

    with pytest.raises(schemaless.JsonSchemaError, match=r"compilation.*recursion") as exc:
        from_data(1, json_schema={"$ref": _REMOTE}, reference_registry=registry)
    assert isinstance(exc.value.__cause__, RecursionError)


def test_shallow_nested_schema_still_reports_instance_validity():
    schema = {"not": {"not": {"type": "integer"}}}
    assert from_data(1, json_schema=schema).is_valid
    assert from_data("1", json_schema=schema).report.error_count == 1


@pytest.mark.parametrize(
    "schema",
    [
        {"$schema": "https://example.com/unknown-dialect"},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
        {"$defs": {"nested": {"$id": "nested", "$schema": "https://example.com/unknown-dialect"}}},
    ],
)
def test_unsupported_dialects_are_not_silently_downgraded(schema):
    with pytest.raises(schemaless.JsonSchemaError, match="dialect"):
        from_data(None, json_schema=schema)


def test_only_supported_or_optional_vocabularies_are_accepted():
    core = "https://json-schema.org/draft/2020-12/vocab/core"
    extension = "https://example.com/vocabulary"
    schema = {"$vocabulary": {core: True, extension: False}, "type": "integer"}
    assert from_data(1, json_schema=schema).is_valid
    schema["$vocabulary"][extension] = True
    with pytest.raises(schemaless.JsonSchemaError, match="required vocabulary"):
        from_data(1, json_schema=schema)
    with pytest.raises(schemaless.JsonSchemaError, match=r"/\$vocabulary"):
        from_data(1, json_schema={"$vocabulary": {"invalid": 1}})


def test_annotation_objects_are_not_inspected_as_dialects():
    schema = {"const": {"$schema": "https://example.com/not-a-dialect"}}
    assert from_data(schema["const"], json_schema=schema).is_valid


def test_invalid_instance_keeps_native_access_and_xdm():
    stream = StringIO('{"a/~":[1.2]}')
    stream.name = "sample.json"
    schema = {"properties": {"a/~": {"prefixItems": [{"type": "string"}]}}}
    document = from_json(stream, json_schema=schema)
    assert from_json(document.to_json()).to_data() == {"a/~": [Decimal("1.2")]}
    assert document.value_at("/a~1~0/0") == Decimal("1.2")
    assert document.to_xdm()[0].get(XdmAtomic.string("a/~"))[0].get(1) == (
        XdmAtomic.decimal(Decimal("1.2")),
    )
    issue = document.report.issues[0]
    assert issue.location.source == "sample.json"
    assert issue.location.pointer == "/a~1~0/0"
    assert issue.location.schema_pointer == "/properties/a~1~0/prefixItems/0/type"
    assert "1.2" not in issue.message
    with pytest.raises(ValidationError) as exc:
        document.require_valid()
    assert exc.value.report is document.report


def test_integer_is_mathematical_not_the_native_storage_type():
    document = from_json("9007199254740993.00", json_schema={"type": "integer"})
    assert document.is_valid
    assert document.to_data() == Decimal("9007199254740993.00")
    assert document.to_json() == "9007199254740993.00"
    assert not from_json("1.01", json_schema={"type": "integer"}).is_valid
    assert not from_json("true", json_schema={"type": "integer"}).is_valid


def test_native_decimal_and_host_float_schema_numbers_compare_exactly():
    assert from_json("0.3", json_schema={"multipleOf": 0.1}).is_valid
    assert from_data(0.3, json_schema='{"multipleOf":0.1}').is_valid
    assert not from_json(
        "0.300000000000000000000000000001", json_schema='{"multipleOf":0.1}'
    ).is_valid
    assert from_json("1", json_schema='{"minLength":1.0}').is_valid
    assert from_json("1e1000000000", json_schema={"multipleOf": 2}).is_valid
    assert from_json("0", json_schema='{"multipleOf":1e1000000000}').is_valid
    assert not from_json("2", json_schema='{"multipleOf":1e1000000000}').is_valid
    assert from_json("300e-3", json_schema='{"multipleOf":0.1}').is_valid
    assert from_data("annotation", json_schema={"multipleOf": 2}).is_valid
    assert from_json("0.3", json_schema={"const": 0.3, "maximum": 0.3}).is_valid
    assert from_data(0.3, json_schema='{"const":0.3,"minimum":0.3}').is_valid


def test_explicit_nested_draft_uses_the_same_native_numeric_types():
    schema = {"properties": {"x": {"$id": "x", "$schema": _DRAFT, "type": "integer"}}}
    assert from_json('{"x":1.0}', json_schema=schema).is_valid


def test_relative_id_and_refs_resolve_against_the_schema_base_not_the_instance():
    registry = Registry().with_resource(_REMOTE, DRAFT202012.create_resource({"type": "integer"}))
    schema = {
        "$id": "types/root.json",
        "$defs": {"selection": {"$anchor": "here", "$ref": "integer.json"}},
        "$ref": "#here",
    }
    options = {
        "json_schema": schema,
        "base_uri": "https://schemas.example/root.json",
        "reference_registry": registry,
    }
    assert from_data(1, **options).is_valid
    invalid = from_data("1", **options)
    assert invalid.report.issues[0].location.schema_pointer == "/type"


def test_internal_reference_reports_the_keyword_in_the_referenced_schema():
    schema = {"$defs": {"a/b": {"type": "integer"}}, "$ref": "#/$defs/a~1b"}
    issue = from_data("x", json_schema=schema).report.issues[0]
    assert issue.location.schema_pointer == "/$defs/a~1b/type"


def test_false_subschema_keeps_its_instance_and_schema_occurrence():
    issue = from_data(
        {"excluded": 1}, json_schema={"properties": {"excluded": False}}
    ).report.issues[0]
    assert issue.location.pointer == "/excluded"
    assert issue.location.schema_pointer == "/properties/excluded"


def test_boolean_reference_reports_the_target_occurrence_under_urn_base():
    schema = {"$id": "urn:schemas:root", "$defs": {"deny": False}, "$ref": "#/$defs/deny"}
    assert (
        from_data(1, json_schema=schema).report.issues[0].location.schema_pointer == "/$defs/deny"
    )


def test_unknown_schema_keyword_does_not_become_an_assertion():
    assert from_data(1, json_schema={"_pyxsd_false": False}).is_valid


def test_diagnostics_sort_array_indices_as_numbers():
    document = from_data(
        [False] * 11,
        json_schema={"items": {"type": "integer"}},
        limits=JsonLimits(max_diagnostics=3),
    )
    assert [issue.location.pointer for issue in document.report.issues] == ["/0", "/1", "/2"]


def test_registered_schema_is_checked_before_it_is_used():
    registry = Registry().with_resource(_REMOTE, DRAFT202012.create_resource({"type": "int"}))
    with pytest.raises(schemaless.JsonSchemaError, match="/type"):
        from_data(1, json_schema={"$ref": _REMOTE}, reference_registry=registry)


@pytest.mark.parametrize(
    "ref", ["#/$defs/missing", "#unregistered", "missing.json", "#/examples/not-an-index"]
)
def test_unresolved_references_raise_instead_of_reporting_instance_invalidity(ref):
    with pytest.raises(schemaless.JsonSchemaError, match="reference"):
        from_data(0, json_schema={"$ref": ref, "examples": []})


def test_reference_must_target_a_schema():
    with pytest.raises(schemaless.JsonSchemaError, match="must be a boolean or object"):
        from_data(1, json_schema={"$ref": "#/const", "const": 1})


def test_recursive_reference_without_progress_raises_schema_error():
    with pytest.raises(schemaless.JsonSchemaError, match="recursion"):
        from_data(0, json_schema={"$ref": "#"})


def test_remote_reference_never_enables_the_default_network_retriever(monkeypatch):
    def reject_network(*args, **kwargs):
        pytest.fail("HTTP must not be attempted without a resource policy")

    monkeypatch.setattr("urllib.request.urlopen", reject_network)
    with pytest.raises(schemaless.JsonSchemaError, match="reference"):
        from_data(0, json_schema={"$ref": _REMOTE})


def test_registry_is_registration_not_permission_to_retrieve():
    calls = []

    def retrieve(uri):
        calls.append(uri)
        return DRAFT202012.create_resource(True)

    with pytest.raises(schemaless.JsonSchemaError, match="reference"):
        from_data(1, json_schema={"$ref": _REMOTE}, reference_registry=Registry(retrieve=retrieve))
    assert calls == []


class _MemoryPolicy:
    def __init__(self, resources):
        self.resources = resources
        self.reads = []

    def read(self, source, *, base, purpose):
        self.reads.append((source, base, purpose))
        if source not in self.resources:
            raise ResourcePolicyError("Not authorized")
        data = self.resources[source]
        return data if isinstance(data, ResourceData) else ResourceData(source, data)

    def parse(self, data, context):
        pytest.fail("JSON Schema resources should not use the XML parser")

    def new_report(self):
        return ValidationReport(max_issues=1)


def test_explicit_policy_handles_reads_with_actual_parents_not_schema_ids():
    intermediary = "https://schemas.example/types/intermediate.json"
    parent = "https://schemas.example/root.json"
    policy = _MemoryPolicy(
        {
            intermediary: b'{"$id":"https://claimed.example/types/claimed.json","$ref":"integer.json"}',
            "https://claimed.example/types/integer.json": b'{"type":"integer"}',
        }
    )
    document = from_data(
        "bad",
        json_schema={"$ref": intermediary, "const": 3},
        base_uri=parent,
        resource_policy=policy,
    )
    assert policy.reads == [
        (intermediary, parent, "schema"),
        ("https://claimed.example/types/integer.json", intermediary, "schema"),
    ]
    assert not document.is_valid
    assert document.report.error_count == 2
    assert document.report.suppressed_count == 1


def test_policy_uses_canonical_retrieval_location_for_relative_refs_after_redirect():
    target = "https://schemas.example/canonical/relocated.json"
    relative = "https://schemas.example/canonical/resource.json"
    policy = _MemoryPolicy(
        {
            _REMOTE: ResourceData(target, b'{"$ref":"resource.json"}'),
            relative: b"true",
        }
    )
    assert from_data(1, json_schema={"$ref": _REMOTE}, resource_policy=policy).is_valid
    assert policy.reads == [(_REMOTE, None, "schema"), (relative, target, "schema")]


def test_relative_local_base_is_not_applied_twice_by_policy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "child.json").write_text("true", encoding="utf-8")

    class LocalPolicy(RemoteResourcePolicy):
        def read(self, source, *, base, purpose):
            assert resolve_reference(source, base=base) == str(tmp_path / "schemas" / "child.json")
            return super().read(source, base=base, purpose=purpose)

    assert from_data(
        1,
        json_schema={"$ref": "child.json"},
        base_uri="schemas/root.json",
        resource_policy=LocalPolicy(),
    ).is_valid


def test_declaring_remote_id_does_not_authorize_anonymous_remote_loading():
    def reject_dns(*args):
        pytest.fail("the resource policy must deny the read before DNS")

    with pytest.raises(schemaless.JsonSchemaError, match="reference"):
        from_data(
            1,
            json_schema={"$id": "https://schemas.example/root.json", "$ref": _REMOTE},
            resource_policy=RemoteResourcePolicy(resolver=reject_dns),
        )


def test_policy_denial_is_an_input_error_not_instance_invalidity():
    with pytest.raises(schemaless.JsonSchemaError, match="reference"):
        from_data(1, json_schema={"$ref": _REMOTE}, resource_policy=_MemoryPolicy({}))


def test_policy_is_consulted_again_when_reusing_compiled_guidance():
    policy = _MemoryPolicy({_REMOTE: b"true"})
    guidance = SchemaGuidance.compile({"$ref": _REMOTE}, resource_policy=policy)
    assert not guidance.validate(1).has_errors
    policy.resources.clear()
    with pytest.raises(schemaless.JsonSchemaError, match="reference"):
        guidance.validate(1)


def test_repeated_sibling_references_do_not_read_the_same_policy_edge_twice():
    policy = _MemoryPolicy({_REMOTE: b'{"type":"integer"}'})
    schema = {"properties": {"one": {"$ref": _REMOTE}, "two": {"$ref": _REMOTE}}}
    assert from_data({"one": 1, "two": 2}, json_schema=schema, resource_policy=policy).is_valid
    assert policy.reads == [(_REMOTE, None, "schema")]


def test_retrieved_schema_cannot_replace_a_registered_resource():
    registered = "https://schemas.example/registered"
    registry = Registry().with_resource(registered, DRAFT202012.create_resource({"const": 1}))
    policy = _MemoryPolicy({_REMOTE: f'{{"$id":"{registered}","const":2}}'.encode()})
    with pytest.raises(schemaless.JsonSchemaError, match="Conflicting"):
        from_data(
            2, json_schema={"$ref": _REMOTE}, reference_registry=registry, resource_policy=policy
        )


def test_policy_can_reauthorize_the_same_resource_for_another_parent():
    a, b = "https://schemas.example/a", "https://schemas.example/b"
    policy = _MemoryPolicy(
        {
            a: f'{{"$ref":"{_REMOTE}"}}'.encode(),
            b: f'{{"$ref":"{_REMOTE}"}}'.encode(),
            _REMOTE: b"true",
        }
    )
    schema = {"allOf": [{"$ref": a}, {"$ref": b}]}
    assert from_data(2, json_schema=schema, resource_policy=policy).is_valid
    assert (_REMOTE, a, "schema") in policy.reads
    assert (_REMOTE, b, "schema") in policy.reads


def test_registry_accepts_multiple_names_for_the_same_resource():
    a, b = "https://schemas.example/a", "https://schemas.example/b"
    resource = DRAFT202012.create_resource({"$id": a, "type": "integer"})
    registry = Registry().with_resources([(a, resource), (b, resource)])
    assert from_data(2, json_schema={"$ref": b}, reference_registry=registry).is_valid


def test_fetch_from_one_parent_does_not_authorize_it_for_another():
    parent_a = "https://a.example/a"
    parent_b = "https://b.example/b"

    class Policy(_MemoryPolicy):
        def read(self, source, *, base, purpose):
            if source == _REMOTE and base != parent_a:
                raise ResourcePolicyError("Not authorized for this parent")
            return super().read(source, base=base, purpose=purpose)

    policy = Policy(
        {
            parent_a: f'{{"$ref":"{_REMOTE}"}}'.encode(),
            _REMOTE: b'{"$ref":"https://b.example/b"}',
            parent_b: f'{{"$ref":"{_REMOTE}"}}'.encode(),
        }
    )
    with pytest.raises(schemaless.JsonSchemaError, match="Cannot retrieve"):
        from_data(1, json_schema={"$ref": parent_a}, resource_policy=policy)


def test_schema_declaring_required_format_assertion_does_not_silently_ignore_it():
    schema = {"$vocabulary": {"https://json-schema.org/draft/2020-12/vocab/format-assertion": True}}
    with pytest.raises(schemaless.JsonSchemaError, match="required vocabulary"):
        from_data("not-a-date", json_schema=schema)


def test_registered_resource_takes_precedence_over_policy_retrieval():
    registry = Registry().with_resource(_REMOTE, DRAFT202012.create_resource(True))
    assert from_data(
        1,
        json_schema={"$ref": _REMOTE},
        reference_registry=registry,
        resource_policy=_MemoryPolicy({}),
    ).is_valid


def test_policy_retrieval_uses_strict_decoding_and_schema_checks():
    policy = _MemoryPolicy({_REMOTE: b'{"type": "integer", "type": "null"}'})
    with pytest.raises(schemaless.JsonSchemaError, match="Invalid JSON Schema input"):
        from_data(1, json_schema={"$ref": _REMOTE}, resource_policy=policy)


def test_unevaluated_properties_respects_property_evaluation_through_refs():
    schema = {
        "$defs": {"known": {"properties": {"outer": True}}},
        "allOf": [{"$ref": "#/$defs/known"}],
        "unevaluatedProperties": False,
    }
    assert from_data({"outer": 1}, json_schema=schema).is_valid
    document = from_data({"outer": 1, "extra": 2}, json_schema=schema)
    assert not document.is_valid
    assert document.report.issues[0].location.schema_pointer == "/unevaluatedProperties"


def test_dynamic_ref_uses_the_evaluators_dynamic_scope():
    address = "https://schemas.example/tree"
    registry = Registry().with_resource(
        address,
        DRAFT202012.create_resource(
            {
                "$id": address,
                "$dynamicAnchor": "node",
                "properties": {"child": {"$dynamicRef": "#node"}},
            }
        ),
    )
    schema = {
        "$id": "https://schemas.example/strict-tree",
        "$dynamicAnchor": "node",
        "$ref": address,
        "required": ["name"],
    }
    assert from_data({"name": "root"}, json_schema=schema, reference_registry=registry).is_valid
    invalid = from_data(
        {"name": "root", "child": {}}, json_schema=schema, reference_registry=registry
    )
    assert invalid.report.issues[0].location.pointer == "/child"


@pytest.mark.parametrize("maximum", [0, 2, 5])
def test_diagnostic_limit_keeps_first_sorted_paths_without_changing_validity(maximum):
    document = from_data(
        {"Z": 0, "a": 0},
        json_schema={"properties": {"a": {"type": "string", "const": 1}, "Z": {"type": "string"}}},
        limits=JsonLimits(max_diagnostics=maximum),
    )
    expected = [
        ("/Z", "/properties/Z/type"),
        ("/a", "/properties/a/const"),
        ("/a", "/properties/a/type"),
    ]
    assert document.report.error_count == 3
    assert document.report.suppressed_count == max(0, 3 - maximum)
    assert document.report.diagnostics_complete is (maximum >= 3)
    assert not document.is_valid
    assert [(i.location.pointer, i.location.schema_pointer) for i in document.report.issues] == (
        expected[:maximum]
    )


def test_diagnostic_sort_accepts_paths_from_different_resources():
    a, b = "https://schemas.example/a", "https://schemas.example/b"
    registry = Registry().with_contents(
        [
            (a, {"$ref": "#/ext/0", "ext": [{"type": "integer"}]}),
            (b, {"$ref": "#/ext/1", "ext": {"1": {"type": "boolean"}}}),
        ],
        default_specification=DRAFT202012,
    )
    document = from_data(
        "not either", json_schema={"allOf": [{"$ref": b}, {"$ref": a}]}, reference_registry=registry
    )
    assert [issue.location.schema_pointer for issue in document.report.issues] == [
        "/ext/0/type",
        "/ext/1/type",
    ]


def test_native_json_works_without_the_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    assert from_json("1").to_data() == 1
    assert from_data(None).to_xdm() == ()
    with pytest.raises(schemaless.JsonSchemaError, match=r"pyxsd\[jsonschema\]"):
        from_json("1", json_schema=True)
