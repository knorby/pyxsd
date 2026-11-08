"""Tests for ``pyxsd-json`` import and the public schema adapters.

Import must reach a normal :class:`~pyxsd.document.Document` through the
existing binder: conversion errors (malformed JSON, wrong containers,
wrong scalar types, conflicting markers) raise before any tree exists,
while correctly shaped but lexically invalid content lands on the
document's validation report. Round trips over the shared hybrid export
fixtures pin the layout decoding for every layout, and the generated
classes, defaults, transforms, and XML output behave exactly as they do
for documents parsed from XML.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from tests.test_data_export import (
    ABSTRACT_SUBSTITUTION_SCHEMA,
    EMPTY_ATTRIBUTE_SCHEMA,
    EMPTY_SCHEMA,
    MIXED_NILABLE_SCHEMA,
    MIXED_NO_ATTRIBUTE_SCHEMA,
    NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
    NAMESPACED_SUBSTITUTION_SCHEMA,
    QNAME_ATTRIBUTE_SCHEMA,
    QNAME_CHILD_SCHEMA,
    QNAME_LIST_SCHEMA,
    QNAME_ROOT_SCHEMA,
    RECORD_SCHEMA,
    REPEATED_ATTRIBUTE_SCHEMA,
    REPEATED_ATTRIBUTED_CHILD_SCHEMA,
    REPEATED_NILABLE_ATTRIBUTE_SCHEMA,
    REPEATED_SCHEMA,
    SEQUENCE_ATTRIBUTE_SCHEMA,
    SEQUENCE_NILABLE_SCHEMA,
    SEQUENCE_OPTIONAL_SCHEMA,
    SEQUENCE_REPEATED_CHOICE_SCHEMA,
    SEQUENCE_WILDCARD_SCHEMA,
    SHARED_EXPECTED,
    SIMPLE_CONTENT_ATTRIBUTE_SCHEMA,
    SIMPLE_CONTENT_INT_ATTRIBUTE_SCHEMA,
    SIMPLE_CONTENT_WILDCARD_SCHEMA,
    SUBSTITUTION_SCHEMA,
    UNNAMESPACED_XSITYPE_SCHEMA,
    WILDCARD_GENERIC_SCHEMA,
    XSITYPE_CHILD_SCHEMA,
    XSITYPE_EQUAL_SCHEMA,
    XSITYPE_LIST_SCHEMA,
    XSITYPE_RECORD_SCHEMA,
    XSITYPE_SCHEMA,
    XSITYPE_SIMPLE_SCHEMA,
)
from tests.test_data_formats import FIXTURES

import pyxsd
from pyxsd.data_conversion.contracts import JsonValue
from pyxsd.data_conversion.import_data import data_to_element, json_to_data
from pyxsd.exceptions import PyXSDError, ValidationError

XSI_TYPE_EQNAME = "Q{urn:example}Ext"
XSI_INT_EQNAME = "Q{http://www.w3.org/2001/XMLSchema}int"

CONDITIONAL_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="ListType">
    <xs:sequence>
      <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="kind" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="RecordType">
    <xs:all>
      <xs:element name="a" type="xs:string"/>
    </xs:all>
    <xs:attribute name="kind" type="xs:string"/>
  </xs:complexType>
  <xs:element name="root" type="xs:anyType">
    <xs:alternative test="@kind='list'" type="ListType"/>
    <xs:alternative test="@kind='record'" type="RecordType"/>
  </xs:element>
</xs:schema>"""

DEFAULT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="e" type="xs:string" default="dflt"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

DEFAULT_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:string" default="fallback"/>
</xs:schema>"""


def _schema(source: str) -> pyxsd.Schema:
    schema = pyxsd.Schema.compile(StringIO(source))
    assert not schema.report.errors
    return schema


def _compile(schema_source: str, instance: str) -> pyxsd.Document:
    return _schema(schema_source).parse(StringIO(instance))


def _import(schema_source: str | pyxsd.Schema, data: object, **options: object) -> pyxsd.Document:
    schema = schema_source if isinstance(schema_source, pyxsd.Schema) else _schema(schema_source)
    return schema.from_data(data, **options)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Strict JSON parsing
# ---------------------------------------------------------------------------


def test_json_to_data_parses_text_bytes_and_bytearray_equally():
    text = '{"root": [1, 2.5, "x", true, null]}'
    expected = {"root": [1, 2.5, "x", True, None]}

    assert json_to_data(text) == expected
    assert json_to_data(text.encode("utf-8")) == expected
    assert json_to_data(bytearray(text.encode("utf-8"))) == expected


@pytest.mark.parametrize(
    "source",
    [
        '{"a": 1, "a": 2}',
        '{"a": {"b": 1, "b": 2}}',
    ],
)
def test_json_to_data_rejects_duplicate_keys(source):
    with pytest.raises(PyXSDError, match="json-duplicate-key"):
        json_to_data(source)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_to_data_rejects_non_standard_numeric_constants(constant):
    with pytest.raises(PyXSDError, match="json-invalid"):
        json_to_data(f'{{"a": {constant}}}')


@pytest.mark.parametrize(
    "source",
    ['{"a": ', "[1, 2", "'not json'", "{} extra", ""],
)
def test_json_to_data_rejects_malformed_json(source):
    with pytest.raises(PyXSDError, match="json-invalid"):
        json_to_data(source)


def test_json_to_data_rejects_non_utf8_bytes():
    with pytest.raises(PyXSDError, match="json-invalid"):
        json_to_data(b'{"a": "\xff\xfe"}')


@pytest.mark.parametrize("source", [Path("data.json"), 42, None, ["json"], {"a": 1}])
def test_json_to_data_rejects_non_text_sources(source):
    with pytest.raises(TypeError):
        json_to_data(source)


def test_json_to_data_reports_the_offending_duplicate_key():
    with pytest.raises(PyXSDError, match="duplicate key 'a'"):
        json_to_data('{"a": 1, "a": 2}')


# ---------------------------------------------------------------------------
# Shared hybrid fixture round trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SHARED_EXPECTED))
def test_each_shared_hybrid_fixture_round_trips(name):
    schema_source, instance_source = FIXTURES[name]
    document = _compile(schema_source, instance_source)
    exported = document.to_data(format="pyxsd-json")
    assert exported == SHARED_EXPECTED[name]

    imported = document.schema.from_data(exported, format="pyxsd-json")

    assert imported.root is not None
    assert imported.to_data(format="pyxsd-json") == exported
    imported.require_valid()


@pytest.mark.parametrize("name", sorted(SHARED_EXPECTED))
def test_each_shared_fixture_survives_revalidate_and_transforms(name):
    schema_source, instance_source = FIXTURES[name]
    document = _compile(schema_source, instance_source)
    exported = document.to_data(format="pyxsd-json")
    imported = document.schema.from_data(exported, format="pyxsd-json")

    again = imported.revalidate()
    again.require_valid()
    assert again.to_data(format="pyxsd-json") == exported

    from pyxsd.transforms import ToData

    assert imported.transform(ToData(format="pyxsd-json")) == exported


@pytest.mark.parametrize("name", sorted(SHARED_EXPECTED))
def test_from_json_matches_from_data_for_shared_fixtures(name):
    schema_source, instance_source = FIXTURES[name]
    document = _compile(schema_source, instance_source)
    exported = document.to_data(format="pyxsd-json")

    from_data = document.schema.from_data(exported, format="pyxsd-json")
    from_json = document.schema.from_json(json.dumps(exported), format="pyxsd-json")

    assert from_json.to_data(format="pyxsd-json") == from_data.to_data(format="pyxsd-json")
    from_json.require_valid()


# ---------------------------------------------------------------------------
# Layout decoding
# ---------------------------------------------------------------------------


def test_empty_layout_builds_an_empty_element():
    imported = _import(EMPTY_SCHEMA, {"root": ""})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": ""}


def test_empty_plus_layout_reads_attributes_and_nil():
    attributed = _import(EMPTY_ATTRIBUTE_SCHEMA, {"root": {"@id": "x"}})
    assert attributed.to_data(format="pyxsd-json") == {"root": {"@id": "x"}}

    nilled = _import(
        EMPTY_ATTRIBUTE_SCHEMA.replace('name="root"', 'name="root" nillable="true"'),
        {"root": {"@id": "x", "#content": None}},
    )
    nilled.require_valid()
    assert nilled.to_data(format="pyxsd-json") == {"root": {"@id": "x", "#content": None}}


def test_simple_and_simple_plus_layouts_read_text_and_attributes():
    simple = _import(
        SIMPLE_CONTENT_ATTRIBUTE_SCHEMA,
        {"root": {"@id": "r", "#content": "text"}},
    )
    assert simple.to_data(format="pyxsd-json") == {"root": {"@id": "r", "#content": "text"}}

    absent_content = _import(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, {"root": {"@id": "r"}})
    assert absent_content.to_data(format="pyxsd-json") == {"root": {"@id": "r", "#content": ""}}


def test_list_layout_creates_one_child_per_array_item():
    imported = _import(REPEATED_SCHEMA, {"dates": ["2026-01-01", "2026-02-01"]})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"dates": ["2026-01-01", "2026-02-01"]}
    assert [str(date) for date in imported.root.date] == ["2026-01-01", "2026-02-01"]


def test_list_children_keep_their_attributes():
    imported = _import(
        REPEATED_ATTRIBUTED_CHILD_SCHEMA,
        {"root": [{"@id": 1, "#content": "A"}, {"@id": 2, "#content": "B"}]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "root": [{"@id": 1, "#content": "A"}, {"@id": 2, "#content": "B"}]
    }


def test_list_plus_reads_attributes_and_the_child_array():
    imported = _import(
        REPEATED_ATTRIBUTE_SCHEMA,
        {"dates": {"@source": "known", "date": ["2026-01-01"]}},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "dates": {"@source": "known", "date": ["2026-01-01"]}
    }
    assert imported.root.source == "known"


def test_list_plus_keeps_an_empty_child_array_without_attributes():
    imported = _import(REPEATED_ATTRIBUTE_SCHEMA, {"dates": {"date": []}})

    assert imported.root is not None
    assert imported.to_data(format="pyxsd-json") == {"dates": {"date": []}}
    # The schema requires at least one date; that lands on the report.
    assert any(error.code == "occurrence-min" for error in imported.report.errors)


def test_list_plus_nil_omits_the_child_array():
    imported = _import(
        REPEATED_NILABLE_ATTRIBUTE_SCHEMA,
        {"dates": {"@source": "known", "#content": None}},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "dates": {"@source": "known", "#content": None}
    }


def test_record_creates_children_in_schema_order_regardless_of_json_order():
    imported = _import(RECORD_SCHEMA, {"root": {"@id": "r", "b": "B", "a": "A"}})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": {"@id": "r", "a": "A", "b": "B"}}
    assert [child._name_ for child in imported.root._children_] == ["a", "b"]


@pytest.mark.parametrize(
    ("child_value", "expected"),
    [
        ("B", {"root": {"a": "A", "b": "B"}}),
        ("", {"root": {"a": "A", "b": ""}}),
        (None, {"root": {"a": "A", "b": None}}),
    ],
)
def test_record_distinguishes_absent_empty_and_nil_children(child_value, expected):
    imported = _import(RECORD_SCHEMA, {"root": {"a": "A", "b": child_value}})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == expected


def test_sequence_preserves_array_order_and_leading_attribute_maps():
    imported = _import(
        SEQUENCE_REPEATED_CHOICE_SCHEMA,
        {"root": [{"a": 1}, {"b": "x"}, {"a": 2}]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"a": 1}, {"b": "x"}, {"a": 2}]}


def test_sequence_covers_zero_occurrences():
    imported = _import(SEQUENCE_OPTIONAL_SCHEMA, {"root": []})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": []}
    assert imported.root._children_ == []


def test_sequence_nil_marker_follows_the_attribute_map():
    imported = _import(
        SEQUENCE_NILABLE_SCHEMA,
        {"root": [{"@id": "r"}, {"#content": None}]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"@id": "r"}, {"#content": None}]}


def test_mixed_retains_text_and_tail_order():
    imported = _import(
        MIXED_NO_ATTRIBUTE_SCHEMA,
        {"root": ["a", {"i": "x"}, "b", {"i": "y"}, "c"]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "root": ["a", {"i": "x"}, "b", {"i": "y"}, "c"]
    }


def test_mixed_adjacent_strings_merge_into_one_text_region():
    imported = _import(MIXED_NO_ATTRIBUTE_SCHEMA, {"root": ["a", "b", {"i": "x"}, "c"]})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": ["ab", {"i": "x"}, "c"]}


def test_mixed_nil_marker_follows_the_attribute_map():
    imported = _import(
        MIXED_NILABLE_SCHEMA,
        {"root": [{"@id": "p1"}, {"#content": None}]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"@id": "p1"}, {"#content": None}]}


def test_whitespace_only_sequence_text_is_discarded():
    imported = _import(SEQUENCE_OPTIONAL_SCHEMA, {"root": ["  ", {"a": "A"}, "\n"]})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"a": "A"}]}
    # The whitespace never reaches the bound tree as character data.
    assert imported.root._value_ is None
    assert getattr(imported.root._children_[0], "_tail_", None) is None


def test_non_whitespace_sequence_text_reaches_the_binder_report():
    imported = _import(SEQUENCE_OPTIONAL_SCHEMA, {"root": [{"a": "A"}, "oops"]})

    assert imported.root is not None
    assert imported.report.has_errors
    with pytest.raises(ValidationError):
        imported.require_valid()


# ---------------------------------------------------------------------------
# Root selection
# ---------------------------------------------------------------------------


def test_a_single_root_object_is_required():
    schema = _schema(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data(["root"])
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data("root")
    with pytest.raises(PyXSDError, match="root-ambiguous"):
        schema.from_data({})
    with pytest.raises(PyXSDError, match="root-ambiguous"):
        schema.from_data({"root": "", "other": ""})


def test_an_invalid_root_eqname_is_a_name_error():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"Q{urn:example}": ""})


@pytest.mark.parametrize(
    "element",
    ["root", "{urn:example}root", "Q{urn:example}root"],
)
def test_element_selection_accepts_local_clark_and_eqname_spellings(element):
    imported = _import(
        NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
        {"Q{urn:example}root": [{"Q{urn:example}child": "v"}]},
        element=element,
    )

    imported.require_valid()
    assert imported.root is not None


def test_a_contradicting_element_selection_is_a_root_mismatch():
    schema = _schema(NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="root-mismatch"):
        schema.from_data({"Q{urn:example}root": []}, element="child")
    with pytest.raises(PyXSDError, match="root-mismatch"):
        schema.from_data({"Q{urn:example}root": []}, element="{urn:other}root")
    with pytest.raises(PyXSDError, match="root-mismatch"):
        schema.from_data({"Q{urn:example}root": []}, element="missing")


def test_an_element_head_accepts_its_substitution_members_as_roots():
    imported = _import(SUBSTITUTION_SCHEMA, {"special": "2"}, element="item")

    imported.require_valid()
    assert imported.root is not None
    assert imported.root._name_ == "special"


def test_an_element_head_accepts_transitive_substitution_members():
    imported = _import(SUBSTITUTION_SCHEMA, {"extra": "3"}, element="item")

    imported.require_valid()
    assert imported.root is not None
    assert imported.root._name_ == "extra"


def test_an_ambiguous_local_element_selection_is_a_name_error(tmp_path):
    (tmp_path / "b.xsd").write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:b" elementFormDefault="qualified">
  <xs:element name="root" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:a" elementFormDefault="qualified">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:element name="root" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    schema = pyxsd.Schema.compile(str(main))

    with pytest.raises(PyXSDError, match="name-ambiguous"):
        schema.from_data({"Q{urn:a}root": "x"}, element="root")


def test_an_unknown_root_reaches_the_binder_unknown_root_report():
    schema = _schema(EMPTY_SCHEMA)

    document = schema.from_data({"nope": ""})

    assert document.root is None
    assert document.report.has_errors
    assert any(error.code == "unknown-root" for error in document.report.errors)


# ---------------------------------------------------------------------------
# xsi:type markers
# ---------------------------------------------------------------------------


def test_an_explicit_xsi_type_marker_selects_the_actual_layout():
    imported = _import(
        XSITYPE_SCHEMA,
        {
            "Q{urn:example}root": [
                {"@xsi:type": XSI_TYPE_EQNAME},
                {"Q{urn:example}a": "x"},
                {"Q{urn:example}b": 2},
            ]
        },
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": [
            {"@xsi:type": XSI_TYPE_EQNAME},
            {"Q{urn:example}a": "x"},
            {"Q{urn:example}b": 2},
        ]
    }


def test_an_xsi_type_equal_to_the_declared_type_stays_omitted():
    imported = _import(XSITYPE_EQUAL_SCHEMA, {"Q{urn:example}root": [{"Q{urn:example}a": "x"}]})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": [{"Q{urn:example}a": "x"}]
    }


def test_an_xsi_type_on_a_simple_root_selects_simple_plus():
    imported = _import(
        XSITYPE_SIMPLE_SCHEMA,
        {"root": {"@xsi:type": XSI_INT_EQNAME, "#content": 5}},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "root": {"@xsi:type": XSI_INT_EQNAME, "#content": 5}
    }


def test_an_xsi_type_on_a_child_is_kept_in_its_child_map():
    imported = _import(
        XSITYPE_CHILD_SCHEMA,
        {"root": [{"value": {"@xsi:type": XSI_INT_EQNAME, "#content": 5}}]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "root": [{"value": {"@xsi:type": XSI_INT_EQNAME, "#content": 5}}]
    }


def test_an_xsi_type_promotes_a_list_to_list_plus():
    imported = _import(
        XSITYPE_LIST_SCHEMA,
        {
            "Q{urn:example}root": {
                "@xsi:type": "Q{urn:example}ListType",
                "Q{urn:example}date": ["2026-01-01"],
            }
        },
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": {
            "@xsi:type": "Q{urn:example}ListType",
            "Q{urn:example}date": ["2026-01-01"],
        }
    }


def test_an_xsi_type_keeps_a_record_a_record():
    imported = _import(
        XSITYPE_RECORD_SCHEMA,
        {"Q{urn:example}root": {"@xsi:type": "Q{urn:example}RecordType", "Q{urn:example}a": "A"}},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": {"@xsi:type": "Q{urn:example}RecordType", "Q{urn:example}a": "A"}
    }


def test_an_xsi_type_promotes_an_empty_type_to_empty_plus():
    empty_type_schema = XSITYPE_RECORD_SCHEMA.replace(
        '<xs:all>\n      <xs:element name="a" type="xs:string"/>\n    </xs:all>', ""
    )
    assert "xs:all" not in empty_type_schema  # the restriction really applied
    imported = _import(
        empty_type_schema,
        {"Q{urn:example}root": {"@xsi:type": "Q{urn:example}RecordType"}},
    )

    assert imported.root is not None
    assert imported.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": {"@xsi:type": "Q{urn:example}RecordType"}
    }


def test_an_unnamespaced_xsi_type_uses_the_local_eqname():
    imported = _import(UNNAMESPACED_XSITYPE_SCHEMA, {"root": [{"@xsi:type": "T"}, {"a": "A"}]})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"@xsi:type": "T"}, {"a": "A"}]}


def test_an_unresolvable_xsi_type_reaches_the_binder_report():
    imported = _import(
        WILDCARD_GENERIC_SCHEMA,
        {"root": [{"@xsi:type": "Q{urn:missing}Nope"}, {"other": ["v"]}]},
    )

    assert imported.root is not None
    assert any(error.code == "xsi-type" for error in imported.report.errors)
    with pytest.raises(ValidationError):
        imported.require_valid()


@pytest.mark.parametrize("value", [5, True, None, ["T"]])
def test_a_non_string_xsi_type_marker_is_a_scalar_type_error(value):
    schema = _schema(UNNAMESPACED_XSITYPE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"root": {"@xsi:type": value}})


@pytest.mark.parametrize("value", ["Q{urn:example", "Q{}", "bad name", "p:T"])
def test_a_malformed_xsi_type_eqname_is_a_name_error(value):
    schema = _schema(UNNAMESPACED_XSITYPE_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"root": {"@xsi:type": value}})


# ---------------------------------------------------------------------------
# Conditional type assignment
# ---------------------------------------------------------------------------


def test_conditional_type_assignment_selects_the_alternative_during_binding():
    imported = _import(
        CONDITIONAL_SCHEMA,
        {"root": [{"@kind": "list"}, {"date": "2026-01-01"}]},
    )

    imported.require_valid()
    assert type(imported.root).__name__ == "ListType"
    assert imported.to_data(format="pyxsd-json") == {
        "root": {"@kind": "list", "date": ["2026-01-01"]}
    }


def test_conditional_record_types_import_through_the_undetermined_container():
    imported = _import(CONDITIONAL_SCHEMA, {"root": {"@kind": "record", "a": "x"}})

    imported.require_valid()
    assert type(imported.root).__name__ == "RecordType"
    assert imported.to_data(format="pyxsd-json") == {"root": {"@kind": "record", "a": "x"}}


def test_undetermined_roots_without_a_selector_stay_generic():
    imported = _import(CONDITIONAL_SCHEMA, {"root": {"a": "x"}})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"a": ["x"]}]}


# ---------------------------------------------------------------------------
# EQName QNames and namespace frames
# ---------------------------------------------------------------------------


def test_qname_content_translates_an_eqname_to_a_bound_lexical_qname():
    imported = _import(QNAME_ROOT_SCHEMA, {"root": "Q{urn:example}v"})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": "Q{urn:example}v"}


def test_qname_children_translate_eqnames():
    imported = _import(QNAME_CHILD_SCHEMA, {"root": [{"ref": "Q{urn:example}v"}]})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"ref": "Q{urn:example}v"}]}


def test_qname_attributes_translate_eqnames():
    imported = _import(QNAME_ATTRIBUTE_SCHEMA, {"root": {"@ref": "Q{urn:example}v"}})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": {"@ref": "Q{urn:example}v"}}


def test_qname_lists_translate_each_eqname_item():
    imported = _import(
        QNAME_LIST_SCHEMA,
        {"Q{urn:example}root": ["Q{urn:example}a", "Q{urn:other}b"]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": ["Q{urn:example}a", "Q{urn:other}b"]
    }


def test_an_unqualified_eqname_needs_no_namespace_binding():
    imported = _import(QNAME_ROOT_SCHEMA, {"root": "v"})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": "v"}


def test_caller_namespaces_become_the_root_frame_and_supply_prefixes():
    schema = _schema(QNAME_ROOT_SCHEMA)

    imported = schema.from_data({"root": "Q{urn:example}v"}, namespaces={"t": "urn:example"})

    imported.require_valid()
    assert str(imported.root) == "t:v"
    assert imported.to_data(format="pyxsd-json") == {"root": "Q{urn:example}v"}


def test_generated_prefixes_never_collide_with_caller_prefixes():
    schema = _schema(QNAME_ROOT_SCHEMA)

    imported = schema.from_data({"root": "Q{urn:example}v"}, namespaces={"ns0": "urn:other"})

    imported.require_valid()
    lexical = str(imported.root)
    assert lexical != "ns0:v"
    assert imported.to_data(format="pyxsd-json") == {"root": "Q{urn:example}v"}


def test_generated_prefixes_are_recorded_on_the_element_frames():
    schema = _schema(NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA)
    element = data_to_element(schema, {"Q{urn:example}root": [{"Q{urn:example}child": "v"}]})

    # Every element's inherited frame is recorded, even when it holds no
    # generated bindings (an unrecorded element would fall back to the
    # schema document's own root bindings instead).
    assert schema.namespace_context.bindings_for(element) == {}

    child = element.find("{urn:example}child")
    assert child is not None
    assert schema.namespace_context.bindings_for(child) == {}


def test_nested_eqname_qnames_record_bindings_on_their_own_element():
    schema = _schema(QNAME_CHILD_SCHEMA)
    element = data_to_element(schema, {"root": [{"ref": "Q{urn:example}v"}]})

    ref = element.find("ref")
    assert ref is not None
    assert ref.text is not None
    prefix = ref.text.split(":")[0]
    frame = schema.namespace_context.bindings_for(ref)
    assert frame[prefix] == "urn:example"

    root_frame = schema.namespace_context.bindings_for(element)
    assert prefix not in root_frame


def test_namespaced_elements_and_attributes_round_trip_with_eqnames():
    imported = _import(
        NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
        {"Q{urn:example}root": [{"@Q{urn:example}attr": 5}, {"Q{urn:example}child": "v"}]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": [
            {"@Q{urn:example}attr": 5},
            {"Q{urn:example}child": "v"},
        ]
    }


# ---------------------------------------------------------------------------
# Substitutions and wildcards
# ---------------------------------------------------------------------------


def test_substitution_members_keep_their_names_and_order():
    imported = _import(
        SUBSTITUTION_SCHEMA,
        {"root": [{"item": "1"}, {"special": "2"}, {"extra": "3"}]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "root": [{"item": "1"}, {"special": "2"}, {"extra": "3"}]
    }


def test_an_abstract_substitution_head_with_one_member_is_a_list():
    imported = _import(ABSTRACT_SUBSTITUTION_SCHEMA, {"root": ["2"]})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": ["2"]}


def test_namespaced_substitution_members_use_eqnames():
    imported = _import(
        NAMESPACED_SUBSTITUTION_SCHEMA,
        {"Q{urn:example}root": [{"Q{urn:example}special": "2"}]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "Q{urn:example}root": [{"Q{urn:example}special": "2"}]
    }


def test_a_wildcard_child_matching_a_global_uses_its_declaration():
    imported = _import(SEQUENCE_WILDCARD_SCHEMA, {"root": [{"known": "v"}]})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"known": "v"}]}


def test_an_unknown_wildcard_child_binds_generically():
    imported = _import(WILDCARD_GENERIC_SCHEMA, {"root": [{"other": ["v"]}]})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"other": ["v"]}]}


def test_a_namespaced_wildcard_child_keeps_its_eqname():
    namespaced_wildcard_schema = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
    imported = _import(
        namespaced_wildcard_schema,
        {"root": [{"Q{urn:runtime}child": ["v"]}]},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"Q{urn:runtime}child": ["v"]}]}


def test_wildcard_attributes_pass_through_as_lexical_strings():
    imported = _import(
        SIMPLE_CONTENT_WILDCARD_SCHEMA,
        {"root": {"@wild": "x", "#content": "text"}},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": {"@wild": "x", "#content": "text"}}


# ---------------------------------------------------------------------------
# Defaults, generated classes, XML output
# ---------------------------------------------------------------------------


def test_declared_defaults_apply_to_empty_imported_content():
    exported = _compile(DEFAULT_SCHEMA, "<root><e/></root>").to_data(format="pyxsd-json")
    assert exported == {"root": [{"e": "dflt"}]}

    imported = _import(DEFAULT_SCHEMA, exported)
    imported.require_valid()
    assert imported.root.e == "dflt"
    assert imported.to_data(format="pyxsd-json") == exported


def test_an_empty_string_means_absent_content_so_defaults_apply():
    imported = _import(DEFAULT_SCHEMA, {"root": [{"e": ""}]})

    imported.require_valid()
    assert imported.root.e == "dflt"


def test_a_default_root_value_applies_to_empty_content():
    schema = _schema(DEFAULT_ROOT_SCHEMA)

    empty = schema.from_data({"root": ""})
    assert empty.root == "fallback"

    provided = schema.from_data({"root": "given"})
    assert provided.root == "given"


def test_imported_documents_expose_the_generated_classes():
    imported = _import(
        SEQUENCE_ATTRIBUTE_SCHEMA,
        {"root": [{"@id": "r"}, {"a": "A"}, {"b": "B"}]},
    )

    assert type(imported.root).__name__ == "root|complexType"
    assert imported.root.id == "r"
    assert imported.root.a == "A"
    assert imported.root.b == "B"


def test_typed_attribute_values_are_converted():
    imported = _import(
        SIMPLE_CONTENT_INT_ATTRIBUTE_SCHEMA,
        {"root": {"@id": 7, "#content": "text"}},
    )

    imported.require_valid()
    assert int(imported.root.id) == 7
    assert imported.to_data(format="pyxsd-json") == {"root": {"@id": 7, "#content": "text"}}


def test_imported_documents_serialize_to_namespaced_xml():
    imported = _import(
        NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
        {"Q{urn:example}root": [{"@Q{urn:example}attr": 5}, {"Q{urn:example}child": "v"}]},
    )

    text = imported.to_string()
    assert 'xmlns:ns0 = "urn:example"' in text

    reparsed = imported.schema.parse(StringIO(text))
    reparsed.require_valid()
    assert reparsed.to_data(format="pyxsd-json") == imported.to_data(format="pyxsd-json")


def test_imported_documents_revalidate_to_the_same_data():
    imported = _import(
        REPEATED_ATTRIBUTE_SCHEMA,
        {"dates": {"@source": "known", "date": ["2026-01-01", "2026-02-01"]}},
    )

    again = imported.revalidate()
    again.require_valid()
    assert again.to_data(format="pyxsd-json") == {
        "dates": {"@source": "known", "date": ["2026-01-01", "2026-02-01"]}
    }


def test_imported_documents_support_transforms():
    from pyxsd.transforms import ToData, ToDict

    imported = _import(RECORD_SCHEMA, {"root": {"@id": "r", "a": "A"}})

    assert imported.transform(ToData(format="pyxsd-json")) == {"root": {"@id": "r", "a": "A"}}
    assert imported.transform(ToDict()) == {"@id": "r", "a": "A"}


# ---------------------------------------------------------------------------
# Conversion errors: wrong containers and scalar types
# ---------------------------------------------------------------------------


def test_a_simple_layout_has_no_attribute_container():
    schema = _schema(FIXTURES["simple"][0])

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"simple": {"@a": 1}})


def test_a_simple_layout_has_no_array_container():
    schema = _schema(FIXTURES["simple"][0])

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data({"simple": [42]})
    assert "/simple" in str(caught.value)


def test_an_empty_layout_rejects_attributes_text_and_children():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": {"@a": 1}})
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": "text"})
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": []})


def test_an_empty_plus_layout_rejects_arrays_and_reports_unknown_children():
    schema = _schema(EMPTY_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": []})

    imported = schema.from_data({"root": {"@id": "x", "child": "v"}})
    assert imported.root is not None
    assert imported.report.has_errors


def test_a_list_layout_has_no_attribute_map():
    schema = _schema(REPEATED_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"dates": {"@source": "known", "date": []}})


def test_a_list_plus_requires_its_child_array():
    schema = _schema(REPEATED_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"dates": {"@source": "known"}})
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"dates": {"date": "2026-01-01"}})


def test_a_record_child_requires_its_scalar_container():
    schema = _schema(RECORD_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data({"root": {"a": ["A"]}})
    assert "/root/a" in str(caught.value)


def test_a_sequence_child_map_has_exactly_one_entry():
    schema = _schema(SEQUENCE_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": [{"a": "A", "b": "B"}]})
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": [{}]})


def test_attribute_maps_must_precede_sequence_content():
    schema = _schema(SEQUENCE_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": [{"a": "A"}, {"@id": "x"}]})


def test_a_mixed_item_must_be_a_string_or_a_singleton_child_map():
    schema = _schema(MIXED_NO_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": [42]})


# ---------------------------------------------------------------------------
# Conversion errors: marker conflicts
# ---------------------------------------------------------------------------


def test_a_nil_record_cannot_carry_children():
    schema = _schema(RECORD_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": {"#content": None, "a": "A"}})


def test_a_non_null_content_marker_in_a_record_is_a_conflict():
    schema = _schema(RECORD_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": {"a": "A", "#content": "text"}})


def test_a_nil_list_plus_cannot_carry_its_child_array():
    schema = _schema(REPEATED_NILABLE_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"dates": {"#content": None, "date": []}})


@pytest.mark.parametrize(
    "data",
    [
        {"root": [{"#content": None}, {"a": "A"}]},
        {"root": [{"#content": None}, {"@id": "x"}]},
        {"root": [{"#content": None}, "tail"]},
    ],
)
def test_nothing_may_follow_a_nil_marker_in_a_sequence(data):
    schema = _schema(SEQUENCE_NILABLE_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data(data)


def test_a_non_null_content_marker_in_a_sequence_is_a_conflict():
    schema = _schema(SEQUENCE_OPTIONAL_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": [{"#content": "text"}]})


def test_repeated_attribute_keys_across_maps_conflict():
    schema = _schema(SEQUENCE_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": [{"@id": "1"}, {"@id": "2"}]})


# ---------------------------------------------------------------------------
# Conversion errors: scalar types with nested JSON pointers
# ---------------------------------------------------------------------------


def test_a_boolean_is_not_an_integer_at_the_root():
    schema = _schema(FIXTURES["simple"][0])

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data({"simple": True})
    assert "/simple" in str(caught.value)


def test_a_nested_scalar_error_carries_its_nested_json_pointer():
    schema = _schema(REPEATED_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data({"dates": ["2026-01-01", 5]})
    assert str(caught.value).startswith("scalar-type-invalid at '/dates/1'")


def test_a_deeply_nested_scalar_error_carries_the_full_pointer():
    imported_input: JsonValue = {
        "Q{urn:example}root": [
            {"@xsi:type": XSI_TYPE_EQNAME},
            {"Q{urn:example}a": "x"},
            {"Q{urn:example}b": True},
        ]
    }
    schema = _schema(XSITYPE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data(imported_input)
    assert "/Q{urn:example}root/2/Q{urn:example}b" in str(caught.value)


def test_an_attribute_scalar_error_carries_the_attribute_pointer():
    schema = _schema(SIMPLE_CONTENT_INT_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data({"root": {"@id": "x", "#content": "text"}})
    assert "/root/@id" in str(caught.value)


def test_an_undeclared_attribute_value_must_be_a_string():
    schema = _schema(SIMPLE_CONTENT_WILDCARD_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data({"root": {"@wild": 5, "#content": "text"}})
    assert "/root/@wild" in str(caught.value)


def test_a_record_child_scalar_error_carries_the_child_pointer():
    schema = _schema(RECORD_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data({"root": {"a": 5}})
    assert "/root/a" in str(caught.value)


def test_untyped_wildcard_text_must_be_a_string():
    schema = _schema(WILDCARD_GENERIC_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"root": [{"other": 5}]})
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": [{"other": [5]}]})


# ---------------------------------------------------------------------------
# Format and option validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish", "jsonml"])
def test_other_recognized_formats_are_not_imported_yet(format):
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="schema-shape-unsupported"):
        schema.from_data({"root": ""}, format=format)


def test_unknown_formats_and_fidelities_raise_value_errors():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(ValueError, match="unknown data format"):
        schema.from_data({"root": ""}, format="yaml")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown fidelity mode"):
        schema.from_data({"root": ""}, fidelity="lossy")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown data format"):
        schema.from_json('{"root": ""}', format="yaml")  # type: ignore[arg-type]


def test_from_json_validates_the_format_before_parsing_the_json():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(ValueError, match="unknown data format"):
        schema.from_json('{"root": ', format="yaml")  # type: ignore[arg-type]


def test_the_hybrid_importer_rejects_typed_and_always_list_options():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(TypeError):
        schema.from_data({"root": ""}, typed=False)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        schema.from_data({"root": ""}, always_list=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Coverage of remaining defensive and generic branches
# ---------------------------------------------------------------------------

EXTENDED_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="Base">
    <xs:attribute name="id" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="Derived">
    <xs:complexContent>
      <xs:extension base="Base">
        <xs:attribute name="id" type="xs:int"/>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="root" type="Derived"/>
</xs:schema>"""


def test_a_derived_attribute_redeclaration_shadows_its_base():
    imported = _import(EXTENDED_ATTRIBUTE_SCHEMA, {"root": {"@id": 7}})

    imported.require_valid()
    # The derived declaration (xs:int) governs the value.
    assert int(imported.root.id) == 7
    assert imported.to_data(format="pyxsd-json") == {"root": {"@id": 7}}


def test_a_clark_element_with_invalid_syntax_is_a_name_error():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"root": ""}, element="{urn:example}bad name")


def test_sibling_eqname_qnames_reuse_one_generated_prefix():
    sibling_schema = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="first" type="xs:QName"/>
        <xs:element name="second" type="xs:QName"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
    schema = _schema(sibling_schema)
    element = data_to_element(
        schema, {"root": [{"first": "Q{urn:example}a"}, {"second": "Q{urn:example}b"}]}
    )

    first = element.find("first")
    second = element.find("second")
    assert first is not None and second is not None
    assert first.text is not None and second.text is not None
    assert first.text.split(":")[0] == second.text.split(":")[0]

    imported = schema.from_data(
        {"root": [{"first": "Q{urn:example}a"}, {"second": "Q{urn:example}b"}]}
    )
    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {
        "root": [{"first": "Q{urn:example}a"}, {"second": "Q{urn:example}b"}]
    }


def test_a_qname_list_with_a_plain_string_is_a_scalar_type_error():
    schema = _schema(QNAME_LIST_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"Q{urn:example}root": "plain"})


def test_a_simple_plus_value_must_be_an_object():
    schema = _schema(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": ["text"]})


def test_a_simple_plus_nil_marker_conflicts_with_content_properties():
    nillable = SIMPLE_CONTENT_ATTRIBUTE_SCHEMA.replace('name="root"', 'name="root" nillable="true"')
    schema = _schema(nillable)

    nilled = schema.from_data({"root": {"@id": "r", "#content": None}})
    nilled.require_valid()
    assert nilled.to_data(format="pyxsd-json") == {"root": {"@id": "r", "#content": None}}

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": {"unknown": "v", "#content": None}})
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": {"#content": None, "unknown": "v"}})


def test_a_simple_plus_unknown_child_reaches_the_binder():
    imported = _import(
        SIMPLE_CONTENT_ATTRIBUTE_SCHEMA,
        {"root": {"@id": "r", "#content": "text", "extra": "v"}},
    )

    assert imported.root is not None
    assert any(error.code == "unexpected-element" for error in imported.report.errors)


def test_a_list_plus_value_must_be_an_object():
    schema = _schema(REPEATED_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"dates": ["2026-01-01"]})


def test_list_plus_marker_conflicts():
    schema = _schema(REPEATED_NILABLE_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"dates": {"#content": "text"}})
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"dates": {"date": [], "#content": None}})
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"dates": {"#content": None, "unknown": "v"}})


def test_a_list_plus_unknown_child_reaches_the_binder():
    imported = _import(
        REPEATED_ATTRIBUTE_SCHEMA,
        {"dates": {"@source": "known", "date": ["2026-01-01"], "unknown": "v"}},
    )

    assert imported.root is not None
    assert imported.report.has_errors


EMPTY_PLUS_NIL_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:complexType name="EmptyType">
    <xs:attribute name="id" type="xs:string"/>
  </xs:complexType>
  <xs:element name="root" type="t:EmptyType" nillable="true"/>
</xs:schema>"""


def test_an_empty_plus_marker_conflicts():
    schema = _schema(EMPTY_PLUS_NIL_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"Q{urn:example}root": {"#content": "text"}})
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"Q{urn:example}root": {"unknown": "v", "#content": None}})
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"Q{urn:example}root": {"#content": None, "unknown": "v"}})


def test_a_record_value_must_be_an_object():
    schema = _schema(RECORD_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": []})


def test_a_record_nil_marker_after_content_conflicts():
    schema = _schema(RECORD_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": {"a": "A", "#content": None}})


def test_an_ordered_value_must_be_an_array():
    schema = _schema(SEQUENCE_OPTIONAL_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": "text"})


def test_an_ordered_nil_marker_after_content_conflicts():
    schema = _schema(SEQUENCE_NILABLE_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": [{"a": "A"}, {"#content": None}]})


def test_a_duplicate_xsi_type_marker_across_maps_conflicts():
    schema = _schema(UNNAMESPACED_XSITYPE_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": [{"@xsi:type": "T"}, {"@xsi:type": "T"}]})
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data(
            {"root": [{"@xsi:type": "Q{urn:missing}Nope"}, {"@xsi:type": "Q{urn:missing}Nope"}]}
        )


ANY_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:anyType"/>
</xs:schema>"""


def test_a_generic_null_child_keeps_its_nil_through_binding():
    imported = _import(WILDCARD_GENERIC_SCHEMA, {"root": [{"other": None}]})

    imported.require_valid()
    # A generic wildcard child admits the nil marker; the round trip
    # keeps it inside the generic child's mixed array.
    assert imported.to_data(format="pyxsd-json") == {"root": [{"other": [{"#content": None}]}]}


def test_generic_object_content_markers():
    schema = _schema(ANY_ROOT_SCHEMA)

    text = schema.from_data({"root": {"#content": "text"}})
    text.require_valid()
    assert text.to_data(format="pyxsd-json") == {"root": ["text"]}

    nilled = schema.from_data({"root": {"#content": None}})
    assert nilled.report.has_errors

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": {"child": "v", "#content": None}})
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": {"#content": None, "child": "v"}})
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"root": {"#content": 5}})


def test_nothing_may_follow_a_nil_marker_in_generic_content():
    schema = _schema(ANY_ROOT_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": [{"#content": None}, {"child": "v"}]})


def test_generic_array_items_follow_the_mixed_contract():
    schema = _schema(ANY_ROOT_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": [42]})
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": [{"child": "v"}, {"@id": "x"}]})
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": [{"child": "v", "other": "w"}]})
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": [{"child": "v"}, {"#content": None}]})
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"root": [{"#content": "text"}]})

    merged = schema.from_data({"root": ["a", "", "b", {"child": "v"}, "tail"]})
    merged.require_valid()
    assert merged.to_data(format="pyxsd-json") == {"root": ["ab", {"child": ["v"]}, "tail"]}


def test_a_generic_object_with_an_unresolvable_marker_decodes_generically():
    imported = _import(
        ANY_ROOT_SCHEMA,
        {"root": {"@xsi:type": "Q{urn:missing}Nope", "other": ["v"]}},
    )

    assert imported.root is not None
    assert any(error.code == "xsi-type" for error in imported.report.errors)


def test_generic_array_nil_marker_terminates_content():
    nillable_any_schema = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:anyType" nillable="true"/>
</xs:schema>"""
    imported = _import(nillable_any_schema, {"root": [{"#content": None}]})

    imported.require_valid()
    assert imported.to_data(format="pyxsd-json") == {"root": [{"#content": None}]}


# ---------------------------------------------------------------------------
# Validation stays on the document report
# ---------------------------------------------------------------------------


def test_a_lexically_invalid_list_item_lands_on_the_report():
    imported = _import(REPEATED_SCHEMA, {"dates": ["2026-01-01", "not-a-date"]})

    assert imported.root is not None
    # The binder drops the invalid item and records the issue.
    assert len(imported.root.date) == 1
    assert imported.report.has_errors
    with pytest.raises(ValidationError):
        imported.require_valid()


def test_an_unknown_record_child_lands_on_the_report():
    imported = _import(RECORD_SCHEMA, {"root": {"a": "A", "zz": "v"}})

    assert imported.root is not None
    assert imported.report.has_errors
    assert any(error.code == "unexpected-element" for error in imported.report.errors)


def test_an_undeclared_attribute_lands_on_the_report():
    imported = _import(
        SEQUENCE_ATTRIBUTE_SCHEMA, {"root": [{"@nope": "x"}, {"a": "A"}, {"b": "B"}]}
    )

    assert imported.root is not None
    assert imported.report.has_errors
    assert any(error.code == "unexpected-attribute" for error in imported.report.errors)


def test_a_non_nillable_element_with_the_null_marker_lands_on_the_report():
    imported = _import(SEQUENCE_OPTIONAL_SCHEMA, {"root": None})

    assert imported.root is not None
    assert imported.report.has_errors
    assert any(error.code == "nil" for error in imported.report.errors)


def test_conversion_errors_never_produce_a_document():
    schema = _schema(REPEATED_SCHEMA)

    with pytest.raises(PyXSDError):
        schema.from_data({"dates": ["2026-01-01", 5]})
    with pytest.raises(PyXSDError):
        schema.from_json('{"dates": ["2026-01-01", 5]}')
