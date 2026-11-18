"""Draft 2020-12 schemas for the RELAX NG ``pyxsd-json`` profile.

Every positive fixture exports through a real compiled grammar and validates
against the generated schema.  Negative fixtures pin the shapes the schema
rejects; the few rules JSON Schema cannot carry (grammar validity, name-class
membership, candidate identity and sorting) stay in the disclosure list rather
than being pretended away.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import pyxsd
from pyxsd.exceptions import PyXSDError
from pyxsd.relaxng import RelaxNGSchema
from pyxsd.relaxng.conversion import RelaxNGUnsupportedAdapterError
from pyxsd.relaxng.datatypes import Datatype, DatatypeFactory, DatatypeLibraryRegistration
from pyxsd.relaxng.json_profile import export_profile
from pyxsd.relaxng.json_schema import (
    _admitted_names,
    _any_of,
    _apply_cardinality,
    _child_model,
    _combine_tokens,
    _content_scalar_contracts,
    _excluded_eqnames,
    _may_be_empty,
    _mentioned_names,
    _name_class_metadata,
    _token_patterns,
    build_relaxng_json_schema,
)
from pyxsd.relaxng.names import AnyName, ExceptName, Name, NameChoice, NameClass, NsName
from pyxsd.relaxng.patterns import (
    Choice,
    Data,
    Empty,
    Group,
    List,
    OneOrMore,
    Ref,
    Text,
)
from pyxsd.relaxng.projection import Cardinality, ContentContract, ProjectionPlan

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"

INTEGER = '<data type="integer"/>'
STRING = '<data type="string"/>'
A = '<element name="a"><data type="string"/></element>'
B = '<element name="b"><data type="string"/></element>'
ATTR = '<optional><attribute name="z"><data type="integer"/></attribute></optional>'
REQUIRED_ATTR = '<attribute name="z"><data type="integer"/></attribute>'
WILDCARD_ATTR = "<zeroOrMore><attribute><anyName/><text/></attribute></zeroOrMore>"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def schema_for(content: str, *, name: str = 'name="r"') -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" datatypeLibrary="{XSD}" {name}>{content}</element>')
    )
    assert not schema.report.has_errors, schema.report.issues
    return schema


def grammar_for(body: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<grammar xmlns="{NS}" datatypeLibrary="{XSD}">{body}</grammar>')
    )
    assert not schema.report.has_errors, schema.report.issues
    return schema


def parse(schema: RelaxNGSchema, xml: str) -> Any:
    document = schema.parse(io.StringIO(xml), typed=True)
    assert not document.report.has_errors, document.report.issues
    return document


def generated(schema: RelaxNGSchema, element: str | None = None) -> dict[str, Any]:
    result = schema.json_schema(element=element)
    Draft202012Validator.check_schema(result)
    return result


def errors(schema: RelaxNGSchema, data: object, element: str | None = None) -> list[Any]:
    return list(Draft202012Validator(generated(schema, element)).iter_errors(data))


def unvalidated(schema: RelaxNGSchema, element: str | None = None) -> list[str]:
    return generated(schema, element)["x-pyxsd-validation"]["unvalidated"]


def exported(
    schema: RelaxNGSchema,
    xml: str,
    *,
    plan: ProjectionPlan | None = None,
    element: str | None = None,
) -> object:
    document = parse(schema, xml)
    if plan is None:
        return document.to_data(format="pyxsd-json")
    return export_profile(document.root, plan, element=element)


def schema_from_plan(plan: ProjectionPlan, element: str | None = None) -> dict[str, Any]:
    result = build_relaxng_json_schema(SimpleNamespace(projection=plan), element=element)
    Draft202012Validator.check_schema(result)
    return result


def plan_with(schema: RelaxNGSchema, **changes: Any) -> ProjectionPlan:
    plan = schema.projection
    route_id = plan.roots[0]
    return replace(
        plan, routes={**plan.routes, route_id: replace(plan.routes[route_id], **changes)}
    )


def assert_valid(schema: RelaxNGSchema, data: object, element: str | None = None) -> None:
    assert errors(schema, data, element) == []


def root_body(result: dict[str, Any]) -> dict[str, Any]:
    """Follow the selected root's single ``$ref`` to its route definition."""
    if "$ref" in result:
        return result["$defs"][result["$ref"].rsplit("/", 1)[-1]]
    values = result["properties"]
    reference = next(iter(values.values()))["$ref"]
    return result["$defs"][reference.rsplit("/", 1)[-1]]


class _OpaqueDatatype(Datatype):
    """A decodable custom datatype with no stable library/type metadata."""

    supports_decode = True

    def __init__(self, value: object) -> None:
        self.value = value

    def allows(self, lexical: str, context: object) -> bool:
        return True

    def equal(self, left: str, left_context: object, right: str, right_context: object) -> bool:
        return left == right

    def decode(self, lexical: str, context: object) -> object:
        return self.value


class _NamedDatatype(Datatype):
    """A custom datatype with stable metadata and a selected value."""

    library_uri = "urn:custom"
    type_name = "value"
    parameters = ()
    supports_decode = True

    def __init__(self, value: object) -> None:
        self.value = value

    def allows(self, lexical: str, context: object) -> bool:
        return True

    def equal(self, left: str, left_context: object, right: str, right_context: object) -> bool:
        return left == right

    def decode(self, lexical: str, context: object) -> object:
        return self.value


class _NonDecodingDatatype(_NamedDatatype):
    supports_decode = False


class _Factory(DatatypeFactory):
    def __init__(self, value: object, *, decodable: bool = True, named: bool = False) -> None:
        self.value = value
        self.decodable = decodable
        self.named = named

    def create(self, type_name: str, parameters: tuple[Any, ...], context: object) -> Datatype:
        if self.named:
            return (
                _NamedDatatype(self.value) if self.decodable else _NonDecodingDatatype(self.value)
            )
        return _OpaqueDatatype(self.value)


def custom_schema(content: str, library: str, **factory: Any) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="r" datatypeLibrary="{library}">{content}</element>'
        ),
        datatype_libraries={library: DatatypeLibraryRegistration(_Factory(**factory), "custom/1")},
    )
    assert not schema.report.has_errors, schema.report.issues
    return schema


# ---------------------------------------------------------------------------
# Envelope, options, and the XSD boundary
# ---------------------------------------------------------------------------


def test_profile_envelope_and_disclosures() -> None:
    schema = schema_for(INTEGER)

    result = generated(schema, "r")

    assert result["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert result["x-pyxsd-format"] == "pyxsd-json"
    assert result["x-pyxsd-profile"] == "relaxng"
    assert result["x-pyxsd-profile-version"] == 1
    assert result["x-pyxsd-validation"]["level"] == "structural"
    reasons = result["x-pyxsd-validation"]["unvalidated"]
    assert reasons == sorted(reasons)
    assert {
        "relaxng-grammar-validity",
        "relaxng-name-class",
        "relaxng-scalar-candidates",
        "relaxng-datatype-context",
    } <= set(reasons)


def test_builder_and_schema_method_agree() -> None:
    schema = schema_for(INTEGER)

    assert build_relaxng_json_schema(schema, element="r") == schema.json_schema(element="r")


def test_fidelity_modes_generate_the_same_profile() -> None:
    schema = schema_for(f"<interleave>{A}{B}</interleave>")

    strict = schema.json_schema(element="r", fidelity="strict")
    best_effort = schema.json_schema(element="r", fidelity="best-effort")

    assert strict == best_effort


def test_invalid_format_and_fidelity_names_raise() -> None:
    schema = schema_for(INTEGER)

    with pytest.raises(ValueError, match="unknown data format"):
        schema.json_schema(format="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown fidelity"):
        schema.json_schema(fidelity="loose")  # type: ignore[arg-type]


def test_only_the_profile_format_is_available() -> None:
    schema = schema_for(INTEGER)

    for format in ("pyxsd", "parker", "badgerfish", "jsonml"):
        with pytest.raises(RelaxNGUnsupportedAdapterError):
            schema.json_schema(format=format)  # type: ignore[arg-type]
    assert RelaxNGUnsupportedAdapterError("parker").format == "parker"


def test_profile_rejects_native_only_options() -> None:
    schema = schema_for(INTEGER)

    with pytest.raises(ValueError, match="native-only"):
        schema.json_schema(typed=False)
    with pytest.raises(ValueError, match="native-only"):
        schema.json_schema(always_list=True)
    with pytest.raises(ValueError, match="native-only"):
        build_relaxng_json_schema(schema, format="parker", typed=False)


def test_native_options_are_allowed_for_the_native_name_then_rejected() -> None:
    schema = schema_for(INTEGER)

    with pytest.raises(RelaxNGUnsupportedAdapterError):
        schema.json_schema(format="pyxsd", typed=False)
    with pytest.raises(RelaxNGUnsupportedAdapterError):
        schema.json_schema(format="pyxsd", always_list=True)


def test_xsd_json_schema_output_is_unchanged() -> None:
    source = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"><xs:complexType>
    <xs:sequence><xs:element name="a" type="xs:boolean"/></xs:sequence>
  </xs:complexType></xs:element>
</xs:schema>"""
    schema = pyxsd.Schema.compile(io.StringIO(source))
    document = schema.parse(io.StringIO("<root><a>true</a></root>"))

    result = schema.json_schema(format="pyxsd-json", element="root")

    assert result["x-pyxsd-format"] == "pyxsd-json"
    assert "x-pyxsd-profile" not in result
    assert (
        list(Draft202012Validator(result).iter_errors(document.to_data(format="pyxsd-json"))) == []
    )


# ---------------------------------------------------------------------------
# Root selection and open name classes
# ---------------------------------------------------------------------------


def test_closed_root_spellings() -> None:
    schema = schema_for(INTEGER)
    expected = generated(schema, "r")

    for selector in (None, "r", "{}r", "Q{}r"):
        actual = generated(schema, selector)
        if selector is None:
            assert actual["x-pyxsd-qname"] == "r"
        else:
            assert actual == expected


def test_namespaced_closed_root_uses_eqname_keys() -> None:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="r" ns="urn:a"><empty/></element>')
    )

    for selector in (None, "{urn:a}r", "Q{urn:a}r"):
        result = generated(schema, selector)
        assert result["x-pyxsd-qname"] == "Q{urn:a}r"
        assert result["properties"] == {"Q{urn:a}r": result["properties"]["Q{urn:a}r"]}
    assert exported(schema, '<r xmlns="urn:a"/>') == {"Q{urn:a}r": ""}


def test_multiple_closed_roots_use_anyof_and_reject_other_names() -> None:
    schema = grammar_for(
        '<start><choice><element name="a"><empty/></element>'
        '<element name="b"><empty/></element></choice></start>'
    )

    result = generated(schema)
    assert "anyOf" in result
    assert_valid(schema, {"a": ""}, None)
    assert_valid(schema, {"b": ""}, None)
    assert errors(schema, {"c": ""}, None)
    assert_valid(schema, {"b": ""}, "b")
    assert errors(schema, {"a": ""}, "b")


def test_root_object_rejects_extra_or_missing_properties() -> None:
    schema = schema_for("<empty/>")

    assert errors(schema, {}, "r")
    assert errors(schema, {"r": "", "extra": ""}, "r")
    assert errors(schema, {"other": ""}, "r")


def test_root_selection_errors_are_the_exporter_errors() -> None:
    many = grammar_for(
        '<start><choice><element name="r" ns="urn:a"><empty/></element>'
        '<element name="r" ns="urn:b"><empty/></element></choice></start>'
    )
    with pytest.raises(PyXSDError, match="ambiguous"):
        generated(many, "r")
    with pytest.raises(PyXSDError, match="unresolved"):
        generated(many, "missing")
    with pytest.raises(PyXSDError, match="name-invalid"):
        generated(many, "1bad")
    with pytest.raises(PyXSDError, match="name-invalid"):
        generated(many, "{urn:a")
    with pytest.raises(PyXSDError, match="name-invalid"):
        generated(many, "Q{urn:a")


def test_open_start_classes_stay_symbolic_and_narrow_on_request() -> None:
    schema = schema_for(f"<anyName/>{STRING}", name="")
    result = generated(schema)
    assert "anyOf" not in result
    assert result["propertyNames"]["x-pyxsd-wildcard"] is True
    assert result["x-pyxsd-wildcard"] is True
    assert result["x-pyxsd-name-class"] == {"kind": "anyName"}
    assert "relaxng-wildcard-names" in unvalidated(schema)
    assert_valid(schema, {"Q{urn:a}r": ""}, None)
    assert_valid(schema, {"plain": ""}, None)
    assert errors(schema, {"bad:name": ""}, None)
    assert errors(schema, {"#value": ""}, None)
    with pytest.raises(PyXSDError, match="ambiguous"):
        generated(schema, "r")

    narrow = generated(schema, "Q{urn:a}r")
    assert narrow["x-pyxsd-qname"] == "Q{urn:a}r"
    assert narrow["additionalProperties"] is False


def test_nsname_open_root_accepts_local_narrowing() -> None:
    schema = schema_for(f'<nsName ns="urn:a"/>{STRING}', name="")

    result = generated(schema)
    assert result["x-pyxsd-name-class"] == {"kind": "nsName", "uri": "urn:a"}
    assert_valid(schema, {"Q{urn:a}r": ""}, None)
    assert_valid(schema, {"Q{urn:a}r": ""}, "r")


def test_open_root_reports_exclusions_and_named_siblings() -> None:
    schema = grammar_for(
        '<start><choice><element name="a"><empty/></element>'
        "<element><anyName><except><name>b</name></except></anyName><empty/></element>"
        "</choice></start>"
    )

    result = generated(schema)
    open_objects = [branch for branch in result["anyOf"] if "propertyNames" in branch]
    assert len(open_objects) == 1
    assert open_objects[0]["propertyNames"]["not"] == {"enum": ["a", "b"]}
    assert open_objects[0]["x-pyxsd-name-class"]["kind"] == "except"
    assert_valid(schema, {"a": ""}, None)
    assert_valid(schema, {"c": ""}, None)


def test_plan_without_roots_describes_nothing() -> None:
    plan = ProjectionPlan((), {}, "pyxsd.relaxng.projection-grammar/1:test")

    result = schema_from_plan(plan)

    assert result["not"] == {}
    assert "$defs" not in result


# ---------------------------------------------------------------------------
# Scalars: plain values and tagged alternatives
# ---------------------------------------------------------------------------


def test_plain_scalar_exports_validate() -> None:
    qname = schema_for('<data type="QName"/>')
    cases: list[tuple[RelaxNGSchema, str, object]] = [
        (schema_for(STRING), "<r/>", ""),
        (schema_for(INTEGER), "<r>12</r>", 12),
        (schema_for('<data type="boolean"/>'), "<r>true</r>", True),
        (schema_for('<data type="decimal"/>'), "<r>1.20</r>", "1.20"),
        (schema_for('<data type="float"/>'), "<r>1.5</r>", 1.5),
        (schema_for('<data type="float"/>'), "<r>INF</r>", "INF"),
        (schema_for('<data type="float"/>'), "<r>-INF</r>", "-INF"),
        (schema_for('<data type="float"/>'), "<r>NaN</r>", "NaN"),
        (qname, '<r xmlns:p="urn:x">p:a</r>', "Q{urn:x}a"),
    ]
    for case, xml, expected in cases:
        data = exported(case, xml)
        assert data == {"r": expected}
        assert_valid(case, data)


def test_scalar_type_rejections() -> None:
    integer = schema_for(INTEGER)
    boolean = schema_for('<data type="boolean"/>')
    decimal = schema_for('<data type="decimal"/>')
    floating = schema_for('<data type="float"/>')
    qname = schema_for('<data type="QName"/>')

    assert errors(integer, {"r": True})
    assert errors(integer, {"r": "12"})
    assert errors(integer, {"r": 12.5})
    assert errors(integer, {"r": [12]})
    assert errors(integer, {"r": {"#content": 12}})
    assert errors(boolean, {"r": 1})
    assert errors(boolean, {"r": "true"})
    assert errors(decimal, {"r": 1.2})
    assert errors(decimal, {"r": "1e3"})
    assert errors(floating, {"r": "+INF"})
    assert errors(floating, {"r": "Infinity"})
    assert errors(qname, {"r": "p:a"})
    assert errors(qname, {"r": "not a name"})
    for case in (integer, boolean, decimal, floating, qname):
        assert errors(case, {"r": None})


def test_text_scalar_accepts_the_tag_and_its_lexical_value() -> None:
    schema = schema_for("<text/>")

    tag = exported(schema, "<r>abc</r>")["r"]
    assert "#value" in tag
    assert_valid(schema, {"r": tag})
    assert_valid(schema, {"r": "abc"})
    assert "relaxng-scalar-tag-order" in unvalidated(schema)


def test_tagged_alternative_validates_and_does_not_freeze_identity() -> None:
    schema = schema_for(f"<choice>{INTEGER}{STRING}</choice>")

    data = exported(schema, "<r>12</r>")
    tag = data["r"]
    assert set(tag) == {"#value"}
    assert_valid(schema, data)
    serialized = json.dumps(generated(schema, "r"))
    assert "pyxsd.relaxng.candidate/1:" not in serialized
    assert tag["#value"]["grammar"] not in serialized
    tag_definition = root_body(generated(schema, "r"))["anyOf"][0]
    body = tag_definition["properties"]["#value"]
    assert body["properties"]["grammar"] == {"type": "string", "minLength": 1}
    assert "enum" not in body["properties"]["candidates"]
    assert "relaxng-scalar-tag-grammar-identity" in unvalidated(schema)
    assert "relaxng-scalar-tag-order" in unvalidated(schema)


def _tag(schema: RelaxNGSchema, xml: str = "<r>12</r>") -> dict[str, Any]:
    data = exported(schema, xml)
    return data["r"]


def test_tag_negative_cases() -> None:
    schema = schema_for(f"<choice>{INTEGER}{STRING}</choice>")
    tag = _tag(schema)
    body = tag["#value"]

    invalid: list[object] = [
        None,
        [],
        {},
        {"#value": body, "extra": 1},
        {**tag, "other": body},
        {"#value": {**body, "extra": 1}},
        {"#value": {k: v for k, v in body.items() if k != "grammar"}},
        {"#value": {**body, "version": 2}},
        {"#value": {**body, "version": "1"}},
        {"#value": {**body, "version": 1.5}},
        {"#value": {**body, "grammar": ""}},
        {"#value": {**body, "grammar": 1}},
        {"#value": {**body, "lexical": None}},
        {"#value": {**body, "candidates": []}},
        {"#value": {**body, "candidates": [body["candidates"][0], body["candidates"][0]]}},
        {"#value": {**body, "candidates": [""]}},
        {"#value": {**body, "candidates": [1]}},
        {"#value": {**body, "candidates": "abc"}},
        {"#value": {**body, "context": None}},
        {"#value": {**body, "context": {}}},
        {"#value": {**body, "context": {"base_uri": None}}},
        {"#value": {**body, "context": {"base_uri": None, "namespaces": {}, "x": 1}}},
        {"#value": {**body, "context": {"base_uri": 1, "namespaces": {}}}},
        {"#value": {**body, "context": {"base_uri": None, "namespaces": {"xml": "urn:x"}}}},
        {
            "#value": {
                **body,
                "context": {"base_uri": None, "namespaces": {"p": "urn:p", "xml": "urn:x"}},
            }
        },
        {"#value": {**body, "context": {"base_uri": None, "namespaces": {"p": 1}}}},
    ]
    for value in invalid:
        assert errors(schema, {"r": value}), value


def test_unsorted_candidates_are_accepted_and_disclosed() -> None:
    schema = schema_for(f"<choice>{INTEGER}{STRING}</choice>")
    tag = _tag(schema)
    body = tag["#value"]
    reversed_candidates = {"#value": {**body, "candidates": list(reversed(body["candidates"]))}}

    assert_valid(schema, {"r": reversed_candidates})
    assert "relaxng-scalar-tag-order" in unvalidated(schema)


def test_tags_are_rejected_in_nonscalar_positions() -> None:
    integer = schema_for(INTEGER)
    ambiguous = schema_for(f"<choice>{INTEGER}{STRING}</choice>")
    tag = _tag(ambiguous)
    record = schema_for(f"<interleave>{A}{B}</interleave>")
    sequence = schema_for(f"{A}{B}")
    empty = schema_for("<empty/>")
    repeated = schema_for(f"<zeroOrMore>{A}</zeroOrMore>")
    mixed = schema_for(f"<mixed><zeroOrMore>{A}</zeroOrMore></mixed>")

    assert errors(integer, {"r": tag})
    assert errors(record, {"r": {"#value": tag["#value"]}})
    assert errors(sequence, {"r": [tag]})
    assert errors(empty, {"r": tag})
    assert errors(repeated, {"r": [tag]})
    assert errors(mixed, {"r": [tag]})


def test_multiple_contracts_use_anyof_with_tag_and_plain_values() -> None:
    schema = schema_for(f"<choice>{INTEGER}{STRING}</choice>")
    branches = root_body(generated(schema, "r"))["anyOf"]

    assert {branch.get("type") for branch in branches} >= {"integer", "string"}
    assert any(branch.get("properties", {}).get("#value") for branch in branches)
    assert_valid(schema, {"r": 12})
    assert_valid(schema, {"r": "twelve"})


def test_empty_scalar_fallback_is_the_empty_string() -> None:
    schema = schema_for(f"<choice><empty/>{INTEGER}</choice>")

    assert exported(schema, "<r/>") == {"r": ""}
    assert_valid(schema, {"r": ""})
    assert_valid(schema, {"r": 12})
    assert errors(schema, {"r": None})


def test_custom_nondecoding_scalar_uses_the_tag() -> None:
    schema = custom_schema(
        '<data type="value" datatypeLibrary="urn:custom"/>',
        "urn:custom",
        value="x",
        decodable=False,
        named=True,
    )

    data = exported(schema, "<r>x</r>")
    assert "#value" in data["r"]
    assert_valid(schema, data)


def test_custom_decodable_scalar_uses_a_disclosed_sound_union() -> None:
    schema = custom_schema(
        '<data type="value" datatypeLibrary="urn:custom"/>',
        "urn:custom",
        value="x",
        decodable=True,
        named=True,
    )

    result = generated(schema, "r")
    assert root_body(result)["$ref"] == "#/$defs/opaque-scalar"
    union = result["$defs"]["opaque-scalar"]["anyOf"]
    assert {branch.get("type") for branch in union} == {
        "array",
        "boolean",
        "number",
        "string",
    }
    assert "relaxng-custom-scalar" in unvalidated(schema)
    assert_valid(schema, exported(schema, "<r>x</r>"))
    for value in ("x", 1, 1.5, True, [], [1, "x", [True]]):
        assert_valid(schema, {"r": value})
    for value in ({"not": "a scalar"}, [None], [{}], [1, [None]]):
        assert errors(schema, {"r": value})


def test_list_scalars_are_arrays_not_repeated_children() -> None:
    homogeneous = schema_for('<list><oneOrMore><data type="integer"/></oneOrMore></list>')
    fixed = schema_for('<list><group><data type="integer"/><data type="string"/></group></list>')
    empty = schema_for("<list><empty/></list>")

    assert exported(homogeneous, "<r>1 2</r>") == {"r": [1, 2]}
    assert exported(fixed, "<r>1 two</r>") == {"r": [1, "two"]}
    assert exported(empty, "<r/>") == {"r": []}
    for case, xml in (
        (homogeneous, "<r>1 2</r>"),
        (fixed, "<r>1 two</r>"),
        (empty, "<r/>"),
    ):
        assert_valid(case, exported(case, xml))
    assert errors(homogeneous, {"r": ["1", "2"]})
    assert errors(homogeneous, {"r": [[1]]})
    assert errors(fixed, {"r": [1]})
    assert errors(empty, {"r": [1]})


def test_homogeneous_list_token_minimums() -> None:
    optional = schema_for('<list><zeroOrMore><data type="integer"/></zeroOrMore></list>')
    required = schema_for('<list><oneOrMore><data type="integer"/></oneOrMore></list>')

    assert exported(optional, "<r/>") == {"r": []}
    assert_valid(optional, {"r": []})
    assert_valid(required, {"r": [1]})
    assert errors(required, {"r": []})
    assert root_body(generated(required, "r"))["minItems"] == 1
    assert "minItems" not in root_body(generated(optional, "r"))


def test_xsd_list_valued_atomics_require_a_token() -> None:
    for type_name in ("IDREFS", "ENTITIES", "NMTOKENS"):
        schema = schema_for(f'<data type="{type_name}"/>')
        body = root_body(generated(schema, "r"))
        assert body["type"] == "array"
        assert body["minItems"] == 1
        assert errors(schema, {"r": []})
        assert_valid(schema, {"r": ["a"]})
    assert exported(schema_for('<data type="IDREFS"/>'), "<r>a b</r>") == {"r": ["a", "b"]}


def test_nonplain_list_uses_the_whole_list_tag() -> None:
    schema = schema_for('<list><choice><data type="integer"/><data type="string"/></choice></list>')

    data = exported(schema, "<r>12</r>")
    assert "#value" in data["r"]
    assert_valid(schema, data)
    assert "relaxng-scalar-tag-order" in unvalidated(schema)


def test_nondecodable_list_uses_the_whole_list_tag() -> None:
    schema = custom_schema(
        '<list><oneOrMore><data type="value" datatypeLibrary="urn:custom"/></oneOrMore></list>',
        "urn:custom",
        value="x",
        decodable=False,
        named=True,
    )

    data = exported(schema, "<r>x y</r>")
    assert "#value" in data["r"]
    assert_valid(schema, data)


def test_custom_list_token_uses_the_sound_union() -> None:
    schema = custom_schema(
        '<list><oneOrMore><data type="value" datatypeLibrary="urn:custom"/></oneOrMore></list>',
        "urn:custom",
        value=5,
        named=True,
    )

    result = generated(schema, "r")
    body = root_body(result)
    assert body["type"] == "array"
    assert body["items"] == {"$ref": "#/$defs/opaque-scalar"}
    assert "relaxng-custom-scalar" in unvalidated(schema)


# ---------------------------------------------------------------------------
# Route layouts
# ---------------------------------------------------------------------------


def test_empty_layout_is_string_or_attribute_object() -> None:
    plain = schema_for("<empty/>")
    optional = schema_for(f"{ATTR}<empty/>")
    required = schema_for(f"{REQUIRED_ATTR}<empty/>")
    wildcard = schema_for(WILDCARD_ATTR)

    assert exported(plain, "<r/>") == {"r": ""}
    assert_valid(plain, {"r": ""})
    assert errors(plain, {"r": "x"})
    assert errors(plain, {"r": None})
    assert errors(plain, {"r": {}})

    assert exported(optional, "<r/>") == {"r": {}}
    assert_valid(optional, {"r": {}})
    assert_valid(optional, {"r": {"@z": 3}})
    assert errors(optional, {"r": {"#content": 3}})

    assert exported(required, '<r z="3"/>') == {"r": {"@z": 3}}
    assert errors(required, {"r": {}})

    assert_valid(wildcard, {"r": {}})
    assert_valid(wildcard, {"r": {"@a": "x"}})
    assert errors(wildcard, {"r": {"@xmlns": "urn:x"}})
    assert errors(wildcard, {"r": {"@Q{http://www.w3.org/2000/xmlns/}p": "urn:x"}})


def test_scalar_layout_with_attributes_and_required_content() -> None:
    optional = schema_for(f"{ATTR}{INTEGER}")
    required = schema_for(f"{REQUIRED_ATTR}{INTEGER}")

    assert exported(optional, "<r>12</r>") == {"r": {"#content": 12}}
    assert_valid(optional, {"r": {"#content": 12}})
    assert_valid(optional, {"r": {"@z": 3, "#content": 12}})
    assert errors(optional, {"r": {"@z": 3}})
    assert errors(optional, {"r": {"#content": "12"}})
    assert errors(optional, {"r": {"#content": 12, "extra": 1}})

    assert_valid(required, {"r": {"@z": 3, "#content": 12}})
    assert errors(required, {"r": {"#content": 12}})


def test_generic_reason_route_uses_the_ordered_array() -> None:
    schema = schema_for(
        f'<attribute name="z"><data type="integer"/></attribute><choice>{INTEGER}{A}</choice>'
    )

    data = exported(schema, '<r z="3">12</r>')
    assert data == {"r": [{"@z": 3}, "12"]}
    assert_valid(schema, data)
    assert "relaxng-generic-route" in unvalidated(schema)
    assert root_body(generated(schema, "r"))["x-pyxsd-wildcard"] is True


def test_repeated_layout_is_a_bare_or_attribute_array() -> None:
    bare = schema_for(f"<zeroOrMore>{A}</zeroOrMore>")
    many = schema_for(f"<oneOrMore>{A}</oneOrMore>")
    attributed = schema_for(f"{ATTR}<zeroOrMore>{A}</zeroOrMore>")

    assert exported(bare, "<r><a>A</a></r>") == {"r": ["A"]}
    assert exported(bare, "<r/>") == {"r": []}
    assert_valid(bare, {"r": []})
    assert_valid(bare, {"r": ["A"]})
    assert errors(bare, {"r": {"a": ["A"]}})
    assert errors(many, {"r": []})

    data = exported(attributed, '<r z="3"><a>A</a></r>')
    assert data == {"r": {"@z": 3, "a": ["A"]}}
    assert_valid(attributed, data)
    assert_valid(attributed, {"r": {"a": []}})
    assert errors(attributed, {"r": {"@z": 3}})
    assert errors(attributed, {"r": ["A"]})


def test_repeated_layout_cardinality_limits() -> None:
    schema = schema_for(f"<zeroOrMore>{A}</zeroOrMore>")
    route_id = schema.projection.roots[0]
    field = next(iter(schema.projection.routes[route_id].children.values()))
    plan = plan_with(
        schema,
        children={"a": replace(field, cardinality=Cardinality(1, 2))},
    )
    result = schema_from_plan(plan, "r")
    body = root_body(result)

    assert body["minItems"] == 1
    assert body["maxItems"] == 2
    validator = Draft202012Validator(result)
    assert list(validator.iter_errors({"r": []}))
    assert list(validator.iter_errors({"r": ["A", "B", "C"]}))
    assert list(validator.iter_errors({"r": ["A"]})) == []


def test_record_layout_requires_only_statically_required_fields() -> None:
    schema = schema_for(f"<interleave>{A}<optional>{B}</optional></interleave>")
    attributed = schema_for(
        f'<attribute name="q"><data type="string"/></attribute><interleave>{A}{B}</interleave>'
    )

    result = root_body(generated(schema, "r"))
    assert result["required"] == ["a"]
    assert_valid(schema, {"r": {"a": "A"}})
    assert_valid(schema, {"r": {"a": "A", "b": "B"}})
    assert errors(schema, {"r": {"b": "B"}})
    assert errors(schema, {"r": {"a": "A", "extra": "x"}})
    assert errors(schema, {"r": ["A"]})

    assert_valid(attributed, {"r": {"@q": "Q", "a": "A", "b": "B"}})
    assert errors(attributed, {"r": {"a": "A", "b": "B"}})


def test_interleave_with_open_attributes_stays_a_record() -> None:
    schema = schema_for(f"{WILDCARD_ATTR}<interleave>{A}{B}</interleave>")

    data = exported(schema, '<r n="1"><b>B</b><a>A</a></r>')

    assert data == {"r": {"@n": "1", "b": "B", "a": "A"}}
    assert_valid(schema, data)
    assert errors(schema, {"r": {"a": "A", "b": "B", "extra": "x"}})


def test_interleave_with_a_repeatable_field_is_an_ordered_array() -> None:
    schema = schema_for(f"<interleave><zeroOrMore>{A}</zeroOrMore>{B}</interleave>")

    data = exported(schema, "<r><a>A</a><b>B</b><a>C</a></r>")

    assert data == {"r": [{"a": "A"}, {"b": "B"}, {"a": "C"}]}
    assert_valid(schema, data)
    assert errors(schema, {"r": {"a": "A", "b": "B"}})
    assert "relaxng-particle-order" in unvalidated(schema)


def test_sequence_layout_is_an_ordered_array_only() -> None:
    schema = schema_for(f'<attribute name="q"><data type="string"/></attribute>{A}{B}')
    plain = schema_for(f"{A}{B}")

    data = exported(schema, '<r q="Q"><a>A</a><b>B</b></r>')
    assert data == {"r": [{"@q": "Q"}, {"a": "A"}, {"b": "B"}]}
    assert_valid(schema, data)
    assert_valid(plain, {"r": []})
    assert_valid(plain, {"r": [{"a": "A"}, {"b": "B"}]})
    # Count and order are disclosed rather than encoded in the array.
    assert_valid(plain, {"r": [{"a": "A"}, {"a": "B"}]})
    assert errors(plain, {"r": ["A"]})
    assert errors(plain, {"r": {"a": "A"}})
    assert "relaxng-particle-order" in unvalidated(plain)


def test_mixed_layout_is_an_ordered_text_and_child_array() -> None:
    schema = schema_for(f"{WILDCARD_ATTR}<mixed><zeroOrMore>{A}</zeroOrMore></mixed>")

    data = exported(schema, '<r z="3">before<a>A</a>after</r>')
    assert data == {"r": [{"@z": "3"}, "before", {"a": "A"}, "after"]}
    assert_valid(schema, data)
    assert errors(schema, {"r": {"a": "A"}})
    assert errors(schema, {"r": [{"#value": {}}]})
    assert "relaxng-mixed-order" in unvalidated(schema)


def test_generic_route_is_a_disclosed_ordered_array() -> None:
    schema = schema_for(f"<choice>{INTEGER}{A}</choice>")

    text = exported(schema, "<r>12</r>")
    child = exported(schema, "<r><a>A</a></r>")
    assert text == {"r": ["12"]}
    assert child == {"r": [{"a": "A"}]}
    assert_valid(schema, text)
    assert_valid(schema, child)
    assert errors(schema, {"r": 12})
    assert errors(schema, {"r": {"a": "A"}})
    assert "relaxng-generic-route" in unvalidated(schema)


def test_unknown_child_route_uses_the_recursive_generic_envelope() -> None:
    schema = schema_for(f"<zeroOrMore>{A}</zeroOrMore>")
    plan = plan_with(schema, children={})

    data = exported(schema, "<r><a>x</a></r>", plan=plan)
    assert data == {"r": [{"a": ["x"]}]}
    result = schema_from_plan(plan, "r")
    assert list(Draft202012Validator(result).iter_errors(data)) == []
    assert list(Draft202012Validator(result).iter_errors({"r": ["x"]}))
    assert list(Draft202012Validator(result).iter_errors({"r": [{"a": "x"}]}))


def test_two_missing_child_routes_share_one_generic_definition() -> None:
    schema = schema_for(f"{A}{B}")
    plan = plan_with(schema, children={})

    result = schema_from_plan(plan, "r")

    assert "generic-route" in result["$defs"]
    assert result["$defs"]["generic-route"]["items"]["anyOf"][2]["additionalProperties"] == {
        "$ref": "#/$defs/generic-route"
    }


def test_repeated_field_without_a_child_route_uses_the_generic_envelope() -> None:
    schema = schema_for(f"<zeroOrMore>{A}</zeroOrMore>")
    route_id = schema.projection.roots[0]
    field = next(iter(schema.projection.routes[route_id].children.values()))
    plan = plan_with(schema, children={"a": replace(field, child_route_id=None)})

    data = exported(schema, "<r><a>x</a></r>", plan=plan)
    assert data == {"r": [["x"]]}
    result = schema_from_plan(plan, "r")
    assert list(Draft202012Validator(result).iter_errors(data)) == []


def test_child_wildcards_cover_scalar_empty_and_structured_routes() -> None:
    scalar = schema_for(
        '<zeroOrMore><element><anyName/><data type="string"/></element></zeroOrMore>'
    )
    empty = schema_for("<zeroOrMore><element><anyName/><empty/></element></zeroOrMore>")
    record = schema_for(
        f"<zeroOrMore><element><anyName/><interleave>{A}{B}</interleave></element></zeroOrMore>"
    )

    scalar_data = exported(scalar, '<r><x xmlns="urn:c">12</x><y/></r>')
    assert scalar_data == {"r": [{"Q{urn:c}x": {"#content": "12"}}, {"y": {"#content": ""}}]}
    assert_valid(scalar, scalar_data)
    assert "relaxng-wildcard-names" in unvalidated(scalar)

    empty_data = exported(empty, "<r><x/></r>")
    assert empty_data == {"r": [{"x": []}]}
    assert_valid(empty, empty_data)

    record_data = exported(record, "<r><x><a>A</a><b>B</b></x></r>")
    assert record_data == {"r": [{"x": [{"a": "A"}, {"b": "B"}]}]}
    assert_valid(record, record_data)


def test_wildcard_child_without_a_route_uses_the_generic_envelope() -> None:
    schema = schema_for("<zeroOrMore><element><anyName/><empty/></element></zeroOrMore>")
    route_id = schema.projection.roots[0]
    route = schema.projection.routes[route_id]
    wildcard = route.wildcards[0]
    plan = replace(
        schema.projection,
        routes={
            **schema.projection.routes,
            route_id: replace(route, wildcards=(replace(wildcard, child_route_id=None),)),
        },
    )

    data = exported(schema, "<r><x/></r>", plan=plan)
    assert data == {"r": [{"x": []}]}
    result = schema_from_plan(plan, "r")
    assert list(Draft202012Validator(result).iter_errors(data)) == []


def test_attribute_with_empty_content_stays_a_lexical_string() -> None:
    schema = schema_for(f'<attribute name="e"><empty/></attribute>{STRING}')

    data = exported(schema, '<r e="">x</r>')

    assert data == {"r": {"@e": "", "#content": "x"}}
    assert_valid(schema, data)


def test_record_with_only_optional_fields_has_no_required_member() -> None:
    schema = schema_for(
        f"<interleave><optional>{A}</optional><optional>{B}</optional></interleave>"
    )

    body = root_body(generated(schema, "r"))

    assert "required" not in body
    assert exported(schema, "<r/>") == {"r": {}}
    assert_valid(schema, {"r": {}})
    assert_valid(schema, {"r": {"a": "A"}})


def test_wildcard_attribute_values_stay_lexical_strings() -> None:
    schema = schema_for(f"{WILDCARD_ATTR}{INTEGER}")

    data = exported(schema, '<r other="12" xml:lang="en" xmlns:p="urn:p">3</r>')
    assert data == {
        "r": {
            "@other": "12",
            "@Q{http://www.w3.org/XML/1998/namespace}lang": "en",
            "#content": 3,
        }
    }
    assert_valid(schema, data)
    assert errors(schema, {"r": {"@xmlns": "urn:x", "#content": 3}})
    assert errors(schema, {"r": {"@bad name": "x", "#content": 3}})


def test_attribute_scalar_tags_follow_element_content_contracts() -> None:
    schema = schema_for(f'<attribute name="q"><data type="integer"/></attribute>{STRING}')

    data = exported(schema, '<r q="3">x</r>')
    assert "#value" in data["r"]["@q"]
    assert data["r"]["#content"] == "x"
    assert_valid(schema, data)


def test_two_unknown_record_field_routes_share_the_generic_definition() -> None:
    schema = schema_for(f"<interleave>{A}{B}</interleave>")
    route_id = schema.projection.roots[0]
    route = schema.projection.routes[route_id]
    children = {name: replace(field, child_route_id=None) for name, field in route.children.items()}
    plan = plan_with(schema, children=children)

    result = schema_from_plan(plan, "r")
    body = root_body(result)

    assert body["properties"]["a"] == {"$ref": "#/$defs/generic-route"}
    assert body["properties"]["b"] == {"$ref": "#/$defs/generic-route"}


def test_duplicate_child_wildcards_do_not_repeat_a_branch() -> None:
    schema = schema_for(
        "<mixed><zeroOrMore><element><anyName/><empty/></element></zeroOrMore></mixed>"
    )
    route_id = schema.projection.roots[0]
    route = schema.projection.routes[route_id]
    wildcard = route.wildcards[0]
    plan = plan_with(schema, wildcards=(wildcard, wildcard))

    result = schema_from_plan(plan, "r")
    child_map = next(
        item
        for item in root_body(result)["items"]["anyOf"]
        if item.get("x-pyxsd-wildcard") and "propertyNames" in item
    )

    reference = child_map["additionalProperties"]
    assert re.fullmatch(r"#/\$defs/route-\d+-generic", reference["$ref"])
    assert reference["$ref"].rsplit("/", 1)[-1] in result["$defs"]


ATTR_PAIR = (
    '<attribute name="q"><data type="integer"/></attribute>'
    "<zeroOrMore><attribute><anyName><except><name>q</name></except></anyName>"
    "<text/></attribute></zeroOrMore>"
)


@pytest.mark.parametrize(
    ("content", "xml"),
    [
        ("<empty/>", '<r q="3" extra="x"/>'),
        ('<data type="integer"/>', '<r q="3" extra="x">4</r>'),
        (f"<zeroOrMore>{A}</zeroOrMore>", '<r q="3" extra="x"><a>A</a></r>'),
        (f"<interleave>{A}{B}</interleave>", '<r q="3" extra="x"><a>A</a><b>B</b></r>'),
        (f"{A}{B}", '<r q="3" extra="x"><a>A</a><b>B</b></r>'),
        (f"<mixed><zeroOrMore>{A}</zeroOrMore></mixed>", '<r q="3" extra="x">t<a>A</a></r>'),
    ],
    ids=["empty", "scalar", "repeated", "record", "sequence", "mixed"],
)
def test_attribute_wildcard_does_not_constrain_declared_typed_attributes(
    content: str, xml: str
) -> None:
    schema = schema_for(ATTR_PAIR + content)

    data = exported(schema, xml)
    assert_valid(schema, data)
    assert "@extra" in json.dumps(data)
    assert "@q" in json.dumps(data)
    bad = copy.deepcopy(data["r"])
    if isinstance(bad, list):
        attribute_map = next(item for item in bad if any(k.startswith("@") for k in item))
        attribute_map["@xmlns"] = "urn:x"
    else:
        bad["@xmlns"] = "urn:x"
    assert errors(schema, {"r": bad})
    xmlns_name = (
        next(key for key in json.dumps(data).split('"') if key.startswith("@Q{http"))
        if False
        else "@Q{http://www.w3.org/2000/xmlns/}p"
    )
    bad = copy.deepcopy(data["r"])
    if isinstance(bad, list):
        attribute_map = next(item for item in bad if any(k.startswith("@") for k in item))
        attribute_map[xmlns_name] = "urn:x"
    else:
        bad[xmlns_name] = "urn:x"
    assert errors(schema, {"r": bad})
    assert "relaxng-attribute-name-class" in unvalidated(schema)


def test_opaque_scalar_definition_is_shared_across_positions() -> None:
    schema = custom_schema(
        '<attribute name="a"><data type="value" datatypeLibrary="urn:custom"/></attribute>'
        '<attribute name="b"><data type="value" datatypeLibrary="urn:custom"/></attribute>'
        "<empty/>",
        "urn:custom",
        value="x",
        named=True,
    )

    result = generated(schema, "r")
    body = root_body(result)
    assert body["properties"]["@a"]["$ref"] == "#/$defs/opaque-scalar"
    assert body["properties"]["@b"]["$ref"] == "#/$defs/opaque-scalar"
    assert list(result["$defs"]).count("opaque-scalar") == 1
    data = exported(schema, '<r a="x" b="y"/>')
    assert data == {"r": {"@a": "x", "@b": "x"}}
    assert_valid(schema, data)


@pytest.mark.parametrize(
    ("attribute_content", "xml", "expected"),
    [
        ('<data type="boolean"/>', '<r q="true" extra="x"/>', True),
        (
            '<list><oneOrMore><data type="integer"/></oneOrMore></list>',
            '<r q="1 2" extra="x"/>',
            [1, 2],
        ),
        (
            '<choice><data type="integer"/><data type="string"/></choice>',
            '<r q="12" extra="x"/>',
            "tag",
        ),
    ],
    ids=["boolean", "list", "tag"],
)
def test_attribute_wildcard_keeps_declared_boolean_list_and_tag_values(
    attribute_content: str, xml: str, expected: object
) -> None:
    schema = schema_for(
        f'<attribute name="q">{attribute_content}</attribute>'
        "<zeroOrMore><attribute><anyName><except><name>q</name></except></anyName>"
        "<text/></attribute></zeroOrMore><empty/>"
    )

    data = exported(schema, xml)

    assert_valid(schema, data)
    value = data["r"]["@q"]
    if expected == "tag":
        assert "#value" in value
    else:
        assert value == expected


def test_attribute_wildcard_still_rejects_a_wrong_declared_value() -> None:
    schema = schema_for(f"{ATTR_PAIR}<empty/>")

    assert_valid(schema, {"r": {"@q": 3, "@extra": "x"}})
    assert errors(schema, {"r": {"@q": "3", "@extra": "x"}})
    assert errors(schema, {"r": {"@q": 3, "@extra": 1}})


def test_attribute_wildcard_name_class_metadata() -> None:
    schema = schema_for(
        '<zeroOrMore><attribute><nsName ns="urn:a"/><text/></attribute></zeroOrMore><empty/>'
    )

    body = root_body(generated(schema, "r"))

    assert body["x-pyxsd-name-class"] == [{"kind": "nsName", "uri": "urn:a"}]
    assert "relaxng-attribute-name-class" in unvalidated(schema)
    data = exported(schema, '<r xmlns:p="urn:a" p:x="1"/>')
    assert data == {"r": {"@Q{urn:a}x": "1"}}
    assert_valid(schema, data)

    excluded = schema_for(f"{ATTR_PAIR}<empty/>")
    excluded_body = root_body(generated(excluded, "r"))
    assert excluded_body["x-pyxsd-name-class"][0]["kind"] == "except"
    assert excluded_body["x-pyxsd-name-class"][0]["excluded"]["local"] == "q"


def test_sequence_with_an_unknown_child_model_uses_a_generic_map() -> None:
    schema = schema_for(f"{A}{B}")
    plan = plan_with(schema, children={})

    data = exported(schema, "<r><a>A</a><b>B</b></r>", plan=plan)
    assert data == {"r": [{"a": ["A"]}, {"b": ["B"]}]}
    result = schema_from_plan(plan, "r")
    assert list(Draft202012Validator(result).iter_errors(data)) == []
    assert list(Draft202012Validator(result).iter_errors({"r": ["A"]}))


# ---------------------------------------------------------------------------
# Recursion, determinism, and structural limits
# ---------------------------------------------------------------------------

RECURSIVE = f"""\
<start><ref name="node"/></start>
<define name="node"><element name="node">
  <optional><attribute name="id">{INTEGER}</attribute></optional>
  <zeroOrMore><ref name="node"/></zeroOrMore>
</element></define>"""


def test_recursive_route_emits_a_ref_cycle_and_validates_depth_two() -> None:
    schema = grammar_for(RECURSIVE)
    result = generated(schema, "node")
    assert any(
        definition.get("properties", {}).get("node", {}).get("items") == {"$ref": "#/$defs/route-1"}
        for definition in result["$defs"].values()
    )
    data = exported(schema, '<node id="1"><node id="2"/></node>')
    assert data == {"node": {"@id": 1, "node": [{"@id": 2, "node": []}]}}
    assert_valid(schema, data, "node")
    assert errors(schema, {"node": {"@id": 1, "node": [{"@id": 2, "extra": 1}]}}, "node")


RECURSIVE_ANY = """\
<start><ref name="node"/></start>
<define name="node"><element><anyName/>
  <zeroOrMore><ref name="node"/></zeroOrMore>
</element></define>"""


def test_recursive_anyname_route_emits_a_finite_ref_cycle() -> None:
    schema = grammar_for(RECURSIVE_ANY)

    result = generated(schema)
    route = next(iter(result["$defs"].values()))
    wildcard_map = next(
        branch
        for branch in route["items"]["anyOf"]
        if branch.get("x-pyxsd-wildcard") and "propertyNames" in branch
    )
    assert wildcard_map["additionalProperties"] == {"$ref": "#/$defs/route-1"}
    data = exported(schema, "<n0><n1><n2/></n1></n0>")
    assert data == {"n0": [{"n1": [{"n2": []}]}]}
    assert_valid(schema, data)

    depth = 60
    xml = "".join(f"<n{index}>" for index in range(depth - 1))
    xml += f"<n{depth - 1}/>" + "".join(f"</n{index}>" for index in reversed(range(depth - 1)))
    deep = exported(schema, xml)
    assert_valid(schema, deep)


RECURSIVE_GENERIC_VARIANT = """\
<start><ref name="A"/></start>
<define name="A"><element name="a"><ref name="B"/></element></define>
<define name="B"><element name="b"><zeroOrMore><element><anyName/>
  <ref name="A"/>
</element></zeroOrMore></element></define>"""


def test_generic_variant_definition_is_registered_before_recursive_fill() -> None:
    schema = grammar_for(RECURSIVE_GENERIC_VARIANT)

    result = generated(schema, "a")
    keys = [key for key in result["$defs"] if key.endswith("-generic")]
    assert len(keys) == 1
    assert re.fullmatch(r"route-\d+-generic", keys[0])

    data = exported(schema, "<a><b><x><a><b/></a></x></b></a>")
    assert data == {"a": [{"b": [{"x": [{"a": [{"b": []}]}]}]}]}
    assert_valid(schema, data, "a")


def test_deep_element_chains_finish_without_unbounded_expansion() -> None:
    depth = 60
    content = '<data type="string"/>'
    for index in range(depth - 1, 0, -1):
        content = f'<element name="n{index}">{content}</element>'
    schema = schema_for(content, name='name="n0"')

    result = generated(schema, "n0")
    assert len(result["$defs"]) >= depth
    data = "".join(f"<n{index}>" for index in range(depth))
    data += "x"
    data += "".join(f"</n{index}>" for index in reversed(range(depth)))
    assert_valid(schema, exported(schema, data), "n0")


def test_generation_is_deterministic_across_runs_and_paths(tmp_path: Path) -> None:
    source = (
        f'<element xmlns="{NS}" datatypeLibrary="{XSD}" name="r">'
        f"<interleave>{A}{B}</interleave></element>"
    )
    first = RelaxNGSchema.compile(io.StringIO(source))
    second = RelaxNGSchema.compile(io.StringIO(source))
    path = tmp_path / "schema.rng"
    path.write_text(source, encoding="utf-8")
    third = RelaxNGSchema.compile(path)

    expected = first.json_schema(element="r")
    assert second.json_schema(element="r") == expected
    assert third.json_schema(element="r") == expected


def test_hash_seed_does_not_change_generated_schemas() -> None:
    script = f"""
import io, json
from pyxsd.relaxng import RelaxNGSchema

schema = RelaxNGSchema.compile(io.StringIO('''\
<element xmlns="{NS}" datatypeLibrary="{XSD}" name="r">
  <interleave>{A}{B}</interleave>
</element>'''))
print(json.dumps(schema.json_schema(element="r"), sort_keys=True))
"""
    outputs = []
    for seed in ("0", "1"):
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]


def test_unused_definitions_do_not_freeze_identity_into_the_schema() -> None:
    plain = schema_for(INTEGER)
    with_definition = grammar_for(
        f'<start><element name="r">{INTEGER}</element></start>'
        '<define name="unused"><element name="x"><empty/></element></define>'
    )

    assert plain.json_schema(element="r") == with_definition.json_schema(element="r")


def test_defs_keys_are_allocated_from_sorted_route_ids() -> None:
    schema = schema_for(f"<interleave>{A}{B}</interleave>")

    result = generated(schema, "r")

    assert list(result["$defs"]) == ["route-1", "route-2", "route-3"]


def test_root_route_helper_refuses_an_unreachable_name() -> None:
    from pyxsd.relaxng.json_schema import _RelaxNGJSONSchemaBuilder

    builder = _RelaxNGJSONSchemaBuilder(schema_for(INTEGER).projection)

    with pytest.raises(AssertionError):
        builder._root_route_id("{urn:missing}name")


# ---------------------------------------------------------------------------
# Private static-analysis branches
# ---------------------------------------------------------------------------


def test_admitted_names_cover_closed_and_open_classes() -> None:
    assert _admitted_names(Name(None, "a")) == ("a",)
    assert _admitted_names(NameChoice((Name(None, "b"), Name("urn:a", "a")))) == (
        "b",
        "{urn:a}a",
    )
    assert _admitted_names(NameChoice((Name(None, "b"), AnyName()))) is None
    assert _admitted_names(ExceptName(Name(None, "a"), Name(None, "a"))) == ()
    assert _admitted_names(ExceptName(AnyName(), Name(None, "b"))) is None
    assert _admitted_names(NsName("urn:a")) is None


def test_excluded_and_mentioned_names_cover_open_compositions() -> None:
    assert _excluded_eqnames(ExceptName(AnyName(), Name(None, "b"))) == ("b",)
    assert _excluded_eqnames(
        NameChoice(
            (
                ExceptName(AnyName(), Name(None, "b")),
                ExceptName(AnyName(), Name(None, "a")),
            )
        )
    ) == ("a", "b")
    assert _excluded_eqnames(Name(None, "a")) == ()
    assert _excluded_eqnames(
        ExceptName(ExceptName(AnyName(), Name(None, "a")), Name(None, "b"))
    ) == ("b", "a")
    assert _mentioned_names(NsName("urn:a")) == ()
    assert _mentioned_names(ExceptName(AnyName(), Name(None, "b"))) == ("b",)
    assert _mentioned_names(NameChoice((AnyName(), Name(None, "b")))) == ("b",)
    assert _mentioned_names(AnyName()) == ()


def test_name_class_metadata_is_symbolic_and_total() -> None:
    assert _name_class_metadata(AnyName()) == {"kind": "anyName"}
    assert _name_class_metadata(NsName("urn:a")) == {"kind": "nsName", "uri": "urn:a"}
    assert _name_class_metadata(Name("urn:a", "x")) == {
        "kind": "name",
        "uri": "urn:a",
        "local": "x",
    }
    assert _name_class_metadata(NameChoice((Name(None, "b"), AnyName()))) == {
        "kind": "choice",
        "alternatives": [{"kind": "anyName"}, {"kind": "name", "uri": None, "local": "b"}],
    }
    assert _name_class_metadata(ExceptName(AnyName(), Name(None, "b"))) == {
        "kind": "except",
        "base": {"kind": "anyName"},
        "excluded": {"kind": "name", "uri": None, "local": "b"},
    }

    class _Unknown(NameClass):
        pass

    with pytest.raises(TypeError, match="unknown RELAX NG name class"):
        _name_class_metadata(_Unknown())


def test_child_model_erases_attributes_and_keeps_single_operands() -> None:
    attribute = ContentContract("attribute")
    empty = ContentContract("empty")
    scalar = ContentContract("scalar")

    assert _child_model(attribute) is None
    assert _child_model(empty) is None
    collapsed = _child_model(ContentContract("group", (attribute, scalar)))
    assert collapsed is not None and collapsed.kind == "scalar"
    assert _child_model(ContentContract("choice", (attribute, empty))) is None
    retained = _child_model(ContentContract("choice", (scalar, attribute)))
    assert retained is not None and retained.kind == "choice"
    assert len(retained.operands) == 1
    assert _child_model(ContentContract("repeat", (attribute,))) is None
    assert _child_model(ContentContract("notAllowed")).kind == "notAllowed"


def test_may_be_empty_covers_every_contract_kind() -> None:
    attribute = ContentContract("attribute")
    empty = ContentContract("empty")
    scalar = ContentContract("scalar")
    element = ContentContract("element")
    not_allowed = ContentContract("notAllowed")

    assert _may_be_empty(attribute)
    assert _may_be_empty(empty)
    assert _may_be_empty(element)
    assert not _may_be_empty(scalar)
    assert not _may_be_empty(not_allowed)
    assert not _may_be_empty(ContentContract("group", (attribute, scalar)))
    assert _may_be_empty(ContentContract("interleave", (attribute, empty)))
    assert _may_be_empty(ContentContract("choice", (attribute, scalar)))
    assert _may_be_empty(ContentContract("repeat", (empty,)))
    assert not _may_be_empty(ContentContract("repeat", (scalar,)))
    assert not _may_be_empty(ContentContract("unknown"))
    assert _content_scalar_contracts(ContentContract("empty")) == ()


def test_content_scalar_contracts_ignore_attributes() -> None:
    schema = schema_for(f'<attribute name="q">{INTEGER}</attribute>{INTEGER}')
    route = schema.projection.routes[schema.projection.roots[0]]

    assert _content_scalar_contracts(route.content.model) == (route.content.scalar_contracts[0],)


def test_token_plan_helpers_cover_every_combination() -> None:
    integer = Data(object())
    empty = Empty()
    one = OneOrMore(integer)
    results: dict[int, Any] = {
        id(empty): ("fixed", (), 0),
        id(integer): ("fixed", (integer,), 1),
        id(one): ("list", (integer,), 1),
    }

    assert _combine_tokens(empty, results) == ("fixed", (), 0)
    assert _combine_tokens(integer, results) == ("fixed", (integer,), 1)
    assert _combine_tokens(one, results) == ("list", (integer,), 1)
    assert _combine_tokens(Choice(empty, one), results) == ("list", (integer,), 0)
    assert _combine_tokens(Choice(one, empty), results) == ("list", (integer,), 0)
    assert _combine_tokens(Choice(integer, one), results) is None
    assert _combine_tokens(Group(integer, empty), results) == ("fixed", (integer,), 1)
    assert _combine_tokens(Group(integer, one), {**results, id(one): None}) is None
    assert _combine_tokens(Group(integer, one), results) is None
    pair = Group(integer, integer)
    assert _combine_tokens(OneOrMore(pair), {id(pair): ("fixed", (integer, integer), 2)}) is None
    assert _combine_tokens(OneOrMore(integer), {id(integer): ("fixed", (integer,), 1)}) == (
        "list",
        (integer,),
        1,
    )
    assert _combine_tokens(OneOrMore(integer), {id(integer): None}) is None
    assert _combine_tokens(Choice(integer, one), {id(integer): None, id(one): None}) is None
    assert _combine_tokens(Text(), results) is None
    reference = Ref()
    reference.bind(integer)
    assert _combine_tokens(reference, results) == ("fixed", (integer,), 1)


def test_token_patterns_extracts_fixed_and_homogeneous_lists() -> None:
    fixed_schema = schema_for(
        '<list><group><data type="integer"/><data type="string"/></group></list>'
    )
    homogeneous = schema_for('<list><oneOrMore><data type="integer"/></oneOrMore></list>')
    fixed_route = fixed_schema.projection.routes[fixed_schema.projection.roots[0]]
    repeated_route = homogeneous.projection.routes[homogeneous.projection.roots[0]]
    fixed = fixed_schema.projection.encoding_patterns[
        fixed_route.content.scalar_contracts[0].pattern_identity
    ]
    repeated = homogeneous.projection.encoding_patterns[
        repeated_route.content.scalar_contracts[0].pattern_identity
    ]
    assert isinstance(fixed, List)
    assert isinstance(repeated, List)
    fixed_plan = _token_patterns(fixed)
    repeated_plan = _token_patterns(repeated)
    assert fixed_plan is not None and fixed_plan[0] == "fixed"
    assert repeated_plan is not None and repeated_plan[0] == "list"


def test_builtin_and_list_valued_atomics_have_plain_schemas() -> None:
    token = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="r"><data type="token"/></element>')
    )
    idrefs = schema_for('<data type="IDREFS"/>')

    assert exported(token, "<r>x</r>") == {"r": "x"}
    assert_valid(token, {"r": "x"})
    data = exported(idrefs, "<r>a b</r>")
    assert data == {"r": ["a", "b"]}
    assert_valid(idrefs, data)
    assert errors(idrefs, {"r": "a b"})


def test_shared_content_operands_are_visited_once() -> None:
    shared = ContentContract("scalar")
    group = ContentContract("group", (shared, shared))
    nested = ContentContract("group", (group, shared))

    assert _child_model(group) is not None
    assert not _may_be_empty(group)
    assert _child_model(nested) is not None
    assert not _may_be_empty(nested)


def test_token_patterns_handle_refs_and_shared_operands() -> None:
    integer = Data(object())
    reference = Ref()
    reference.bind(integer)
    shared = List(Group(integer, integer))
    nested = List(Group(Group(integer, integer), integer))

    plan = _token_patterns(shared)
    referenced = _token_patterns(List(reference))
    nested_plan = _token_patterns(nested)

    assert plan == ("fixed", (integer, integer), 2)
    assert referenced == ("fixed", (integer,), 1)
    assert nested_plan == ("fixed", (integer, integer, integer), 3)


def test_cardinality_and_branch_deduplication_helpers() -> None:
    target: dict[str, Any] = {}
    _apply_cardinality(target, Cardinality(0, None))
    assert target == {}
    _apply_cardinality(target, Cardinality(2, 3))
    assert target == {"minItems": 2, "maxItems": 3}
    assert _any_of([{"type": "string"}]) == {"type": "string"}
    assert _any_of([{"type": "string"}, {"type": "string"}]) == {"type": "string"}
    assert _any_of([{"type": "string"}, {"type": "integer"}]) == {
        "anyOf": [{"type": "string"}, {"type": "integer"}]
    }
