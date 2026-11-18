"""Deterministic RELAX NG data import, root selection, and claim binding.

Every fixture exports through a real compiled grammar, then re-imports its own
data: the inverse must rebuild equivalent expanded names, represented values,
and text positions, and must never use instance shape to choose a layout.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from pyxsd.data_conversion.contracts import JsonValue
from pyxsd.exceptions import PyXSDError, PyXSDWarning
from pyxsd.relaxng import RelaxNGDocument, RelaxNGSchema
from pyxsd.relaxng.json_profile import data_to_relaxng_tree
from pyxsd.xsd_data_types import QName

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"

INTEGER = '<data type="integer"/>'
STRING = '<data type="string"/>'
ATTR = '<optional><attribute name="z"><data type="integer"/></attribute></optional>'
A = '<element name="a"><data type="integer"/></element>'
B = '<element name="b"><data type="string"/></element>'
RECORD = f"<interleave>{A}{B}</interleave>"
REPEAT = f"<zeroOrMore>{A}</zeroOrMore>"
MIXED_I = (
    '<mixed><zeroOrMore><element name="i"><data type="string"/></element></zeroOrMore></mixed>'
)
ALT = "<choice><data type='integer'/><data type='string'/></choice>"
NUMERIC_ALT = "<choice><data type='integer'/><data type='decimal'/></choice>"
NAMESPACED = f'<element name="child" ns="urn:a"><data type="string"/></element>{A}'
XML_LANG = (
    '<attribute name="lang" ns="http://www.w3.org/XML/1998/namespace">'
    f"<data type='string'/></attribute>{MIXED_I}"
)

GROUPED_FORMATS = ("pyxsd", "parker", "badgerfish")
ALL_FORMATS = ("pyxsd-json", *GROUPED_FORMATS, "jsonml")


#: Fixtures each format can export strictly and re-import identically.
def schema_for(content: str, *, name: str = 'name="r"') -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" datatypeLibrary="{XSD}" {name}>{content}</element>')
    )
    assert not schema.report.has_errors, schema.report.issues
    return schema


def valid(
    content: str,
    xml: str = "<r/>",
    *,
    name: str = 'name="r"',
    schema: RelaxNGSchema | None = None,
) -> RelaxNGDocument:
    compiled = schema if schema is not None else schema_for(content, name=name)
    document = compiled.parse(io.StringIO(xml), typed=True)
    assert isinstance(document, RelaxNGDocument)
    assert not document.report.has_errors, document.report.issues
    return document


def conversion_error(code: str, pointer: str, action: Any) -> PyXSDError:
    with pytest.raises(PyXSDError) as caught:
        action()
    message = str(caught.value)
    assert message.startswith(f"{code} at {pointer!r}: "), message
    return caught.value


# ---------------------------------------------------------------------------
# Round trips for every format
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("format_name", ALL_FORMATS)
@pytest.mark.parametrize(
    ("content", "xml"),
    [
        (INTEGER, "<r>12</r>"),
        (STRING, "<r>x</r>"),
        (ATTR + INTEGER, '<r z="3">12</r>'),
        (ATTR, "<r/>"),
        (RECORD, "<r><b>B</b><a>1</a></r>"),
        (A + B, "<r><a>1</a><b>B</b></r>"),
        (REPEAT, "<r><a>1</a><a>2</a></r>"),
        (REPEAT, "<r/>"),
    ],
)
def test_round_trip_reexports_identical_data(content: str, xml: str, format_name: str) -> None:
    if format_name == "parker" and content in (ATTR + INTEGER, ATTR):
        pytest.skip("Parker cannot represent attributes")
    document = valid(content, xml)
    data = document.to_data(format=format_name, fidelity="strict")

    imported = document.schema.from_data(data, format=format_name)
    assert not imported.report.has_errors, imported.report.issues
    assert imported.to_data(format=format_name, fidelity="strict") == data
    assert json.loads(imported.to_json(format=format_name)) == data


@pytest.mark.parametrize("format_name", ["pyxsd-json", "jsonml", "pyxsd", "badgerfish"])
@pytest.mark.parametrize(
    ("content", "xml"),
    [
        (NAMESPACED, '<r xmlns:n="urn:a"><n:child>v</n:child><a>1</a></r>'),
        (XML_LANG, '<r xml:lang="en">t<i>x</i>u</r>'),
    ],
)
def test_namespaced_round_trips_where_the_format_retains_them(
    content: str, xml: str, format_name: str
) -> None:
    document = valid(content, xml)
    if format_name in ("pyxsd", "badgerfish") and content is XML_LANG:
        pytest.skip("grouped formats lose mixed text positions strictly")
    data = document.to_data(format=format_name, fidelity="strict")

    imported = document.schema.from_data(data, format=format_name)
    assert not imported.report.has_errors, imported.report.issues
    assert [child._name_ for child in imported.root.children] == [
        child._name_ for child in document.root.children
    ]
    assert imported.root.text_value() == document.root.text_value()
    again = document.schema.from_data(
        imported.to_data(format=format_name, fidelity="strict"), format=format_name
    )
    assert [child._name_ for child in again.root.children] == [
        child._name_ for child in document.root.children
    ]


@pytest.mark.parametrize("format_name", GROUPED_FORMATS)
def test_mixed_content_round_trips_best_effort(format_name: str) -> None:
    document = valid(MIXED_I, "<r>a<i>x</i>b<i>y</i>c</r>")
    data = document.to_data(format=format_name)

    with pytest.warns(PyXSDWarning):
        imported = document.schema.from_data(data, format=format_name, fidelity="best-effort")
    assert not imported.report.has_errors, imported.report.issues
    assert imported.to_data(format=format_name) == data


def test_mixed_content_round_trips_strictly_in_ordered_formats() -> None:
    document = valid(MIXED_I, "<r>a<i>x</i>b<i>y</i>c</r>")
    for format_name in ("pyxsd-json", "jsonml"):
        data = document.to_data(format=format_name, fidelity="strict")
        imported = document.schema.from_data(data, format=format_name)
        assert not imported.report.has_errors
        assert imported.to_data(format=format_name, fidelity="strict") == data


def test_profile_round_trip_keeps_text_positions_and_expanded_names() -> None:
    document = valid(MIXED_I, "<r>a<i>x</i>b<i>y</i>c</r>")
    data = document.to_data(format="pyxsd-json", fidelity="strict")

    imported = document.schema.from_data(data, format="pyxsd-json")
    assert [child._name_ for child in imported.root.children] == ["i", "i"]
    assert imported.root.text_value() == "a"
    assert imported.root.children[0].tail_value() == "b"
    assert imported.root.children[1].tail_value() == "c"
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data


def test_profile_alternatives_round_trip_with_complete_candidates() -> None:
    content = ATTR + ALT
    document = valid(content, '<r z="3">12</r>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    assert "#value" in json.dumps(data)

    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    original = document.root.scalar_value()
    rebuilt = imported.root.scalar_value()
    assert rebuilt.lexical == original.lexical
    assert rebuilt.candidates == original.candidates
    original_attribute = document.root.attribute_value("z")
    rebuilt_attribute = imported.root.attribute_value("z")
    assert rebuilt_attribute.lexical == original_attribute.lexical
    assert tuple(candidate.candidate_id for candidate in rebuilt_attribute.candidates) == tuple(
        candidate.candidate_id for candidate in original_attribute.candidates
    )
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data


def test_native_tagged_alternatives_round_trip() -> None:
    document = valid(ALT, "<r>12</r>")
    data = document.to_data(format="pyxsd", fidelity="strict")
    assert data["$"] == {"#value": data["$"]["#value"]}

    imported = document.schema.from_data(data, format="pyxsd")
    assert imported.root.scalar_value().candidates == document.root.scalar_value().candidates
    assert imported.to_data(format="pyxsd", fidelity="strict") == data


def test_list_scalars_round_trip_through_every_format() -> None:
    document = valid("<data type='NMTOKENS'/>", "<r>1 2 3</r>")
    for format_name in ALL_FORMATS:
        data = document.to_data(format=format_name)
        imported = document.schema.from_data(data, format=format_name)
        assert not imported.report.has_errors, (format_name, imported.report.issues)
        assert imported.root.text_value() == "1 2 3", format_name


def test_repeated_list_valued_children_keep_their_occurrences() -> None:
    document = valid(
        "<zeroOrMore><element name='n'><data type='NMTOKENS'/></element></zeroOrMore>",
        "<r><n>1 2</n><n>3 4</n></r>",
    )
    for format_name in ALL_FORMATS:
        data = document.to_data(format=format_name)
        imported = document.schema.from_data(data, format=format_name)
        assert not imported.report.has_errors, (format_name, imported.report.issues)
        assert [child.text_value() for child in imported.root.children] == [
            "1 2",
            "3 4",
        ], format_name


def test_explicit_empty_string_content_survives_every_format() -> None:
    schema = schema_for(STRING)
    for format_name, data in (
        ("pyxsd-json", {"r": ""}),
        ("pyxsd", {"$": ""}),
        ("parker", ""),
        ("badgerfish", {"r": ""}),
        ("jsonml", ["r", ""]),
    ):
        document = schema.from_data(data, format=format_name)
        assert not document.report.has_errors, (format_name, document.report.issues)
        assert document.root.text_value() == "", format_name


def test_import_is_deterministic_across_repeated_builds() -> None:
    schema = schema_for(RECORD)
    data: JsonValue = {"r": {"a": 1, "b": "B"}}

    first = schema.from_data(data, format="pyxsd-json")
    second = schema.from_data({"r": {"b": "B", "a": 1}}, format="pyxsd-json")
    assert first.to_data(format="pyxsd-json") == second.to_data(format="pyxsd-json")
    assert [child._name_ for child in first.root.children] == [
        child._name_ for child in second.root.children
    ]


# ---------------------------------------------------------------------------
# Root selection
# ---------------------------------------------------------------------------


def test_profile_uses_the_authored_root_key() -> None:
    schema = schema_for(INTEGER)
    document = schema.from_data({"r": 12}, format="pyxsd-json")
    assert document.root._name_ == "r"
    assert not document.report.has_errors


@pytest.mark.parametrize("selector", ["r", "{urn:a}r", "Q{urn:a}r"])
def test_profile_explicit_selectors_narrow_the_start_class(selector: str) -> None:
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<choice xmlns="{NS}"><element name="r" ns="urn:a"><empty/></element>'
            '<element name="other"><empty/></element></choice>'
        )
    )
    document = schema.from_data({"Q{urn:a}r": ""}, format="pyxsd-json", element=selector)
    assert document.root._name_ == "{urn:a}r"
    assert not document.report.has_errors


def test_profile_selector_mismatch_and_unknown_roots() -> None:
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<choice xmlns="{NS}"><element name="r"><empty/></element>'
            '<element name="s"><empty/></element></choice>'
        )
    )
    conversion_error(
        "root-mismatch", "", lambda: schema.from_data({"r": ""}, format="pyxsd-json", element="s")
    )
    conversion_error(
        "root-unresolved", "", lambda: schema.from_data({"r": ""}, format="pyxsd-json", element="t")
    )
    ambiguous = RelaxNGSchema.compile(
        io.StringIO(
            f'<choice xmlns="{NS}"><element name="r" ns="urn:a"><empty/></element>'
            '<element name="r"><empty/></element></choice>'
        )
    )
    conversion_error(
        "root-ambiguous",
        "",
        lambda: ambiguous.from_data({"r": ""}, format="pyxsd-json", element="r"),
    )


def test_profile_root_object_shape() -> None:
    schema = schema_for(INTEGER)
    conversion_error("container-invalid", "", lambda: schema.from_data(12, format="pyxsd-json"))
    conversion_error(
        "root-ambiguous", "", lambda: schema.from_data({"r": 1, "s": 2}, format="pyxsd-json")
    )
    conversion_error(
        "name-invalid", "", lambda: schema.from_data({"#value": 1}, format="pyxsd-json")
    )


def test_root_absorbed_formats_need_a_unique_admitted_name() -> None:
    unique = schema_for(INTEGER)
    for format_name in ("pyxsd", "parker"):
        data = {"$": 12} if format_name == "pyxsd" else 12
        document = unique.from_data(data, format=format_name)
        assert document.root._name_ == "r"
        assert not document.report.has_errors

    ambiguous = RelaxNGSchema.compile(
        io.StringIO(
            f'<choice xmlns="{NS}"><element name="r"><empty/></element>'
            '<element name="s"><empty/></element></choice>'
        )
    )
    for format_name in ("pyxsd", "parker"):
        conversion_error(
            "root-ambiguous",
            "",
            lambda format_name=format_name: ambiguous.from_data({}, format=format_name),
        )
        document = ambiguous.from_data({}, format=format_name, element="s")
        assert document.root._name_ == "s"
        assert not document.report.has_errors


def test_open_start_class_requires_an_explicit_root_absorbed_selector() -> None:
    open_schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}"><anyName/><empty/></element>')
    )
    closed_schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="r" ns="urn:a"><empty/></element>')
    )
    conversion_error("root-ambiguous", "", lambda: open_schema.from_data({}, format="parker"))
    document = open_schema.from_data({}, format="parker", element="{urn:any}thing")
    assert document.root._name_ == "{urn:any}thing"
    assert not document.report.has_errors
    document = closed_schema.from_data({}, format="parker", element="Q{urn:a}r")
    assert document.root._name_ == "{urn:a}r"
    conversion_error(
        "root-unresolved",
        "",
        lambda: closed_schema.from_data({}, format="parker", element="Q{urn:z}r"),
    )


def test_root_retained_conventional_formats_confirm_the_carried_root() -> None:
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<choice xmlns="{NS}" datatypeLibrary="{XSD}">'
            '<element name="r"><data type="integer"/></element>'
            '<element name="s"><empty/></element></choice>'
        )
    )
    document = schema.from_data({"r": 12}, format="badgerfish")
    assert document.root._name_ == "r"
    document = schema.from_data(["r", 12], format="jsonml")
    assert document.root._name_ == "r"
    confirmed = schema.from_data(["r", 12], format="jsonml", element="r")
    assert confirmed.root._name_ == "r"
    conversion_error(
        "root-mismatch",
        "",
        lambda: schema.from_data({"r": 12}, format="badgerfish", element="s"),
    )
    conversion_error(
        "root-mismatch",
        "",
        lambda: schema.from_data(["r", 12], format="jsonml", element="s"),
    )


# ---------------------------------------------------------------------------
# Strict profile shape validation
# ---------------------------------------------------------------------------


def test_profile_rejects_wrong_containers() -> None:
    record = schema_for(RECORD)
    conversion_error(
        "container-invalid", "/r", lambda: record.from_data({"r": []}, format="pyxsd-json")
    )
    sequence = schema_for(A + B)
    conversion_error(
        "container-invalid", "/r", lambda: sequence.from_data({"r": {"a": 1}}, format="pyxsd-json")
    )
    scalar = schema_for(INTEGER)
    conversion_error(
        "container-invalid", "/r", lambda: scalar.from_data({"r": {"a": 1}}, format="pyxsd-json")
    )


def test_profile_requires_complete_wrapped_members() -> None:
    with_attrs = schema_for(ATTR + INTEGER)
    conversion_error(
        "container-invalid",
        "/r",
        lambda: with_attrs.from_data({"r": {"@z": 3}}, format="pyxsd-json"),
    )
    empty_with_attrs = schema_for(ATTR + "<empty/>")
    conversion_error(
        "container-invalid",
        "/r",
        lambda: empty_with_attrs.from_data({"r": ""}, format="pyxsd-json"),
    )
    conversion_error(
        "container-invalid",
        "/r/#content",
        lambda: empty_with_attrs.from_data({"r": {"#content": ""}}, format="pyxsd-json"),
    )
    plain_empty = schema_for("<empty/>")
    conversion_error(
        "container-invalid", "/r", lambda: plain_empty.from_data({"r": {}}, format="pyxsd-json")
    )


def test_profile_repeated_layout_requires_its_array_member() -> None:
    with_attrs = schema_for(ATTR + REPEAT)
    conversion_error(
        "container-invalid",
        "/r/a",
        lambda: with_attrs.from_data({"r": {"@z": 3}}, format="pyxsd-json"),
    )
    plain = schema_for(REPEAT)
    conversion_error(
        "container-invalid", "/r", lambda: plain.from_data({"r": {"a": []}}, format="pyxsd-json")
    )
    conversion_error(
        "container-invalid", "/r", lambda: plain.from_data({"r": 1}, format="pyxsd-json")
    )
    document = with_attrs.from_data({"r": {"@z": 3, "a": []}}, format="pyxsd-json")
    assert not document.report.has_errors


def test_profile_ordered_rules_attribute_first_and_singleton_children() -> None:
    schema = schema_for(ATTR + A + B)
    conversion_error(
        "container-invalid",
        "/r/1",
        lambda: schema.from_data({"r": [{"a": 1}, {"@z": 3}]}, format="pyxsd-json"),
    )
    conversion_error(
        "container-invalid",
        "/r/0",
        lambda: schema.from_data({"r": [{"a": 1, "b": "x"}]}, format="pyxsd-json"),
    )
    conversion_error(
        "container-invalid", "/r/0", lambda: schema.from_data({"r": [12]}, format="pyxsd-json")
    )
    document = schema.from_data({"r": [{"@z": 3}, {"a": 1}, {"b": "x"}]}, format="pyxsd-json")
    assert not document.report.has_errors
    assert document.root.attribute_lexical("z") == "3"


def test_profile_sequence_skips_insignificant_whitespace_strings() -> None:
    schema = schema_for(A + B)
    document = schema.from_data({"r": [" ", {"a": 1}, {"b": "x"}]}, format="pyxsd-json")
    assert not document.report.has_errors
    assert document.root.children[0].tail_value() is None


def test_profile_rejects_duplicate_expanded_child_keys() -> None:
    schema = schema_for(RECORD)
    conversion_error(
        "json-duplicate-key",
        "/r/Q{}a",
        lambda: schema.from_data({"r": {"a": 1, "Q{}a": 2}}, format="pyxsd-json"),
    )


def test_profile_child_and_attribute_names_must_be_names() -> None:
    schema = schema_for(RECORD)
    conversion_error(
        "name-invalid",
        "/r/#value",
        lambda: schema.from_data({"r": {"#value": 1}}, format="pyxsd-json"),
    )
    conversion_error(
        "name-invalid",
        "/r/@xmlns",
        lambda: schema.from_data({"r": {"@xmlns": "urn:x"}}, format="pyxsd-json"),
    )


def test_native_and_grouped_reject_declaration_attribute_keys() -> None:
    schema = schema_for(ATTR + INTEGER)
    conversion_error(
        "name-invalid",
        "/@xmlns",
        lambda: schema.from_data({"@xmlns": {"ns0": "urn:a"}, "$": 12}, format="pyxsd"),
    )


# ---------------------------------------------------------------------------
# JSON parsing boundaries
# ---------------------------------------------------------------------------


def test_from_json_rejects_malformed_sources() -> None:
    schema = schema_for(INTEGER)
    conversion_error("json-invalid", "", lambda: schema.from_json('{"r": ', format="pyxsd-json"))
    conversion_error("json-invalid", "", lambda: schema.from_json(b"\xff\xfe", format="pyxsd-json"))
    conversion_error(
        "json-invalid", "", lambda: schema.from_json('{"r": NaN}', format="pyxsd-json")
    )
    conversion_error(
        "json-duplicate-key", "", lambda: schema.from_json('{"r": 1, "r": 2}', format="pyxsd-json")
    )
    with pytest.raises(TypeError):
        schema.from_json(io.StringIO("{}"), format="pyxsd-json")  # type: ignore[arg-type]


def test_from_data_rejects_nonfinite_numbers_with_pointers() -> None:
    schema = schema_for(ATTR + INTEGER)
    conversion_error(
        "json-invalid",
        "/r/@z",
        lambda: schema.from_data({"r": {"@z": float("inf")}}, format="pyxsd-json"),
    )


def test_from_json_validates_format_before_parsing() -> None:
    schema = schema_for(INTEGER)
    with pytest.raises(ValueError, match="unknown data format"):
        schema.from_json("{}", format="other")
    with pytest.raises(ValueError, match="unknown fidelity"):
        schema.from_json("{}", format="pyxsd-json", fidelity="loose")


def test_conversion_errors_are_wrapped_with_the_format() -> None:
    schema = schema_for(INTEGER)
    with pytest.raises(PyXSDError, match=r"\(format='pyxsd-json'\)"):
        schema.from_data({"r": None}, format="pyxsd-json")
    with pytest.raises(PyXSDError, match=r"json-invalid.*\(format='parker'\)"):
        schema.from_json("{", format="parker")


# ---------------------------------------------------------------------------
# Null handling per format
# ---------------------------------------------------------------------------


def test_bare_null_is_rejected_where_undefined() -> None:
    profile = schema_for(INTEGER)
    conversion_error(
        "scalar-type-invalid", "/r", lambda: profile.from_data({"r": None}, format="pyxsd-json")
    )
    parker = schema_for(INTEGER)
    conversion_error("scalar-type-invalid", "", lambda: parker.from_data(None, format="parker"))
    badgerfish = schema_for(INTEGER)
    conversion_error(
        "scalar-type-invalid", "/r", lambda: badgerfish.from_data({"r": None}, format="badgerfish")
    )
    jsonml = schema_for(INTEGER)
    conversion_error(
        "container-invalid", "/1", lambda: jsonml.from_data(["r", None], format="jsonml")
    )


def test_native_null_keeps_the_non_nil_empty_content_convention() -> None:
    schema = schema_for("<optional><element name='a'><data type='string'/></element></optional>")
    document = schema.from_data({"a": None}, format="pyxsd")
    assert document.root.children[0].text_value() is None
    assert not document.report.has_errors

    leaf = schema_for(ATTR + "<empty/>")
    empty = leaf.from_data({"$": None}, format="pyxsd")
    assert empty.root.text_value() is None
    assert not empty.report.has_errors


# ---------------------------------------------------------------------------
# Namespaces, prefixes, QNames, and base URIs
# ---------------------------------------------------------------------------


def test_seeded_namespaces_scope_lexical_qname_text() -> None:
    schema = schema_for("<data type='QName'/>")
    document = schema.from_data("p:v", format="parker", namespaces={"p": "urn:example"})
    assert not document.report.has_errors, document.report.issues
    value = document.root.scalar_value()
    assert isinstance(value, QName)
    assert value.to_expanded_name() == "{urn:example}v"

    unseeded = schema.from_data("p:v", format="parker")
    assert unseeded.report.has_errors


def test_profile_eqname_qname_values_generate_deterministic_bindings() -> None:
    document = valid("<data type='QName'/>", '<r xmlns:p="urn:example">p:v</r>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    assert data == {"r": "Q{urn:example}v"}

    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors, imported.report.issues
    assert imported.root.scalar_value().to_expanded_name() == "{urn:example}v"
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data


def test_profile_string_values_shaped_like_eqnames_stay_verbatim() -> None:
    schema = schema_for(STRING)
    document = schema.from_data({"r": "Q{urn:x}v"}, format="pyxsd-json")
    assert document.root.text_value() == "Q{urn:x}v"


def test_badgerfish_declarations_inherit_and_shadow() -> None:
    content = (
        "<choice>"
        '<element name="child" ns="urn:a"><data type="string"/></element>'
        '<element name="child" ns="urn:b"><data type="string"/></element>'
        "</choice>"
    )
    schema = schema_for(content)
    document = schema.from_data(
        {
            "r": {
                "@xmlns": {"ns0": "urn:a"},
                "ns0:child": {
                    "@xmlns": {"ns0": "urn:b"},
                    "$": "b",
                },
            }
        },
        format="badgerfish",
        namespaces={"seed": "urn:seed"},
    )
    assert document.root.children[0]._name_ == "{urn:b}child"
    assert not document.report.has_errors, document.report.issues


def test_badgerfish_exported_declarations_round_trip() -> None:
    document = valid(NAMESPACED, '<r xmlns:n="urn:a"><n:child>v</n:child><a>1</a></r>')
    data = document.to_data(format="badgerfish", fidelity="strict")
    imported = document.schema.from_data(data, format="badgerfish")
    assert [child._name_ for child in imported.root.children] == ["{urn:a}child", "a"]
    assert not imported.report.has_errors, imported.report.issues
    again = document.schema.from_data(
        imported.to_data(format="badgerfish", fidelity="strict"), format="badgerfish"
    )
    assert [child._name_ for child in again.root.children] == ["{urn:a}child", "a"]


def test_jsonml_declarations_scope_children() -> None:
    content = '<element name="child" ns="urn:a"><data type="string"/></element>'
    schema = schema_for(content)
    document = schema.from_data(["r", {"xmlns:p": "urn:a"}, ["p:child", "v"]], format="jsonml")
    assert document.root.children[0]._name_ == "{urn:a}child"
    assert not document.report.has_errors


def test_tag_context_round_trips_base_uri_metadata(tmp_path) -> None:
    source = tmp_path / "instance.xml"
    source.write_text("<r>12</r>", encoding="utf-8")
    schema = schema_for(ALT)
    document = schema.parse(source, typed=True)
    assert document.root.base_uri == str(source)
    data = json.loads(document.to_json(format="pyxsd-json", fidelity="strict"))

    imported = schema.from_json(
        json.dumps(data), format="pyxsd-json", namespaces={"q": "urn:other"}
    )
    assert not imported.report.has_errors
    assert imported.root.base_uri == str(source)
    claim = data["r"]["#value"]
    assert claim["context"]["base_uri"] == str(source)


def test_tag_context_shadow_seeded_bindings_per_node() -> None:
    content = (
        "<optional><attribute name='z'><data type='QName'/></attribute></optional>"
        "<choice><data type='QName'/><data type='string'/></choice>"
    )
    document = valid(content, '<r z="p:a" xmlns:p="urn:e" xmlns:q="urn:z">q:b</r>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")

    seeded = document.schema.from_data(
        json.loads(json.dumps(data)), format="pyxsd-json", namespaces={"p": "urn:wrong"}
    )
    assert not seeded.report.has_errors, seeded.report.issues
    rebuilt = seeded.root.scalar_value()
    assert rebuilt.lexical == "q:b"
    assert any(
        isinstance(candidate.decoded, QName) and candidate.decoded.to_expanded_name() == "{urn:z}b"
        for candidate in rebuilt.candidates
    )
    rebuilt_attribute = seeded.root.attribute_value("z")
    assert rebuilt_attribute.lexical == "p:a"
    assert any(
        isinstance(candidate.decoded, QName) and candidate.decoded.to_expanded_name() == "{urn:e}a"
        for candidate in rebuilt_attribute.candidates
    )


def test_conflicting_tag_contexts_on_one_node_are_rejected() -> None:
    content = (
        "<optional><attribute name='z'><data type='QName'/></attribute></optional>"
        "<choice><data type='QName'/><data type='string'/></choice>"
    )
    document = valid(content, '<r z="p:a" xmlns:p="urn:e" xmlns:q="urn:z">q:b</r>')
    data = json.loads(document.to_json(format="pyxsd-json", fidelity="strict"))
    data["r"]["#content"]["#value"]["context"]["namespaces"] = {"q": "urn:wrong"}

    conversion_error(
        "namespace-conflict", "/r", lambda: document.schema.from_data(data, format="pyxsd-json")
    )


# ---------------------------------------------------------------------------
# Native and Parker grouped import
# ---------------------------------------------------------------------------


def test_native_grouped_clark_keys_and_scalar_leaves() -> None:
    content = (
        '<attribute name="lang" ns="http://www.w3.org/XML/1998/namespace">'
        "<data type='string'/></attribute>" + RECORD
    )
    document = valid(content, '<r xml:lang="en"><b>B</b><a>1</a></r>')
    data = document.to_data(format="pyxsd", fidelity="strict")
    assert data == {
        "@{http://www.w3.org/XML/1998/namespace}lang": "en",
        "a": 1,
        "b": "B",
    }
    imported = document.schema.from_data(data, format="pyxsd")
    assert not imported.report.has_errors, imported.report.issues
    assert imported.root.attribute_lexical("{http://www.w3.org/XML/1998/namespace}lang") == "en"


def test_native_repeated_and_always_list_shapes() -> None:
    schema = schema_for(REPEAT)
    document = schema.from_data({"a": [1, 2]}, format="pyxsd")
    assert [child.text_value() for child in document.root.children] == ["1", "2"]
    single = schema.from_data({"a": [1]}, format="pyxsd")
    assert [child.text_value() for child in single.root.children] == ["1"]


def test_native_rejects_marker_style_attribute_keys() -> None:
    schema = schema_for(ATTR + INTEGER)
    conversion_error(
        "name-invalid",
        "/@xsi:nil",
        lambda: schema.from_data({"@xsi:nil": "true", "$": 12}, format="pyxsd"),
    )


def test_native_unreconstructable_order_is_a_strict_loss() -> None:
    content = f"<zeroOrMore><choice>{A}{B}</choice></zeroOrMore>"
    document = valid(content, "<r><a>1</a><b>x</b><a>2</a></r>")
    data = document.to_data(format="pyxsd")

    conversion_error(
        "order-ambiguous",
        "",
        lambda: document.schema.from_data(data, format="pyxsd", fidelity="strict"),
    )
    with pytest.warns(PyXSDWarning):
        imported = document.schema.from_data(data, format="pyxsd", fidelity="best-effort")
    assert [child._name_ for child in imported.root.children] == ["a", "a", "b"]


def test_parker_local_names_and_empty_objects() -> None:
    document = valid(A + B, "<r><a>1</a><b>B</b></r>")
    imported = document.schema.from_data({"a": 1, "b": "B"}, format="parker")
    assert not imported.report.has_errors
    empty = schema_for("<empty/>")
    document = empty.from_data({}, format="parker")
    assert document.root.children == ()
    assert not document.report.has_errors


def test_parker_rejects_attribute_and_marker_keys() -> None:
    schema = schema_for(ATTR + INTEGER)
    conversion_error(
        "name-invalid", "/@z", lambda: schema.from_data({"@z": 3, "$": 12}, format="parker")
    )


def test_parker_ambiguous_local_child_names_need_a_fallback() -> None:
    content = (
        "<choice>"
        '<element name="x" ns="urn:a"><data type="string"/></element>'
        '<element name="x" ns="urn:b"><data type="string"/></element>'
        "</choice>"
    )
    document = valid(content, '<r><x xmlns="urn:a">v</x></r>')
    conversion_error(
        "name-ambiguous",
        "/x",
        lambda: document.schema.from_data({"x": "v"}, format="parker", fidelity="strict"),
    )
    with pytest.warns(PyXSDWarning):
        imported = document.schema.from_data({"x": "v"}, format="parker", fidelity="best-effort")
    assert imported.root.children[0]._name_ == "x"
    assert imported.report.has_errors


def test_parker_wildcard_children_reconstruct_from_local_names() -> None:
    content = "<zeroOrMore><element><anyName/><data type='string'/></element></zeroOrMore>"
    schema = schema_for(content)
    document = schema.from_data({"item": "v"}, format="parker")
    assert document.root.children[0]._name_ == "item"
    assert not document.report.has_errors


def test_grouped_mixed_text_with_children_is_a_disclosed_loss() -> None:
    document = valid(MIXED_I, "<r>a<i>x</i>b</r>")
    with pytest.warns(PyXSDWarning):
        data = document.to_data(format="parker")
    conversion_error(
        "mixed-content",
        "",
        lambda: document.schema.from_data(data, format="parker", fidelity="strict"),
    )
    with pytest.warns(PyXSDWarning):
        imported = document.schema.from_data(data, format="parker", fidelity="best-effort")
    assert imported.root.text_value() == "ab"
    assert not imported.report.has_errors


# ---------------------------------------------------------------------------
# BadgerFish and JsonML import
# ---------------------------------------------------------------------------


def test_badgerfish_unresolved_prefix_is_a_fallback() -> None:
    schema = schema_for('<element name="child" ns="urn:a">' + STRING + "</element>")
    conversion_error(
        "namespace-unresolved",
        "/r/p:child",
        lambda: schema.from_data({"r": {"p:child": "v"}}, format="badgerfish", fidelity="strict"),
    )
    with pytest.warns(PyXSDWarning):
        imported = schema.from_data(
            {"r": {"p:child": "v"}}, format="badgerfish", fidelity="best-effort"
        )
    assert imported.root.children[0]._name_ == "p:child"
    assert imported.report.has_errors


def test_badgerfish_root_declarations_apply_to_the_root_name() -> None:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="r" ns="urn:a"><empty/></element>')
    )
    document = schema.from_data(
        {"ns0:r": {"@xmlns": {"ns0": "urn:a"}}}, format="badgerfish", element="Q{urn:a}r"
    )
    assert document.root._name_ == "{urn:a}r"
    assert not document.report.has_errors


def test_badgerfish_attributes_and_text() -> None:
    document = valid(ATTR + INTEGER, '<r z="3">12</r>')
    data = document.to_data(format="badgerfish", fidelity="strict")
    assert data == {"r": {"@z": 3, "$": 12}}
    imported = document.schema.from_data(data, format="badgerfish")
    assert imported.root.attribute_lexical("z") == "3"
    assert imported.root.text_value() == "12"


def test_jsonml_rejects_wrong_shapes() -> None:
    schema = schema_for(INTEGER)
    conversion_error("container-invalid", "", lambda: schema.from_data("r", format="jsonml"))
    conversion_error("root-ambiguous", "", lambda: schema.from_data([], format="jsonml"))
    conversion_error("container-invalid", "", lambda: schema.from_data([12], format="jsonml"))
    conversion_error(
        "container-invalid", "/2", lambda: schema.from_data(["r", 12, {"a": 1}], format="jsonml")
    )


def test_jsonml_attribute_values_must_be_scalars() -> None:
    schema = schema_for(ATTR + INTEGER)
    conversion_error(
        "scalar-type-invalid",
        "/1/@z",
        lambda: schema.from_data(["r", {"@z": [1]}, 12], format="jsonml"),
    )
    conversion_error(
        "scalar-type-invalid",
        "/1/@z",
        lambda: schema.from_data(["r", {"@z": None}, 12], format="jsonml"),
    )


# ---------------------------------------------------------------------------
# Grammar-invalid content returns an invalid document
# ---------------------------------------------------------------------------


def test_lexically_invalid_data_returns_an_invalid_document() -> None:
    schema = schema_for(INTEGER)
    document = schema.from_data({"r": "bad"}, format="pyxsd-json")
    assert document.report.has_errors
    assert document.root.text_value() == "bad"

    parker = schema.from_data("bad", format="parker")
    assert parker.report.has_errors
    jsonml = schema.from_data(["r", "bad"], format="jsonml")
    assert jsonml.report.has_errors


def test_unknown_children_are_reported_not_rejected() -> None:
    schema = schema_for("<empty/>")
    conversion_error(
        "container-invalid",
        "/r",
        lambda: schema.from_data({"r": {"unknown": ""}}, format="pyxsd-json"),
    )


def test_null_list_items_are_conversion_errors() -> None:
    schema = schema_for(INTEGER)
    conversion_error(
        "scalar-type-invalid",
        "/r/1",
        lambda: schema.from_data({"r": [1, None]}, format="pyxsd-json"),
    )


# ---------------------------------------------------------------------------
# Tag claims, forgery, and branch neutrality
# ---------------------------------------------------------------------------


def alt_data(document: RelaxNGDocument) -> dict[str, Any]:
    return json.loads(document.to_json(format="pyxsd-json", fidelity="strict"))


def test_malformed_tags_are_conversion_errors() -> None:
    schema = schema_for(ALT)
    good = alt_data(valid(ALT, "<r>12</r>"))
    broken = json.loads(json.dumps(good))
    broken["r"]["#value"]["version"] = 2
    conversion_error(
        "scalar-tag-version", "/r", lambda: schema.from_data(broken, format="pyxsd-json")
    )
    extra = json.loads(json.dumps(good))
    extra["r"]["#value"]["extra"] = 1
    conversion_error(
        "scalar-tag-invalid", "/r", lambda: schema.from_data(extra, format="pyxsd-json")
    )
    conversion_error(
        "scalar-tag-invalid",
        "/r",
        lambda: schema.from_data({"r": {"#value": 1}}, format="pyxsd-json"),
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("grammar", "urn:wrong", "scalar-tag-grammar-mismatch"),
        ("candidates", ["stale-id"], "scalar-tag-candidates-mismatch"),
    ],
)
def test_forged_tag_assertions_fail_on_valid_documents(field: str, value: Any, code: str) -> None:
    schema = schema_for(ALT)
    document = valid(ALT, "<r>12</r>")
    data = alt_data(document)
    data["r"]["#value"][field] = value
    conversion_error(code, "/r", lambda: schema.from_data(data, format="pyxsd-json"))


def test_partial_candidate_sets_never_filter_branches() -> None:
    document = valid(ALT, "<r>12</r>")
    data = alt_data(document)
    data["r"]["#value"]["candidates"] = data["r"]["#value"]["candidates"][:1]
    conversion_error(
        "scalar-tag-candidates-mismatch",
        "/r",
        lambda: document.schema.from_data(data, format="pyxsd-json"),
    )


def test_invalid_documents_skip_claim_verification() -> None:
    schema = schema_for(NUMERIC_ALT)
    document = valid(NUMERIC_ALT, "<r>12</r>")
    data = alt_data(document)
    data["r"]["#value"]["candidates"] = ["forged"]
    data["r"]["#value"]["lexical"] = "not-a-number"

    imported = schema.from_data(data, format="pyxsd-json")
    assert imported.report.has_errors
    assert imported.binding_evidence is None


def test_bound_claim_locations_follow_the_built_tree() -> None:
    document = valid(ALT, "<r>12</r>")
    data = alt_data(document)
    tree = data_to_relaxng_tree(document.schema, data)

    assert tree.root.tag == "r"
    assert len(tree.claims) == 1
    claim = tree.claims[0]
    assert claim.location is not None
    assert claim.location.element_path == ()
    assert claim.location.attribute is None
    assert claim.location.content_index == 0
    assert id(tree.root) in tree.contexts
    assert tree.claims[0].pointer == "/r"

    nested = valid(
        "<zeroOrMore><element name='a'>" + ALT + "</element></zeroOrMore>", "<r><a>12</a></r>"
    )
    nested_data = json.loads(nested.to_json(format="pyxsd-json", fidelity="strict"))
    tree = data_to_relaxng_tree(nested.schema, nested_data)
    assert tree.claims[0].location is not None
    assert tree.claims[0].location.element_path == (0,)


def test_attribute_claims_bind_attribute_locations() -> None:
    content = (
        "<optional><attribute name='z'><choice><data type='integer'/>"
        "<data type='string'/></choice></attribute></optional>" + INTEGER
    )
    document = valid(content, '<r z="3">12</r>')
    data = alt_data(document)
    assert "#value" in json.dumps(data["r"]["@z"])

    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data


# ---------------------------------------------------------------------------
# Dispatcher contract
# ---------------------------------------------------------------------------


def test_data_to_relaxng_tree_validates_options_first() -> None:
    schema = schema_for(INTEGER)
    with pytest.raises(ValueError, match="unknown data format"):
        data_to_relaxng_tree(schema, {"r": 1}, format="other")
    with pytest.raises(ValueError, match="unknown fidelity"):
        data_to_relaxng_tree(schema, {"r": 1}, fidelity="loose")


# ---------------------------------------------------------------------------
# Import edge cases and defensive shape boundaries
# ---------------------------------------------------------------------------


def test_profile_qname_prefixes_are_reused_and_avoid_seeded_collisions() -> None:
    content = "<zeroOrMore><element name='q'><data type='QName'/></element></zeroOrMore>"
    document = valid(content, '<r xmlns:p="urn:e"><q>p:a</q><q>p:b</q></r>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data

    single = schema_for("<data type='QName'/>")
    collision = single.from_data(
        {"r": "Q{urn:example}v"},
        format="pyxsd-json",
        namespaces={"ns0": "urn:x", "ns1": "urn:y"},
    )
    assert not collision.report.has_errors
    assert collision.root.scalar_value().to_expanded_name() == "{urn:example}v"
    assert collision.root.namespaces is not None
    assert collision.root.namespaces["ns2"] == "urn:example"


def test_profile_qname_edge_spellings_stay_lexical_or_use_seeded_prefixes() -> None:
    qname = schema_for("<data type='QName'/>")
    malformed = qname.from_data({"r": "Q{urn"}, format="pyxsd-json")
    assert malformed.report.has_errors
    assert malformed.root.text_value() == "Q{urn"

    empty_uri = qname.from_data({"r": "Q{}v"}, format="pyxsd-json")
    assert not empty_uri.report.has_errors
    assert empty_uri.root.text_value() == "v"

    seeded = qname.from_data({"r": "Q{urn:x}v"}, format="pyxsd-json", namespaces={"p": "urn:x"})
    assert not seeded.report.has_errors
    assert seeded.root.text_value() == "p:v"
    assert seeded.root.scalar_value().to_expanded_name() == "{urn:x}v"


def test_profile_qname_with_a_frozen_tag_context_requires_a_prefix() -> None:
    content = (
        "<optional><attribute name='z'><choice><data type='integer'/>"
        "<data type='string'/></choice></attribute></optional>"
        "<data type='QName'/>"
    )
    document = valid(content, '<r z="1" xmlns:p="urn:e">p:v</r>')
    data = alt_data(document)
    data["r"]["#content"] = "Q{urn:unbound}v"
    conversion_error(
        "namespace-conflict",
        "/r/#content",
        lambda: document.schema.from_data(data, format="pyxsd-json"),
    )


def test_profile_wrapped_scalar_rejects_bad_members() -> None:
    schema = schema_for(ATTR + STRING)
    conversion_error(
        "container-invalid", "/r", lambda: schema.from_data({"r": 1}, format="pyxsd-json")
    )
    conversion_error(
        "container-invalid",
        "/r/extra",
        lambda: schema.from_data(
            {"r": {"@z": "x", "#content": "v", "extra": 1}}, format="pyxsd-json"
        ),
    )


def test_profile_record_attributes_and_unknown_root() -> None:
    schema = schema_for(ATTR + RECORD)
    document = schema.from_data({"r": {"@z": 3, "b": "B", "a": 1}}, format="pyxsd-json")
    assert not document.report.has_errors
    assert document.root.attribute_lexical("z") == "3"
    conversion_error(
        "root-unresolved",
        "",
        lambda: schema.from_data({"other": {}}, format="pyxsd-json"),
    )


def test_profile_repeated_rejects_wrong_members_and_nonarrays() -> None:
    schema = schema_for(ATTR + REPEAT)
    conversion_error(
        "container-invalid", "/r", lambda: schema.from_data({"r": 1}, format="pyxsd-json")
    )
    conversion_error(
        "container-invalid",
        "/r/b",
        lambda: schema.from_data({"r": {"@z": 3, "a": [], "b": 1}}, format="pyxsd-json"),
    )
    conversion_error(
        "container-invalid",
        "/r/a",
        lambda: schema.from_data({"r": {"@z": 3, "a": 1}}, format="pyxsd-json"),
    )


def test_profile_empty_attributes_and_duplicate_attribute_maps() -> None:
    empty = schema_for(ATTR + "<empty/>")
    document = empty.from_data({"r": {"@z": 3}}, format="pyxsd-json")
    assert not document.report.has_errors
    assert document.root.attribute_lexical("z") == "3"

    sequence = schema_for(ATTR + A + B)
    conversion_error(
        "marker-conflict",
        "/r/1/@z",
        lambda: sequence.from_data(
            {"r": [{"@z": 3}, {"@z": 4}, {"a": 1}, {"b": "x"}]}, format="pyxsd-json"
        ),
    )
    conversion_error(
        "json-duplicate-key",
        "/r/0/@Q{}z",
        lambda: sequence.from_data(
            {"r": [{"@z": 3, "@Q{}z": 4}, {"a": 1}, {"b": "x"}]}, format="pyxsd-json"
        ),
    )


def test_profile_wildcard_children_use_generic_routes() -> None:
    content = "<zeroOrMore><element><anyName/><data type='string'/></element></zeroOrMore>"
    schema = schema_for(content)
    original = valid(content, "<r><item>v</item></r>")
    data = original.to_data(format="pyxsd-json", fidelity="strict")
    document = schema.from_data(data, format="pyxsd-json")
    assert not document.report.has_errors, document.report.issues
    assert document.root.children[0]._name_ == "item"
    assert document.root.children[0].text_value() == "v"

    mixed = schema_for(
        "<zeroOrMore><attribute><anyName/><text/></attribute></zeroOrMore>"
        "<zeroOrMore><element><anyName/><text/></element></zeroOrMore>"
    )
    mixed_document = mixed.parse(io.StringIO('<r a="x"><item>v</item></r>'), typed=True)
    assert not mixed_document.report.has_errors
    mixed_data = mixed_document.to_data(format="pyxsd-json", fidelity="strict")
    unknown = mixed.from_data(mixed_data, format="pyxsd-json")
    assert not unknown.report.has_errors
    assert unknown.root.attribute_lexical("a") == "x"
    assert unknown.root.children[0]._name_ == "item"


def test_profile_ordered_attributes_without_content_report_missing_children() -> None:
    schema = schema_for(ATTR + A + B)
    document = schema.from_data({"r": [{"@z": 3}]}, format="pyxsd-json")
    assert document.report.has_errors
    assert document.root.attribute_lexical("z") == "3"


def test_name_class_helpers_report_open_and_finite_names() -> None:
    from pyxsd.relaxng.json_profile import _finite_class_names, _open_class
    from pyxsd.relaxng.names import AnyName, ExceptName, Name, NameChoice

    choice = NameChoice((Name(None, "r"), AnyName()))
    assert _open_class(choice)
    assert not _open_class(NameChoice((Name(None, "r"),)))
    assert _finite_class_names(NameChoice((Name("urn:a", "r"), Name(None, "s")))) == [
        "{urn:a}r",
        "s",
    ]
    assert _finite_class_names(ExceptName(Name(None, "r"), Name(None, "s"))) == [
        "r",
        "s",
    ]
    assert _finite_class_names(AnyName()) == []


def test_jsonml_reserves_names_for_list_interpretation_wrappers() -> None:
    from pyxsd.relaxng.xml_conversion import _LexicalNames, _reserve_value_names

    schema = schema_for("<list><choice><data type='integer'/><data type='string'/></choice></list>")
    document = valid(schema=schema, xml="<r>12</r>", content="")
    value = document.root.scalar_value()
    interpretation = value.interpretations[0]
    names = _LexicalNames([])
    _reserve_value_names(interpretation, names)
    assert names.declarations() == {}


def test_profile_booleans_rebuild_as_lexical_text() -> None:
    schema = schema_for(STRING)
    document = schema.from_data({"r": True}, format="pyxsd-json")
    assert not document.report.has_errors
    assert document.root.text_value() == "true"


def test_root_name_class_choices_and_exclusions_select_deterministically() -> None:
    choice = RelaxNGSchema.compile(
        io.StringIO(
            f'<choice xmlns="{NS}"><element name="r"><empty/></element>'
            '<element name="s"><empty/></element></choice>'
        )
    )
    conversion_error("root-ambiguous", "", lambda: choice.from_data({}, format="parker"))
    selected = choice.from_data({}, format="parker", element="s")
    assert selected.root._name_ == "s"

    excluded = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}"><anyName><except><name>x</name></except></anyName>'
            "<empty/></element>"
        )
    )
    conversion_error("root-ambiguous", "", lambda: excluded.from_data({}, format="parker"))
    named = excluded.from_data({}, format="parker", element="{urn:a}y")
    assert named.root._name_ == "{urn:a}y"


def test_imported_tree_parse_accepts_sparse_contexts() -> None:
    from xml.etree import ElementTree as ET

    from pyxsd.relaxng.datatypes import DatatypeContext
    from pyxsd.relaxng.json_profile import ImportedTree

    schema = schema_for("<optional><element name='child'><empty/></element></optional>")
    root = ET.Element("r")
    ET.SubElement(root, "child")
    contexts = {id(root): DatatypeContext(None, {"p": "urn:example"})}
    document = schema._parse_imported_tree(ImportedTree(root, contexts, ()), format="pyxsd-json")
    assert not document.report.has_errors
    assert document.root.namespaces == {"p": "urn:example"}
    assert document.root.children[0].namespaces in (None, {})


def test_badgerfish_and_jsonml_carry_scoped_qname_values() -> None:
    content = (
        "<optional><attribute name='z'><data type='QName'/></attribute></optional>"
        "<choice><data type='QName'/><data type='string'/></choice>"
    )
    document = valid(content, '<r z="p:a" xmlns:p="urn:e" xmlns:q="urn:z">q:b</r>')
    for format_name in ("badgerfish", "jsonml"):
        data = document.to_data(format=format_name, fidelity="strict")
        imported = document.schema.from_data(data, format=format_name)
        assert not imported.report.has_errors, (format_name, imported.report.issues)
        attribute = imported.root.attribute_value("z")
        assert attribute.lexical
        assert any(
            isinstance(candidate.decoded, QName)
            and candidate.decoded.to_expanded_name() == "{urn:e}a"
            for candidate in attribute.candidates
        )
        rebuilt = imported.root.scalar_value()
        assert rebuilt.lexical == "q:b"
        assert any(
            isinstance(candidate.decoded, QName)
            and candidate.decoded.to_expanded_name() == "{urn:z}b"
            for candidate in rebuilt.candidates
        )


def test_grouped_root_shape_errors() -> None:
    scalar = schema_for(INTEGER)
    conversion_error("container-invalid", "", lambda: scalar.from_data(12, format="pyxsd"))
    conversion_error("container-invalid", "", lambda: scalar.from_data(12, format="badgerfish"))
    conversion_error("root-ambiguous", "", lambda: scalar.from_data({}, format="badgerfish"))
    conversion_error(
        "root-unresolved",
        "",
        lambda: scalar.from_data({"other": 12}, format="badgerfish"),
    )
    conversion_error(
        "root-unresolved", "", lambda: scalar.from_data(["other", 12], format="jsonml")
    )


def test_native_child_alternative_tags_and_unknown_lists() -> None:
    content = (
        "<zeroOrMore><element name='a'><choice><data type='integer'/>"
        "<data type='string'/></choice></element></zeroOrMore>"
    )
    document = valid(content, "<r><a>12</a><a>x</a></r>")
    data = document.to_data(format="pyxsd", fidelity="strict")
    imported = document.schema.from_data(data, format="pyxsd")
    assert not imported.report.has_errors
    assert [child.text_value() for child in imported.root.children] == ["12", "x"]

    empty = schema_for("<empty/>")
    unknown = empty.from_data({"unknown": [1, 2]}, format="pyxsd")
    assert unknown.report.has_errors
    assert [child._name_ for child in unknown.root.children] == ["unknown", "unknown"]


def test_native_grouped_scalars_and_containers_reject_bad_shapes() -> None:
    record = schema_for(RECORD)
    conversion_error("container-invalid", "", lambda: record.from_data(1, format="pyxsd"))
    nested = schema_for(f"<element name='child'>{RECORD}</element>")
    conversion_error(
        "container-invalid",
        "/child",
        lambda: nested.from_data({"child": 1}, format="pyxsd"),
    )
    conversion_error(
        "json-duplicate-key",
        "/r/Q{}a",
        lambda: record.from_data({"r": {"a": 1, "Q{}a": 2}}, format="pyxsd"),
    )

    attributed = schema_for(ATTR + INTEGER)
    conversion_error(
        "scalar-type-invalid",
        "/@z",
        lambda: attributed.from_data({"@z": {"bad": 1}, "$": 12}, format="pyxsd"),
    )
    boolean = attributed.from_data({"@z": True, "$": 12}, format="pyxsd")
    assert boolean.root.attribute_lexical("z") == "true"
    conversion_error(
        "container-invalid",
        "/a",
        lambda: schema_for(f"<element name='a'>{STRING}</element>").from_data(
            {"a": {"nested": "v"}}, format="pyxsd"
        ),
    )


def test_grouped_generic_unknown_children_have_no_reconstructable_order() -> None:
    empty = schema_for("<empty/>")
    conversion_error(
        "order-ambiguous",
        "/unknown",
        lambda: empty.from_data({"unknown": {"inner": "v"}}, format="pyxsd"),
    )
    with pytest.warns(PyXSDWarning):
        imported = empty.from_data(
            {"unknown": {"inner": "v"}}, format="pyxsd", fidelity="best-effort"
        )
    assert imported.root.children[0]._name_ == "unknown"


def test_parker_accepts_clark_child_keys_and_rejects_scalar_groups() -> None:
    schema = schema_for(NAMESPACED)
    document = schema.from_data({"{urn:a}child": "v", "a": 1}, format="parker")
    assert [child._name_ for child in document.root.children] == ["{urn:a}child", "a"]
    assert not document.report.has_errors
    conversion_error(
        "container-invalid",
        "",
        lambda: schema_for(RECORD).from_data(1, format="parker"),
    )
    conversion_error(
        "container-invalid",
        "/a",
        lambda: schema_for(f"<element name='a'>{STRING}</element>").from_data(
            {"a": {"nested": "v"}}, format="parker"
        ),
    )
    conversion_error(
        "json-duplicate-key",
        "/Q{}a",
        lambda: schema_for(RECORD).from_data({"a": "", "Q{}a": ""}, format="parker"),
    )


def test_badgerfish_declaration_validation() -> None:
    schema = schema_for(INTEGER)
    conversion_error(
        "container-invalid",
        "/r/@xmlns",
        lambda: schema.from_data({"r": {"@xmlns": "urn:x"}}, format="badgerfish"),
    )
    conversion_error(
        "name-invalid",
        "/r/@xmlns/bad:prefix",
        lambda: schema.from_data({"r": {"@xmlns": {"bad:prefix": "urn:x"}}}, format="badgerfish"),
    )
    conversion_error(
        "container-invalid",
        "/r/@xmlns/p",
        lambda: schema.from_data({"r": {"@xmlns": {"p": 1}}}, format="badgerfish"),
    )
    conversion_error(
        "namespace-conflict",
        "/r/@xmlns",
        lambda: schema.from_data({"r": {"@xmlns": {"xml": "urn:x"}, "$": 1}}, format="badgerfish"),
    )
    conversion_error(
        "container-invalid",
        "/r",
        lambda: schema_for(RECORD).from_data({"r": 1}, format="badgerfish"),
    )
    conversion_error(
        "container-invalid",
        "/r/a",
        lambda: schema_for(f"<element name='a'>{STRING}</element>").from_data(
            {"r": {"a": {"nested": "v"}}}, format="badgerfish"
        ),
    )
    conversion_error(
        "json-duplicate-key",
        "/r/Q{}a",
        lambda: schema_for(RECORD).from_data({"r": {"a": "", "Q{}a": ""}}, format="badgerfish"),
    )


def test_grouped_lexical_names_must_be_valid_qnames() -> None:
    schema = schema_for("<empty/>")
    conversion_error(
        "name-invalid", "/r/:x", lambda: schema.from_data({"r": {":x": ""}}, format="badgerfish")
    )
    conversion_error(
        "name-invalid", "/r/p:", lambda: schema.from_data({"r": {"p:": ""}}, format="badgerfish")
    )
    conversion_error(
        "name-invalid",
        "/r/bad name",
        lambda: schema.from_data({"r": {"bad name": ""}}, format="badgerfish"),
    )


def test_jsonml_booleans_default_namespaces_and_bad_child_arrays() -> None:
    string = schema_for(STRING)
    boolean = string.from_data(["r", True], format="jsonml")
    assert not boolean.report.has_errors
    assert boolean.root.text_value() == "true"

    namespaced = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" name="r" ns="urn:a"><empty/></element>')
    )
    document = namespaced.from_data(["r", {"xmlns": "urn:a"}], format="jsonml")
    assert document.root._name_ == "{urn:a}r"
    assert not document.report.has_errors

    conversion_error(
        "container-invalid", "/1", lambda: string.from_data(["r", [12]], format="jsonml")
    )
    conversion_error(
        "container-invalid",
        "/1",
        lambda: schema_for(A + B).from_data(["r", [12]], format="jsonml"),
    )
    conversion_error("container-invalid", "", lambda: string.from_data([["r"]], format="jsonml"))


def test_jsonml_declaration_validation_and_clark_tags() -> None:
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" datatypeLibrary="{XSD}" name="r" ns="urn:a">'
            '<data type="integer"/></element>'
        )
    )
    document = schema.from_data(["{urn:a}r", 12], format="jsonml")
    assert document.root._name_ == "{urn:a}r"
    assert not document.report.has_errors
    conversion_error(
        "name-invalid",
        "/1/xmlns:bad:prefix",
        lambda: schema.from_data(["r", {"xmlns:bad:prefix": "urn:a"}, 12], format="jsonml"),
    )
    conversion_error(
        "container-invalid",
        "/1/xmlns:p",
        lambda: schema.from_data(["r", {"xmlns:p": 1}, 12], format="jsonml"),
    )
    conversion_error(
        "name-invalid",
        "/1/@xmlns",
        lambda: schema.from_data(["{urn:a}r", {"@xmlns": "urn:a"}, 12], format="jsonml"),
    )


# ---------------------------------------------------------------------------
# Review regressions: grouped order, list ambiguity, attributes, and QName wire
# ---------------------------------------------------------------------------


def test_native_sequence_preserves_json_key_order_over_static_partitions() -> None:
    cases = [
        (
            '<element name="z"><data type="integer"/></element>'
            '<element name="a"><data type="string"/></element>',
            "<r><z>1</z><a>x</a></r>",
            ["z", "a"],
        ),
        (
            '<element name="y"><data type="integer"/></element>'
            "<zeroOrMore><element name='a'><data type='string'/></element></zeroOrMore>",
            "<r><y>1</y><a>x</a><a>y</a></r>",
            ["y", "a", "a"],
        ),
        (
            '<element name="y"><data type="integer"/></element>'
            "<zeroOrMore><element name='a'><data type='string'/></element></zeroOrMore>"
            '<element name="z"><data type="string"/></element>',
            "<r><y>1</y><a>x</a><a>y</a><z>end</z></r>",
            ["y", "a", "a", "z"],
        ),
    ]
    for content, xml, expected in cases:
        document = valid(content, xml)
        data = document.to_data(format="pyxsd", fidelity="strict")
        imported = document.schema.from_data(data, format="pyxsd")
        assert not imported.report.has_errors, imported.report.issues
        assert [child._name_ for child in imported.root.children] == expected
        assert imported.to_data(format="pyxsd", fidelity="strict") == data


def test_native_mixed_children_preserve_json_key_order() -> None:
    document = valid(f"<mixed>{B}{A}</mixed>", "<r><b>B</b><a>1</a></r>")
    data = document.to_data(format="pyxsd", fidelity="strict")
    imported = document.schema.from_data(data, format="pyxsd")
    assert not imported.report.has_errors
    assert [child._name_ for child in imported.root.children] == ["b", "a"]
    assert imported.to_data(format="pyxsd", fidelity="strict") == data


_LIST_OR_INTEGER = (
    '<element name="a"><choice><list><zeroOrMore><data type="integer"/>'
    '</zeroOrMore></list><data type="integer"/></choice></element>'
)


def test_single_list_or_atomic_child_stays_one_list_scalar() -> None:
    document = valid(f"<optional>{_LIST_OR_INTEGER}</optional>", "<r><a>1 2</a></r>")
    for format_name in ("parker", "badgerfish", "jsonml"):
        data = document.to_data(format=format_name, fidelity="strict")
        imported = document.schema.from_data(data, format=format_name)
        assert not imported.report.has_errors, (format_name, imported.report.issues)
        assert [child.text_value() for child in imported.root.children] == ["1 2"]
        assert imported.to_data(format=format_name, fidelity="strict") == data, format_name


def test_repeated_list_or_atomic_child_requires_unambiguous_nesting() -> None:
    document = valid(f"<zeroOrMore>{_LIST_OR_INTEGER}</zeroOrMore>", "<r><a>1 2</a></r>")
    parker_data = document.to_data(format="parker", fidelity="strict")
    conversion_error(
        "scalar-type-ambiguous",
        "/a",
        lambda: document.schema.from_data(parker_data, format="parker"),
    )

    for format_name in ("badgerfish", "jsonml"):
        data = document.to_data(format=format_name, fidelity="strict")
        imported = document.schema.from_data(data, format=format_name)
        assert not imported.report.has_errors, (format_name, imported.report.issues)
        assert [child.text_value() for child in imported.root.children] == ["1 2"]
        assert imported.to_data(format=format_name, fidelity="strict") == data

    one = document.schema.from_data({"a": [[1, 2]]}, format="parker")
    assert not one.report.has_errors
    assert [child.text_value() for child in one.root.children] == ["1 2"]
    two = document.schema.from_data({"a": [[1, 2], [3, 4]]}, format="parker")
    assert not two.report.has_errors
    assert [child.text_value() for child in two.root.children] == ["1 2", "3 4"]

    pure = valid(
        "<zeroOrMore><element name='a'><list><zeroOrMore><data type='integer'/>"
        "</zeroOrMore></list></element></zeroOrMore>",
        "<r><a>1 2</a></r>",
    )
    flat = pure.to_data(format="parker", fidelity="strict")
    assert flat == {"a": [1, 2]}
    imported = pure.schema.from_data(flat, format="parker")
    assert not imported.report.has_errors
    assert [child.text_value() for child in imported.root.children] == ["1 2"]


def test_native_tagged_list_or_atomic_child_keeps_its_assertions() -> None:
    document = valid(
        f"<zeroOrMore>{_LIST_OR_INTEGER}</zeroOrMore>",
        "<r><a>1 2</a><a>3</a></r>",
    )
    data = document.to_data(format="pyxsd", fidelity="strict")
    assert "#value" in json.dumps(data)
    imported = document.schema.from_data(data, format="pyxsd")
    assert not imported.report.has_errors
    assert [child.text_value() for child in imported.root.children] == ["1 2", "3"]
    assert imported.to_data(format="pyxsd", fidelity="strict") == data


def test_grouped_wildcard_list_child_uses_one_scalar() -> None:
    content = "<zeroOrMore><element><anyName/><data type='NMTOKENS'/></element></zeroOrMore>"
    document = valid(content, "<r><item>a b</item></r>")
    for format_name in ("pyxsd", "parker", "badgerfish"):
        data = document.to_data(format=format_name, fidelity="strict")
        imported = document.schema.from_data(data, format=format_name)
        assert not imported.report.has_errors, (format_name, imported.report.issues)
        assert [child.text_value() for child in imported.root.children] == ["a b"]
        assert imported.to_data(format=format_name, fidelity="strict") == data


def test_grouped_child_field_finds_child_wildcards_and_absent_slots() -> None:
    from pyxsd.relaxng.xml_conversion import _child_field

    mixed = schema_for(
        "<zeroOrMore><attribute><anyName/><text/></attribute></zeroOrMore>"
        "<zeroOrMore><element><anyName/><text/></element></zeroOrMore>"
    )
    route = mixed.projection.routes[mixed.projection.roots[0]]
    field = _child_field(route, "item")
    assert field is not None
    assert field.role == "child"

    empty = schema_for("<empty/>")
    absent = empty.projection.routes[empty.projection.roots[0]]
    assert _child_field(absent, "unknown") is None


def test_grouped_list_attributes_join_tokens() -> None:
    qname_content = (
        '<optional><attribute name="z"><list><zeroOrMore><data type="QName"/>'
        "</zeroOrMore></list></attribute></optional><empty/>"
    )
    cases = [
        (
            '<optional><attribute name="z"><data type="NMTOKENS"/></attribute></optional><empty/>',
            '<r z="a b"/>',
            "a b",
            2,
            None,
        ),
        (
            '<optional><attribute name="z"><list><zeroOrMore><data type="integer"/>'
            "</zeroOrMore></list></attribute></optional><empty/>",
            '<r z="1 2"/>',
            "1 2",
            2,
            None,
        ),
        (
            qname_content,
            '<r xmlns:p="urn:e" z="p:a p:b"/>',
            None,
            2,
            ["{urn:e}a", "{urn:e}b"],
        ),
    ]
    for content, xml, lexical, count, expanded in cases:
        document = valid(content, xml)
        for format_name in ("badgerfish", "jsonml"):
            data = document.to_data(format=format_name, fidelity="strict")
            imported = document.schema.from_data(data, format=format_name)
            assert not imported.report.has_errors, (format_name, imported.report.issues)
            if expanded is None:
                assert imported.root.attribute_lexical("z") == lexical
                assert imported.to_data(format=format_name, fidelity="strict") == data
            else:
                values = imported.root.attribute_value("z")
                assert len(values) == count
                assert [value.to_expanded_name() for value in values] == expanded


def test_grouped_list_attribute_with_mixed_atomic_choice_round_trips() -> None:
    content = (
        '<optional><attribute name="z"><choice><list><zeroOrMore>'
        '<data type="integer"/></zeroOrMore></list><data type="integer"/>'
        "</choice></attribute></optional><empty/>"
    )
    document = valid(content, '<r z="1 2"/>')
    for format_name in ("badgerfish", "jsonml"):
        data = document.to_data(format=format_name, fidelity="strict")
        imported = document.schema.from_data(data, format=format_name)
        assert not imported.report.has_errors, (format_name, imported.report.issues)
        assert imported.root.attribute_lexical("z") == "1 2"
        assert imported.to_data(format=format_name, fidelity="strict") == data


def test_heterogeneous_list_qname_string_content_keeps_literal_tokens() -> None:
    content = '<list><data type="QName"/><data type="string"/></list>'
    document = valid(content, '<r xmlns:p="urn:e">p:a Q{urn:x}b</r>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    assert data == {"r": ["Q{urn:e}a", "Q{urn:x}b"]}

    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    assert imported.root.text_value() == "ns0:a Q{urn:x}b"
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data


def test_heterogeneous_list_qname_string_content_with_tag_context() -> None:
    content = (
        "<optional><attribute name='z'><choice><data type='integer'/>"
        "<data type='string'/></choice></attribute></optional>"
        '<list><data type="QName"/><data type="string"/></list>'
    )
    document = valid(content, '<r xmlns:p="urn:e" z="1">p:a Q{urn:x}b</r>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    assert imported.root.text_value() == "p:a Q{urn:x}b"
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data


def test_heterogeneous_list_qname_string_attribute_keeps_literal_tokens() -> None:
    content = (
        '<optional><attribute name="z"><list><data type="QName"/>'
        '<data type="string"/></list></attribute></optional><empty/>'
    )
    document = valid(content, '<r xmlns:p="urn:e" z="p:a Q{urn:x}b"/>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    assert data == {"r": {"@z": ["Q{urn:e}a", "Q{urn:x}b"]}}
    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    assert imported.root.attribute_lexical("z") == "ns0:a Q{urn:x}b"
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data


def test_list_token_contract_resolution_handles_structure() -> None:
    from pyxsd.relaxng.contracts import scalar_contract
    from pyxsd.relaxng.json_profile import (
        _fixed_token_count,
        _token_contracts,
        _token_position_contracts,
    )
    from pyxsd.relaxng.patterns import (
        Choice,
        Data,
        Empty,
        Group,
        OneOrMore,
        Ref,
        Text,
    )

    schema = schema_for("<data type='integer'/>")
    plan = schema.projection
    data = next(pattern for pattern in plan.encoding_patterns.values() if isinstance(pattern, Data))
    integer = scalar_contract(data)

    assert _fixed_token_count(Empty(), set()) == 0
    assert _fixed_token_count(Group(data, data), set()) == 2
    assert _fixed_token_count(Choice(Empty(), OneOrMore(data)), set()) is None
    assert _fixed_token_count(OneOrMore(Empty()), set()) == 0
    assert _fixed_token_count(Text(), set()) is None

    ref = Ref()
    ref.bind(data)
    assert _fixed_token_count(ref, set()) == 1
    cycle = Ref()
    cycle.bind(cycle)
    assert _fixed_token_count(cycle, set()) is None

    assert _token_contracts(Group(data, data), 1, set()) == frozenset({integer})
    assert _token_contracts(Group(OneOrMore(data), data), 0, set()) is None
    assert _token_contracts(Choice(Empty(), OneOrMore(data)), 0, set()) == frozenset({integer})
    assert _token_contracts(OneOrMore(Empty()), 0, set()) == frozenset()
    assert _token_contracts(OneOrMore(Group(data, data)), 1, set()) == frozenset({integer})
    assert _token_contracts(OneOrMore(Choice(Empty(), OneOrMore(data))), 0, set()) is None
    assert _token_contracts(Text(), 0, set()) is None
    assert _token_contracts(ref, 0, set()) == frozenset({integer})
    assert _token_contracts(cycle, 0, set()) is None

    variable = schema_for(
        '<list><zeroOrMore><data type="QName"/></zeroOrMore><data type="string"/></list>'
    )
    variable_plan = variable.projection
    route = variable_plan.routes[variable_plan.roots[0]]
    list_contract = route.content.scalar_contracts[0]
    assert list_contract.kind == "list"
    assert _token_position_contracts((list_contract,), 0, variable_plan) is None


def test_badgerfish_repeatable_wildcard_wraps_occurrences() -> None:
    content = (
        "<zeroOrMore><element><anyName/><choice>"
        '<list><oneOrMore><data type="integer"/></oneOrMore></list>'
        '<data type="integer"/></choice></element></zeroOrMore>'
    )

    repeated = valid(content, "<r><item>1</item><item>2</item></r>")
    repeated_data = repeated.to_data(format="badgerfish", fidelity="strict")
    assert repeated_data == {"r": {"item": ["1", "2"]}}
    imported = repeated.schema.from_data(repeated_data, format="badgerfish")
    assert not imported.report.has_errors, imported.report.issues
    assert [child.text_value() for child in imported.root.children] == ["1", "2"]
    assert imported.to_data(format="badgerfish", fidelity="strict") == repeated_data

    listed = valid(content, "<r><item>1 2</item></r>")
    listed_data = listed.to_data(format="badgerfish", fidelity="strict")
    assert listed_data == {"r": {"item": [[1, 2]]}}
    listed_import = listed.schema.from_data(listed_data, format="badgerfish")
    assert not listed_import.report.has_errors, listed_import.report.issues
    assert [child.text_value() for child in listed_import.root.children] == ["1 2"]
    assert listed_import.to_data(format="badgerfish", fidelity="strict") == listed_data


def test_badgerfish_nonrepeatable_wildcard_list_is_not_over_wrapped() -> None:
    content = (
        "<element><anyName/><list><zeroOrMore><data type='integer'/></zeroOrMore></list></element>"
    )
    document = valid(content, "<r><item>1 2</item></r>")
    data = document.to_data(format="badgerfish", fidelity="strict")
    assert data == {"r": {"item": [1, 2]}}
    imported = document.schema.from_data(data, format="badgerfish")
    assert not imported.report.has_errors, imported.report.issues
    assert [child.text_value() for child in imported.root.children] == ["1 2"]
    assert imported.to_data(format="badgerfish", fidelity="strict") == data


def test_badgerfish_flat_arrays_are_repeated_scalar_occurrences() -> None:
    content = (
        "<zeroOrMore><element name='a'><choice><list><zeroOrMore>"
        '<data type="integer"/></zeroOrMore></list><data type="integer"/>'
        "</choice></element></zeroOrMore>"
    )
    cases = [
        ("<r><a>1</a></r>", ["1"]),
        ("<r><a>1</a><a>2</a></r>", ["1", "2"]),
        ("<r><a>1 2</a></r>", ["1 2"]),
    ]
    for xml, expected in cases:
        document = valid(content, xml)
        data = document.to_data(format="badgerfish", fidelity="strict")
        imported = document.schema.from_data(data, format="badgerfish")
        assert not imported.report.has_errors, imported.report.issues
        assert [child.text_value() for child in imported.root.children] == expected
        assert imported.to_data(format="badgerfish", fidelity="strict") == data

    list_data = {"r": {"a": [[1, 2]]}}
    listed = valid(content, "<r><a>1 2</a></r>").schema.from_data(list_data, format="badgerfish")
    assert not listed.report.has_errors
    assert [child.text_value() for child in listed.root.children] == ["1 2"]


def test_profile_list_qname_content_and_attribute_round_trip() -> None:
    content = '<list><zeroOrMore><data type="QName"/></zeroOrMore></list>'
    document = valid(content, '<r xmlns:p="urn:e" xmlns:q="urn:z">p:a q:b</r>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    assert imported.root.text_value() == "ns0:a ns1:b"
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data

    attribute = (
        '<optional><attribute name="z"><list><zeroOrMore><data type="QName"/>'
        "</zeroOrMore></list></attribute></optional><empty/>"
    )
    attributed = valid(attribute, '<r xmlns:p="urn:e" z="p:a p:b"/>')
    attr_data = attributed.to_data(format="pyxsd-json", fidelity="strict")
    attr_imported = attributed.schema.from_data(attr_data, format="pyxsd-json")
    assert not attr_imported.report.has_errors
    assert attr_imported.to_data(format="pyxsd-json", fidelity="strict") == attr_data


def test_profile_default_namespace_qname_uses_the_local_name_under_a_tag() -> None:
    content = (
        "<optional><attribute name='z'><choice><data type='integer'/>"
        "<data type='string'/></choice></attribute></optional><data type='QName'/>"
    )
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" datatypeLibrary="{XSD}" name="r" ns="urn:d">{content}</element>'
        )
    )
    document = schema.parse(io.StringIO('<r xmlns="urn:d" z="1">v</r>'), typed=True)
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    imported = schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    assert imported.root.text_value() == "v"
    assert imported.root.namespaces == {"": "urn:d"}
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data


def test_profile_xml_namespace_qname_needs_no_added_binding() -> None:
    content = (
        "<optional><attribute name='z'><choice><data type='integer'/>"
        "<data type='string'/></choice></attribute></optional><data type='QName'/>"
    )
    document = valid(content, '<r z="1">xml:lang</r>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    assert imported.root.text_value() == "xml:lang"
    assert "xml" not in (imported.root.namespaces or {})
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data


def test_duplicate_attribute_spellings_are_rejected() -> None:
    content = '<attribute name="z"><data type="string"/></attribute><empty/>'
    cases = [
        ("pyxsd-json", {"r": {"@z": "a", "@Q{}z": "b"}}, "/r/@Q{}z"),
        ("pyxsd", {"@z": "a", "@Q{}z": "b"}, "/@Q{}z"),
        ("badgerfish", {"r": {"@z": "a", "@Q{}z": "b"}}, "/r/@Q{}z"),
        ("jsonml", ["r", {"z": "a", "@z": "b"}], "/1/@z"),
    ]
    for format_name, data, pointer in cases:
        schema = schema_for(content)
        conversion_error(
            "json-duplicate-key",
            pointer,
            lambda format_name=format_name, data=data, schema=schema: schema.from_data(
                data, format=format_name
            ),
        )


def test_native_content_tag_errors_point_at_the_dollar_member() -> None:
    document = valid(ALT, "<r>12</r>")
    data = document.to_data(format="pyxsd", fidelity="strict")
    data["$"]["#value"]["version"] = 2
    conversion_error(
        "scalar-tag-version",
        "/$",
        lambda: document.schema.from_data(data, format="pyxsd"),
    )


def test_post_parse_claim_mismatch_carries_the_format() -> None:
    document = valid(ALT, "<r>12</r>")
    data = alt_data(document)
    data["r"]["#value"]["candidates"] = ["forged"]
    with pytest.raises(
        PyXSDError,
        match=r"scalar-tag-candidates-mismatch at '/r'.*\(format='pyxsd-json'\)",
    ):
        document.schema.from_data(data, format="pyxsd-json")


def test_xsd_import_surface_remains_available() -> None:
    import pyxsd

    xsd_schema = pyxsd.Schema.compile(io.StringIO(XSD_MINIMAL))
    document = xsd_schema.from_data({"note": "hi"}, format="parker", element="note")
    assert document.root is not None
    with pytest.raises(PyXSDError):
        xsd_schema.from_data(
            {"note": {"#value": {"version": 1}}}, format="pyxsd-json", element="note"
        )


XSD_MINIMAL = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="note"><xs:complexType><xs:sequence>'
    '<xs:element name="body" type="xs:string"/>'
    "</xs:sequence></xs:complexType></xs:element></xs:schema>"
)
