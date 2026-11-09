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
import warnings
from io import StringIO
from pathlib import Path

import pytest
from tests.test_data_export import (
    ABSTRACT_SUBSTITUTION_SCHEMA,
    ANYTYPE_ROOT_SCHEMA,
    BOOLEAN_ROOT_SCHEMA,
    DECIMAL_ROOT_SCHEMA,
    EMPTY_ATTRIBUTE_SCHEMA,
    EMPTY_SCHEMA,
    MIXED_NILABLE_SCHEMA,
    MIXED_NO_ATTRIBUTE_SCHEMA,
    MIXED_TWO_CHILD_SCHEMA,
    NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
    NAMESPACED_SIMPLE_ROOT_SCHEMA,
    NAMESPACED_SUBSTITUTION_SCHEMA,
    NATIVE_INSTANCE,
    NATIVE_SCHEMA,
    PARKER_LOCAL_COLLISION_SCHEMA,
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
    SIMPLE_CONTENT_DECLARED_WILDCARD_SCHEMA,
    SIMPLE_CONTENT_INT_ATTRIBUTE_SCHEMA,
    SIMPLE_CONTENT_NILABLE_SCHEMA,
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
from pyxsd.data_conversion.contracts import JsonValue, json_pointer
from pyxsd.data_conversion.import_data import data_to_element, json_to_data
from pyxsd.exceptions import PyXSDError, PyXSDWarning, ValidationError
from pyxsd.xsi import XSI_NAMESPACE, XSI_NIL, XSI_TYPE

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


NAMESPACED_QNAME_ROOT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:element name="ref" type="xs:QName"/>
</xs:schema>"""

NAMESPACED_QNAME_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:simpleType name="QNameList">
    <xs:list itemType="xs:QName"/>
  </xs:simpleType>
  <xs:element name="holder">
    <xs:complexType>
      <xs:attribute name="qattr" type="xs:QName"/>
      <xs:attribute name="qlist" type="t:QNameList"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""


def test_imported_qname_values_keep_their_namespace_in_xml_output():
    # The importer binds the generated prefix ns0 to the value's
    # namespace while the writer assigns ns0 to the element namespace;
    # the serialized value must not silently rebind.
    schema = _schema(NAMESPACED_QNAME_ROOT_SCHEMA)
    imported = schema.from_data({"Q{urn:example}ref": "Q{urn:values}item"})

    imported.require_valid()
    assert imported.root.to_expanded_name() == "{urn:values}item"

    reparsed = schema.parse(StringIO(imported.to_string()))
    reparsed.require_valid()
    assert reparsed.root.to_expanded_name() == "{urn:values}item"


def test_imported_qname_attributes_keep_their_namespace_in_xml_output():
    schema = _schema(NAMESPACED_QNAME_ATTRIBUTE_SCHEMA)
    imported = schema.from_data(
        {
            "Q{urn:example}holder": {
                "@qattr": "Q{urn:values}att",
                "@qlist": ["Q{urn:values}a", "Q{urn:other}b"],
            }
        }
    )

    imported.require_valid()
    assert imported.root.qattr.to_expanded_name() == "{urn:values}att"
    assert [item.to_expanded_name() for item in imported.root.qlist] == [
        "{urn:values}a",
        "{urn:other}b",
    ]

    reparsed = schema.parse(StringIO(imported.to_string()))
    reparsed.require_valid()
    assert reparsed.root.qattr.to_expanded_name() == "{urn:values}att"
    assert [item.to_expanded_name() for item in reparsed.root.qlist] == [
        "{urn:values}a",
        "{urn:other}b",
    ]


def test_imported_qname_lists_keep_their_namespaces_in_xml_output():
    schema = _schema(QNAME_LIST_SCHEMA)
    imported = schema.from_data({"Q{urn:example}root": ["Q{urn:values}a", "Q{urn:other}b"]})

    imported.require_valid()
    expected = ["{urn:values}a", "{urn:other}b"]
    assert [item.to_expanded_name() for item in imported.root] == expected

    reparsed = schema.parse(StringIO(imported.to_string()))
    reparsed.require_valid()
    assert [item.to_expanded_name() for item in reparsed.root] == expected


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


@pytest.mark.parametrize(
    "keys",
    [
        ("a", "Q{}a"),
        ("Q{}a", "a"),
    ],
)
def test_two_record_keys_denoting_one_expanded_name_are_rejected(keys):
    schema = _schema(RECORD_SCHEMA)

    with pytest.raises(PyXSDError, match="json-duplicate-key") as caught:
        schema.from_data({"root": {keys[0]: "A", keys[1]: "B"}})
    assert "/root" in str(caught.value)


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


def test_a_float_is_not_an_integer_at_the_root():
    schema = _schema(FIXTURES["simple"][0])

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data({"simple": 1.0})
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


# ---------------------------------------------------------------------------
# Conventional format round trips
# ---------------------------------------------------------------------------

#: Export fidelity that can represent each shared fixture in each format.
ROUND_TRIP_EXPORT_FIDELITY = {
    ("pyxsd", "mixed"): "best-effort",
    ("parker", "attribute"): "best-effort",
    ("parker", "mixed"): "best-effort",
    ("parker", "namespaced"): "best-effort",
    ("parker", "recursive"): "best-effort",
    ("badgerfish", "mixed"): "best-effort",
}

#: Import fidelity needed to reconstruct the exported representation.
ROUND_TRIP_IMPORT_FIDELITY = {
    ("pyxsd", "mixed"): "best-effort",
    ("parker", "mixed"): "best-effort",
    ("badgerfish", "mixed"): "best-effort",
}


def _round_trip(
    format: str,
    name: str,
    *,
    typed: bool = True,
) -> tuple[JsonValue, pyxsd.Document, JsonValue]:
    """Export one shared fixture, import it, and export it again."""
    schema_source, instance_source = FIXTURES[name]
    document = _compile(schema_source, instance_source)
    export_fidelity = ROUND_TRIP_EXPORT_FIDELITY.get((format, name), "strict")
    import_fidelity = ROUND_TRIP_IMPORT_FIDELITY.get((format, name), "strict")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PyXSDWarning)
        exported = document.to_data(format=format, fidelity=export_fidelity, typed=typed)
        imported = document.schema.from_data(exported, format=format, fidelity=import_fidelity)
        again = imported.to_data(format=format, fidelity=export_fidelity, typed=typed)
    return exported, imported, again


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish", "jsonml"])
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_each_shared_fixture_round_trips_through_its_conventional_format(format, name):
    exported, imported, again = _round_trip(format, name)

    assert again == exported
    if format == "parker" and name == "attribute":
        # Parker cannot carry the required attribute; the loss is a
        # normal validation issue rather than a conversion error.
        assert imported.root is not None
        assert imported.report.has_errors
    else:
        imported.require_valid()


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_native_typed_false_lexical_input_round_trips(name):
    exported, imported, again = _round_trip("pyxsd", name, typed=False)

    assert again == exported
    imported.require_valid()


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish", "jsonml"])
def test_conventional_imports_reach_the_generated_classes(format):
    document = _compile(FIXTURES["recursive"][0], FIXTURES["recursive"][1])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PyXSDWarning)
        exported = document.to_data(format=format, fidelity="best-effort")
        imported = document.schema.from_data(exported, format=format, fidelity="best-effort")

    imported.require_valid()
    assert type(imported.root).__name__ == "node|complexType"
    assert imported.root._name_ == "node"
    assert imported.root._descriptor_ is not None


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish", "jsonml"])
def test_from_json_matches_from_data_for_conventional_formats(format):
    document = _compile(FIXTURES["ordered"][0], FIXTURES["ordered"][1])
    exported = document.to_data(format=format, fidelity="strict")

    from_data = document.schema.from_data(exported, format=format)
    from_json = document.schema.from_json(json.dumps(exported), format=format)

    assert from_json.to_data(format=format, fidelity="strict") == from_data.to_data(
        format=format, fidelity="strict"
    )
    from_json.require_valid()


# ---------------------------------------------------------------------------
# Native import
# ---------------------------------------------------------------------------


def test_native_import_reads_grouped_children_and_attributes():
    imported = _import(
        NATIVE_SCHEMA,
        {"@a": 1, "item": [1, 2], "note": "hi"},
        format="pyxsd",
    )

    imported.require_valid()
    assert [int(item) for item in imported.root.item] == [1, 2]
    assert imported.root.note == "hi"
    assert int(imported.root.a) == 1
    assert imported.to_data(format="pyxsd") == {"@a": 1, "item": [1, 2], "note": "hi"}


def test_native_import_accepts_always_list_output():
    imported = _import(
        NATIVE_SCHEMA,
        {"@a": 1, "item": [1, 2], "note": ["hi"]},
        format="pyxsd",
    )

    imported.require_valid()
    assert imported.root.note == "hi"
    assert imported.to_data(format="pyxsd", always_list=True) == {
        "@a": 1,
        "item": [1, 2],
        "note": ["hi"],
    }


def test_native_import_reads_a_primitive_root_under_dollar():
    imported = _import(FIXTURES["simple"][0], {"$": 42}, format="pyxsd")

    imported.require_valid()
    assert imported.root == 42
    assert imported.to_data(format="pyxsd") == {"$": 42}


def test_native_import_accepts_a_bare_scalar_root():
    imported = _import(FIXTURES["simple"][0], 42, format="pyxsd")

    imported.require_valid()
    assert imported.root == 42


def test_native_import_reads_lexical_scalars_from_typed_false_output():
    integer = _import(FIXTURES["simple"][0], {"$": "42"}, format="pyxsd")
    assert integer.root == 42

    boolean = _import(BOOLEAN_ROOT_SCHEMA, {"$": "true"}, format="pyxsd")
    assert bool(boolean.root) is True

    decimal = _import(DECIMAL_ROOT_SCHEMA, {"$": "1.20"}, format="pyxsd")
    decimal.require_valid()
    assert str(decimal.root) == "1.20"
    assert decimal.to_data(format="pyxsd") == {"$": "1.20"}


def test_native_import_reads_a_declared_typed_attribute():
    imported = _import(
        NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
        {
            "@{urn:example}attr": 5,
            "{urn:example}child": "v",
        },
        format="pyxsd",
    )

    imported.require_valid()
    assert int(imported.root.attr) == 5
    # Native exports a namespaced attribute through its raw lexical
    # storage key, so the value stays lexical just as it does for XML.
    assert imported.to_data(format="pyxsd") == {
        "@{urn:example}attr": "5",
        "{urn:example}child": "v",
    }


def test_native_local_names_resolve_through_one_schema_declaration():
    imported = _import(
        FIXTURES["namespaced"][0],
        {"@attr": "a", "child": "v"},
        format="pyxsd",
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd") == {
        "@attr": "a",
        "{urn:example}child": "v",
    }


def test_native_empty_content_is_a_mapping_and_null_is_empty_when_not_nillable():
    empty = _import(EMPTY_SCHEMA, {}, format="pyxsd")
    empty.require_valid()
    assert empty.to_data(format="pyxsd") == {"$": None}

    nulled = _import(EMPTY_SCHEMA, None, format="pyxsd")
    nulled.require_valid()
    assert nulled.to_data(format="pyxsd") == {"$": None}


def test_native_null_selects_nil_when_empty_content_is_invalid():
    nillable_integer_schema = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:int" nillable="true"/>
</xs:schema>"""
    imported = _import(nillable_integer_schema, None, format="pyxsd")

    imported.require_valid()
    assert imported.root is not None
    assert imported.to_data(format="pyxsd") == {"@xsi:nil": "true"}


NATIVE_NULLABLE_OR_EMPTY_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="nullableOrEmpty" nillable="true">
    <xs:complexType/>
  </xs:element>
</xs:schema>"""


def test_native_null_is_ambiguous_when_empty_and_nil_are_both_valid():
    schema = _schema(NATIVE_NULLABLE_OR_EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="empty-nil-ambiguous"):
        schema.from_data(None, format="pyxsd", element="nullableOrEmpty", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="empty-nil-ambiguous"):
        imported = schema.from_data(
            None, format="pyxsd", element="nullableOrEmpty", fidelity="best-effort"
        )

    imported.require_valid()
    assert imported.to_data(format="pyxsd") == {"@xsi:nil": "true"}


@pytest.mark.parametrize("value", [{"@xsi:nil": "true"}, {"@xsi:nil": True}])
def test_native_reads_a_nil_marker_with_attributes(value):
    schema = NATIVE_NULLABLE_OR_EMPTY_SCHEMA.replace(
        "<xs:complexType/>", '<xs:complexType><xs:attribute name="id"/></xs:complexType>'
    )
    imported = _import(schema, {**value, "@id": "r"}, format="pyxsd")

    imported.require_valid()
    assert imported.to_data(format="pyxsd") == {"@xsi:nil": "true", "@id": "r"}


def test_native_nil_marker_conflicts_with_text_and_children():
    schema = _schema(NATIVE_NULLABLE_OR_EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"@xsi:nil": "true", "$": "x"}, format="pyxsd")
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data({"@xsi:nil": "true", "child": "x"}, format="pyxsd")


def test_native_actual_repeats_expand_arrays_in_schema_order():
    imported = _import(
        SEQUENCE_REPEATED_CHOICE_SCHEMA,
        {"a": [1, 2]},
        format="pyxsd",
    )

    imported.require_valid()
    assert [name for name in ("a", "b") if name in [c._name_ for c in imported.root._children_]]
    assert [c._name_ for c in imported.root._children_] == ["a", "a"]
    assert imported.to_data(format="pyxsd") == {"a": [1, 2]}


def test_native_reconstructs_a_fixed_sequence_in_schema_order():
    imported = _import(
        SEQUENCE_ATTRIBUTE_SCHEMA,
        {"id": "ignored-as-child", "b": "B", "a": "A"},
        format="pyxsd",
    )

    # ``id`` is intentionally not an attribute here: it has no ``@`` and
    # therefore remains unknown content reaching the binder.
    assert imported.root is not None
    assert [child._name_ for child in imported.root._children_][:2] == ["a", "b"]
    assert imported.to_data(format="pyxsd") == {"a": "A", "b": "B"}
    assert imported.report.has_errors


def test_native_unknown_root_selection_reaches_the_binder():
    schema = _schema(EMPTY_SCHEMA)

    imported = schema.from_data({}, format="pyxsd", element="missing")

    assert imported.root is None
    assert any(error.code == "unknown-root" for error in imported.report.errors)


# ---------------------------------------------------------------------------
# Parker import
# ---------------------------------------------------------------------------


def test_parker_import_absorbs_the_root_and_reads_a_scalar():
    imported = _import(FIXTURES["simple"][0], 42, format="parker")

    imported.require_valid()
    assert imported.root == 42
    assert imported.to_data(format="parker", fidelity="strict") == 42


def test_parker_empty_object_is_empty_content_and_null_is_nil():
    empty = _import(EMPTY_SCHEMA, {}, format="parker")
    empty.require_valid()
    assert empty.to_data(format="parker", fidelity="strict") == {}

    nil_schema = NATIVE_NULLABLE_OR_EMPTY_SCHEMA
    nilled = _import(nil_schema, None, format="parker")
    nilled.require_valid()
    assert nilled.to_data(format="parker", fidelity="strict") is None

    simple_empty = _import(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, "", format="parker")
    simple_empty.require_valid()
    assert simple_empty.to_data(format="parker", fidelity="strict") == ""


def test_parker_reconstructs_fixed_sequence_order_from_schema():
    imported = _import(
        SEQUENCE_ATTRIBUTE_SCHEMA,
        {"b": "B", "a": "A"},
        format="parker",
    )

    imported.require_valid()
    assert [child._name_ for child in imported.root._children_] == ["a", "b"]


def test_parker_reconstructs_an_unambiguous_namespaced_local_name():
    imported = _import(FIXTURES["namespaced"][0], {"child": "v"}, format="parker")

    imported.require_valid()
    assert imported.to_data(format="parker", fidelity="strict") == {"child": "v"}
    assert imported.root._children_[0]._descriptor_.expandedInstanceName() == ("{urn:example}child")


def test_parker_ambiguous_local_names_require_best_effort():
    schema = _schema(PARKER_LOCAL_COLLISION_SCHEMA)

    with pytest.raises(PyXSDError, match="name-ambiguous") as caught:
        schema.from_data({"item": "x"}, format="parker")
    assert json_pointer(["item"]) in str(caught.value)

    with pytest.warns(PyXSDWarning, match="name-ambiguous"):
        imported = schema.from_data({"item": "x"}, format="parker", fidelity="best-effort")

    # The fallback leaves the local name unqualified, so the binder
    # selects the unqualified declaration and reports the missing
    # qualified child as a normal validation issue.
    assert imported.root is not None
    assert imported.root.item == "x"
    assert any(error.code == "occurrence-min" for error in imported.report.errors)


def test_parker_an_unresolved_local_name_reaches_the_binder_in_best_effort():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="name-ambiguous"):
        schema.from_data({"zzz": "x"}, format="parker")

    with pytest.warns(PyXSDWarning, match="name-ambiguous"):
        imported = schema.from_data({"zzz": "x"}, format="parker", fidelity="best-effort")

    assert imported.root is not None
    assert any(error.code == "unexpected-element" for error in imported.report.errors)


def test_parker_repeated_arrays_expand_at_their_property_position():
    imported = _import(
        FIXTURES["ordered"][0],
        {"note": "End", "p": ["First", "Second"], "head": "Introduction"},
        format="parker",
    )

    imported.require_valid()
    assert [child._name_ for child in imported.root._children_] == [
        "head",
        "p",
        "p",
        "note",
    ]


def test_parker_attributes_are_not_constructed_from_mapping_keys():
    schema = _schema(FIXTURES["attribute"][0])

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"@id": "a1"}, format="parker")
    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"id:x": "a1"}, format="parker")


# ---------------------------------------------------------------------------
# BadgerFish import
# ---------------------------------------------------------------------------


def test_badgerfish_import_reads_root_attributes_text_and_children():
    imported = _import(
        FIXTURES["attribute"][0],
        {"attributed": {"@id": "a1", "$": "text"}},
        format="badgerfish",
    )

    imported.require_valid()
    assert imported.root.id == "a1"
    assert str(imported.root) == "text"
    assert imported.to_data(format="badgerfish", fidelity="strict") == {
        "attributed": {"@id": "a1", "$": "text"}
    }


def test_badgerfish_repeatable_child_arrays_import_with_one_or_many_items():
    schema = _schema(REPEATED_SCHEMA)

    single = schema.from_data({"dates": {"date": ["2026-01-01"]}}, format="badgerfish")
    single.require_valid()
    assert single.to_data(format="badgerfish", fidelity="strict") == {
        "dates": {"date": ["2026-01-01"]}
    }

    # A single non-array occurrence is accepted even though the exporter
    # always writes an array for a schema-repeatable property.
    plain = schema.from_data({"dates": {"date": "2026-01-01"}}, format="badgerfish")
    plain.require_valid()
    assert plain.to_data(format="badgerfish", fidelity="strict") == {
        "dates": {"date": ["2026-01-01"]}
    }


def test_badgerfish_reconstructs_fixed_sequence_order_from_schema():
    imported = _import(
        SEQUENCE_ATTRIBUTE_SCHEMA,
        {"root": {"b": "B", "a": "A"}},
        format="badgerfish",
    )

    imported.require_valid()
    assert [child._name_ for child in imported.root._children_] == ["a", "b"]


def test_badgerfish_reads_xmlns_prefixed_names():
    imported = _import(
        NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
        {
            "ns0:root": {
                "@xmlns": {"ns0": "urn:example"},
                "@ns0:attr": 5,
                "ns0:child": "v",
            }
        },
        format="badgerfish",
    )

    imported.require_valid()
    assert int(imported.root.attr) == 5
    assert imported.to_data(format="badgerfish", fidelity="strict") == {
        "ns0:root": {
            "@xmlns": {"ns0": "urn:example"},
            "@ns0:attr": 5,
            "ns0:child": "v",
        }
    }


def test_badgerfish_default_namespace_binds_unprefixed_element_names():
    imported = _import(
        FIXTURES["namespaced"][0],
        {"root": {"@xmlns": {"$": "urn:example"}, "@attr": "a", "child": "v"}},
        format="badgerfish",
    )

    imported.require_valid()
    assert imported.to_data(format="badgerfish", fidelity="strict") == {
        "ns0:root": {
            "@xmlns": {"ns0": "urn:example"},
            "@attr": "a",
            "ns0:child": "v",
        }
    }


def test_badgerfish_nil_keeps_attributes_and_rejects_content():
    schema = _schema(SIMPLE_CONTENT_NILABLE_SCHEMA)

    imported = schema.from_data(
        {
            "root": {
                "@xmlns": {"xsi": XSI_NAMESPACE},
                "@xsi:nil": True,
                "@id": "r",
            }
        },
        format="badgerfish",
    )
    imported.require_valid()
    assert imported.to_data(format="badgerfish", fidelity="strict") == {
        "root": {
            "@xmlns": {"xsi": XSI_NAMESPACE},
            "@xsi:nil": True,
            "@id": "r",
        }
    }

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data(
            {
                "root": {
                    "@xmlns": {"xsi": XSI_NAMESPACE},
                    "@xsi:nil": True,
                    "$": "text",
                }
            },
            format="badgerfish",
        )
    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data(
            {
                "root": {
                    "@xmlns": {"xsi": XSI_NAMESPACE},
                    "@xsi:nil": True,
                    "child": "x",
                }
            },
            format="badgerfish",
        )


@pytest.mark.parametrize("value", ["true", 1, None, []])
def test_badgerfish_nil_marker_requires_a_boolean(value):
    schema = _schema(SIMPLE_CONTENT_NILABLE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data(
            {
                "root": {
                    "@xmlns": {"xsi": XSI_NAMESPACE},
                    "@xsi:nil": value,
                }
            },
            format="badgerfish",
        )


def test_badgerfish_nil_marker_requires_its_namespace_declaration():
    schema = _schema(SIMPLE_CONTENT_NILABLE_SCHEMA)

    with pytest.raises(PyXSDError, match="namespace-unresolved"):
        schema.from_data({"root": {"@xsi:nil": True}}, format="badgerfish")


def test_badgerfish_xsi_type_selects_the_actual_layout():
    imported = _import(
        XSITYPE_SCHEMA,
        {
            "ns0:root": {
                "@xmlns": {"ns0": "urn:example", "xsi": XSI_NAMESPACE},
                "@xsi:type": "ns0:Ext",
                "ns0:a": "x",
                "ns0:b": 2,
            }
        },
        format="badgerfish",
    )

    imported.require_valid()
    assert type(imported.root).__name__ == "Ext"
    assert imported.to_data(format="badgerfish", fidelity="strict") == {
        "ns0:root": {
            "@xmlns": {"ns0": "urn:example", "xsi": XSI_NAMESPACE},
            "@xsi:type": "ns0:Ext",
            "ns0:a": "x",
            "ns0:b": 2,
        }
    }


def test_badgerfish_unresolved_xsi_type_is_a_strict_namespace_error():
    schema = _schema(XSITYPE_SCHEMA)

    with pytest.raises(PyXSDError, match="namespace-unresolved"):
        schema.from_data(
            {
                "ns0:root": {
                    "@xmlns": {"ns0": "urn:example", "xsi": XSI_NAMESPACE},
                    "@xsi:type": "missing:Ext",
                    "ns0:a": "x",
                }
            },
            format="badgerfish",
        )
    with pytest.warns(PyXSDWarning, match="namespace-unresolved"):
        imported = schema.from_data(
            {
                "ns0:root": {
                    "@xmlns": {"ns0": "urn:example", "xsi": XSI_NAMESPACE},
                    "@xsi:type": "missing:Ext",
                    "ns0:a": "x",
                }
            },
            format="badgerfish",
            fidelity="best-effort",
        )

    assert imported.root is not None
    assert any(error.code == "unknown-namespace-prefix" for error in imported.report.errors)


# ---------------------------------------------------------------------------
# JsonML import
# ---------------------------------------------------------------------------


def test_jsonml_import_keeps_ordered_children_in_place():
    imported = _import(
        FIXTURES["ordered"][0],
        [
            "section",
            ["head", "Introduction"],
            ["p", "First"],
            ["p", "Second"],
            ["note", "End"],
        ],
        format="jsonml",
    )

    imported.require_valid()
    assert [child._name_ for child in imported.root._children_] == [
        "head",
        "p",
        "p",
        "note",
    ]
    assert imported.to_data(format="jsonml", fidelity="strict") == [
        "section",
        ["head", "Introduction"],
        ["p", "First"],
        ["p", "Second"],
        ["note", "End"],
    ]


def test_jsonml_import_keeps_mixed_text_and_tails_in_order():
    imported = _import(
        FIXTURES["mixed"][0],
        ["para", {"id": "p1"}, "This is a ", ["i", "fine"], " mess!"],
        format="jsonml",
    )

    imported.require_valid()
    assert "".join(imported.root._value_) == "This is a "
    assert imported.root._children_[0]._tail_ == " mess!"
    assert imported.to_data(format="jsonml", fidelity="strict") == [
        "para",
        {"id": "p1"},
        "This is a ",
        ["i", "fine"],
        " mess!",
    ]


def test_jsonml_import_distinguishes_empty_simple_nil_and_empty_content():
    simple_empty = _import(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, ["root", ""], format="jsonml")
    simple_empty.require_valid()
    assert simple_empty.to_data(format="jsonml", fidelity="strict") == ["root", ""]

    empty = _import(EMPTY_SCHEMA, ["root"], format="jsonml")
    empty.require_valid()
    assert empty.to_data(format="jsonml", fidelity="strict") == ["root"]

    nilled = _import(NATIVE_NULLABLE_OR_EMPTY_SCHEMA, ["nullableOrEmpty", None], format="jsonml")
    nilled.require_valid()
    assert nilled.to_data(format="jsonml", fidelity="strict") == ["nullableOrEmpty", None]


def test_jsonml_nil_keeps_attributes_and_must_be_the_only_content_item():
    schema = _schema(SIMPLE_CONTENT_NILABLE_SCHEMA)

    imported = schema.from_data(["root", {"id": "r"}, None], format="jsonml")
    imported.require_valid()
    assert imported.to_data(format="jsonml", fidelity="strict") == [
        "root",
        {"id": "r"},
        None,
    ]

    for data in (
        ["root", None, "text"],
        ["root", "text", None],
        ["root", None, None],
        ["root", None, ["i", "x"]],
    ):
        with pytest.raises(PyXSDError, match="marker-conflict"):
            schema.from_data(data, format="jsonml")


def test_jsonml_reads_namespace_declarations_and_attributes():
    imported = _import(
        FIXTURES["namespaced"][0],
        [
            "ns0:root",
            {"xmlns:ns0": "urn:example", "attr": "a"},
            ["ns0:child", "v"],
        ],
        format="jsonml",
    )

    imported.require_valid()
    assert imported.to_data(format="jsonml", fidelity="strict") == [
        "ns0:root",
        {"xmlns:ns0": "urn:example", "attr": "a"},
        ["ns0:child", "v"],
    ]


def test_jsonml_default_namespace_binds_unprefixed_element_names():
    imported = _import(
        NAMESPACED_SIMPLE_ROOT_SCHEMA,
        ["root", {"xmlns": "urn:example"}, "x"],
        format="jsonml",
    )

    imported.require_valid()
    assert imported.to_data(format="jsonml", fidelity="strict") == [
        "ns0:root",
        {"xmlns:ns0": "urn:example"},
        "x",
    ]


def test_jsonml_nested_declarations_override_for_their_subtree():
    schema = _schema(WILDCARD_GENERIC_SCHEMA)
    element = data_to_element(
        schema,
        [
            "root",
            [
                "a:item",
                {"xmlns:a": "urn:a"},
                ["b:leaf", {"xmlns:b": "urn:b"}, "v"],
                ["a:other", "w"],
            ],
        ],
        format="jsonml",
    )

    item = element.find("{urn:a}item")
    assert item is not None
    leaf = element.find("{urn:a}item/{urn:b}leaf")
    assert leaf is not None and leaf.text == "v"
    other = element.find("{urn:a}item/{urn:a}other")
    assert other is not None and other.text == "w"
    assert schema.namespace_context.bindings_for(leaf) == {"a": "urn:a", "b": "urn:b"}
    assert schema.namespace_context.bindings_for(other) == {"a": "urn:a"}


def test_jsonml_unresolved_prefixes_are_strict_namespace_errors():
    schema = _schema(FIXTURES["namespaced"][0])

    with pytest.raises(PyXSDError, match="namespace-unresolved"):
        schema.from_data(["ns0:root", ["ns0:child", "v"]], format="jsonml")
    with pytest.warns(PyXSDWarning, match="namespace-unresolved"):
        imported = schema.from_data(
            ["ns0:root", ["ns0:child", "v"]],
            format="jsonml",
            fidelity="best-effort",
        )
    assert imported.root is None or imported.report.has_errors


def test_jsonml_xsi_type_selects_the_actual_layout():
    imported = _import(
        XSITYPE_SCHEMA,
        [
            "ns0:root",
            {
                "xmlns:ns0": "urn:example",
                "xmlns:xsi": XSI_NAMESPACE,
                "xsi:type": "ns0:Ext",
            },
            ["ns0:a", "x"],
            ["ns0:b", 2],
        ],
        format="jsonml",
    )

    imported.require_valid()
    assert type(imported.root).__name__ == "Ext"
    assert imported.to_data(format="jsonml", fidelity="strict") == [
        "ns0:root",
        {
            "xmlns:ns0": "urn:example",
            "xmlns:xsi": XSI_NAMESPACE,
            "xsi:type": "ns0:Ext",
        },
        ["ns0:a", "x"],
        ["ns0:b", 2],
    ]


def test_jsonml_accepts_an_empty_attribute_object_only_lexically():
    imported = _import(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, ["root", {}, "x"], format="jsonml")

    imported.require_valid()
    assert imported.to_data(format="jsonml", fidelity="strict") == ["root", "x"]


# ---------------------------------------------------------------------------
# Root selection and carried-root agreement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("format", ["pyxsd", "parker"])
def test_root_absorbed_formats_require_element_with_multiple_globals(format):
    schema = _schema(SUBSTITUTION_SCHEMA)

    with pytest.raises(PyXSDError, match="root-ambiguous"):
        schema.from_data({}, format=format)


@pytest.mark.parametrize("format", ["pyxsd", "parker"])
@pytest.mark.parametrize("element", ["root", "{urn:example}root", "Q{urn:example}root"])
def test_root_absorbed_formats_accept_local_clark_and_eqname_selection(format, element):
    schema = _schema(NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA)
    data = (
        {"@{urn:example}attr": 5, "{urn:example}child": "v"}
        if format == "pyxsd"
        else {"child": "v"}
    )

    imported = schema.from_data(data, format=format, element=element)

    imported.require_valid()
    assert imported.root is not None


@pytest.mark.parametrize("format", ["pyxsd", "parker"])
def test_root_absorbed_formats_reject_an_ambiguous_local_root(format, tmp_path):
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
        schema.from_data("x", format=format, element="root")


@pytest.mark.parametrize("format", ["badgerfish", "jsonml"])
def test_root_carrying_formats_confirm_or_reject_explicit_selection(format):
    schema = _schema(NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA)
    data: JsonValue
    if format == "badgerfish":
        data = {"ns0:root": {"@xmlns": {"ns0": "urn:example"}, "ns0:child": "v"}}
    else:
        data = ["ns0:root", {"xmlns:ns0": "urn:example"}, ["ns0:child", "v"]]

    for element in ["root", "{urn:example}root", "Q{urn:example}root"]:
        imported = schema.from_data(data, format=format, element=element)
        imported.require_valid()

    with pytest.raises(PyXSDError, match="root-mismatch"):
        schema.from_data(data, format=format, element="child")


@pytest.mark.parametrize("format", ["badgerfish", "jsonml"])
def test_root_carrying_formats_accept_substitution_members_as_roots(format):
    schema = _schema(SUBSTITUTION_SCHEMA)
    data: JsonValue = {"special": "2"} if format == "badgerfish" else ["special", "2"]

    imported = schema.from_data(data, format=format, element="item")

    imported.require_valid()
    assert imported.root is not None
    assert imported.root._name_ == "special"


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish", "jsonml"])
def test_an_unknown_explicit_root_reaches_the_binder(format):
    schema = _schema(EMPTY_SCHEMA)
    data: JsonValue
    if format in ("pyxsd", "parker"):
        data = {}
    elif format == "badgerfish":
        data = {"missing": {}}
    else:
        data = ["missing"]

    imported = schema.from_data(data, format=format, element="missing")

    assert imported.root is None
    assert any(error.code == "unknown-root" for error in imported.report.errors)


# ---------------------------------------------------------------------------
# Grouped order and mixed-content fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("format", "data", "path"),
    [
        ("pyxsd", {"b": ["x"], "a": [1]}, ""),
        ("parker", {"b": ["x"], "a": [1]}, ""),
        ("badgerfish", {"root": {"b": ["x"], "a": [1]}}, "/root"),
    ],
)
def test_grouped_formats_reject_ambiguous_interleavings(format, data, path):
    schema = _schema(SEQUENCE_REPEATED_CHOICE_SCHEMA)

    with pytest.raises(PyXSDError, match="order-ambiguous") as caught:
        schema.from_data(data, format=format)
    assert path in str(caught.value)

    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        imported = schema.from_data(data, format=format, fidelity="best-effort")

    imported.require_valid()
    names = [child._name_ for child in imported.root._children_]
    assert names == ["b", "a"]


@pytest.mark.parametrize(
    ("format", "data"),
    [
        ("pyxsd", {"a": [1, 2]}),
        ("parker", {"a": [1, 2]}),
        ("badgerfish", {"root": {"a": [1, 2]}}),
    ],
)
def test_grouped_formats_accept_a_unique_repeated_name(format, data):
    schema = _schema(SEQUENCE_REPEATED_CHOICE_SCHEMA)

    with warnings.catch_warnings():
        warnings.simplefilter("error", PyXSDWarning)
        imported = schema.from_data(data, format=format)

    imported.require_valid()


@pytest.mark.parametrize(
    ("format", "data"),
    [
        ("pyxsd", {"$": "This is a  mess!", "i": "fine"}),
        ("parker", {"$": "This is a  mess!", "i": "fine"}),
        ("badgerfish", {"para": {"$": "This is a  mess!", "i": "fine"}}),
    ],
)
def test_grouped_formats_reject_lost_mixed_text_positions(format, data):
    schema = _schema(FIXTURES["mixed"][0])

    with pytest.raises(PyXSDError, match="mixed-content"):
        schema.from_data(data, format=format)

    with pytest.warns(PyXSDWarning, match="mixed-content"):
        imported = schema.from_data(data, format=format, fidelity="best-effort")

    imported.require_valid()
    assert "".join(imported.root._value_) == "This is a  mess!"


@pytest.mark.parametrize(
    ("format", "data"),
    [
        ("pyxsd", {"i": "x", "b": "y"}),
        ("parker", {"i": "x", "b": "y"}),
        ("badgerfish", {"root": {"i": "x", "b": "y"}}),
    ],
)
def test_grouped_mixed_multiple_names_are_an_order_loss(format, data):
    schema = _schema(MIXED_TWO_CHILD_SCHEMA)

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        schema.from_data(data, format=format)
    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        imported = schema.from_data(data, format=format, fidelity="best-effort")

    imported.require_valid()
    assert [child._name_ for child in imported.root._children_] == ["i", "b"]


def test_parker_does_not_treat_a_text_key_as_mixed_text_outside_mixed_content():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"$": "text"}, format="parker")


# ---------------------------------------------------------------------------
# Lexical QName fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("format", "data"),
    [
        ("pyxsd", {"$": "v:item"}),
        ("parker", "v:item"),
        ("badgerfish", {"root": {"@xmlns": {"v": "urn:values"}, "$": "missing:item"}}),
        (
            "jsonml",
            ["root", {"xmlns:v": "urn:values"}, "missing:item"],
        ),
    ],
)
def test_unresolved_qname_values_are_strict_namespace_errors(format, data):
    schema = _schema(QNAME_ROOT_SCHEMA)

    with pytest.raises(PyXSDError, match="namespace-unresolved"):
        schema.from_data(data, format=format)
    with pytest.warns(PyXSDWarning, match="namespace-unresolved"):
        imported = schema.from_data(data, format=format, fidelity="best-effort")

    imported.require_valid()


def test_caller_namespaces_resolve_native_and_parker_qname_values():
    native = _schema(QNAME_ROOT_SCHEMA).from_data(
        {"$": "v:item"}, format="pyxsd", namespaces={"v": "urn:values"}
    )
    native.require_valid()
    assert native.root.to_expanded_name() == "{urn:values}item"

    parker = _schema(QNAME_ROOT_SCHEMA).from_data(
        "v:item", format="parker", namespaces={"v": "urn:values"}
    )
    parker.require_valid()
    assert parker.root.to_expanded_name() == "{urn:values}item"


def test_badgerfish_and_jsonml_qname_values_resolve_from_their_frames():
    badgerfish = _schema(QNAME_CHILD_SCHEMA).from_data(
        {
            "root": {
                "ref": {
                    "@xmlns": {"v": "urn:values"},
                    "$": "v:item",
                }
            }
        },
        format="badgerfish",
    )
    badgerfish.require_valid()
    assert badgerfish.root.ref.to_expanded_name() == "{urn:values}item"

    jsonml = _schema(QNAME_CHILD_SCHEMA).from_data(
        ["root", ["ref", {"xmlns:v": "urn:values"}, "v:item"]],
        format="jsonml",
    )
    jsonml.require_valid()
    assert jsonml.root.ref.to_expanded_name() == "{urn:values}item"


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish", "jsonml"])
def test_malformed_names_are_conversion_errors(format):
    schema = _schema(EMPTY_SCHEMA)
    data: JsonValue
    if format == "pyxsd" or format == "parker":
        data = {"bad name": ""}
    elif format == "badgerfish":
        data = {"root": {"bad name": ""}}
    else:
        data = ["root", ["bad name", ""]]

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data(data, format=format)


# ---------------------------------------------------------------------------
# Wrong containers and unknown content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish", "jsonml"])
def test_conventional_formats_report_wrong_root_containers(format):
    schema = _schema(EMPTY_SCHEMA)
    data: JsonValue = ["root"] if format in ("pyxsd", "parker", "badgerfish") else "root"

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data(data, format=format)


@pytest.mark.parametrize(
    ("format", "data"),
    [
        ("pyxsd", {"root": "text"}),
        ("parker", "text"),
        ("badgerfish", {"root": "text"}),
        ("jsonml", ["root", "text"]),
    ],
)
def test_element_only_scalars_reach_the_validation_report(format, data):
    schema = _schema(SEQUENCE_OPTIONAL_SCHEMA)

    imported = schema.from_data(data, format=format)

    assert imported.root is not None
    assert imported.report.has_errors


@pytest.mark.parametrize(
    ("format", "data"),
    [
        ("pyxsd", {"root": {"zzz": "v"}}),
        ("parker", {"zzz": "v"}),
        ("badgerfish", {"root": {"p:zzz": "v", "@xmlns": {"p": "urn:x"}}}),
        ("jsonml", ["root", ["p:zzz", {"xmlns:p": "urn:x"}, "v"]]),
    ],
)
def test_unknown_conventional_content_reaches_the_binder(format, data):
    schema = _schema(EMPTY_SCHEMA)

    if format == "parker":
        with pytest.warns(PyXSDWarning, match="name-ambiguous"):
            imported = schema.from_data(data, format=format, fidelity="best-effort")
    else:
        imported = schema.from_data(data, format=format, fidelity="best-effort")

    assert imported.root is not None
    assert any(error.code == "unexpected-element" for error in imported.report.errors)


@pytest.mark.parametrize(
    ("format", "data"),
    [
        ("badgerfish", {"root": {"@id": "r"}, "other": {}}),
        ("parker", {1: "x"}),
        ("jsonml", []),
    ],
)
def test_malformed_conventional_roots_are_conversion_errors(format, data):
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError):
        schema.from_data(data, format=format)


def test_badgerfish_duplicate_expanded_attributes_conflict():
    schema = _schema(FIXTURES["namespaced"][0])

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data(
            {
                "root": {
                    "@xmlns": {"a": "urn:example", "b": "urn:example"},
                    "@a:attr": "x",
                    "@b:attr": "y",
                }
            },
            format="badgerfish",
        )


def test_jsonml_attribute_object_values_use_declared_types():
    imported = _import(
        NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA,
        [
            "ns0:root",
            {"xmlns:ns0": "urn:example", "ns0:attr": 5},
            ["ns0:child", "v"],
        ],
        format="jsonml",
    )

    imported.require_valid()
    assert int(imported.root.attr) == 5


def test_jsonml_scalar_arrays_are_xsd_lists_only_for_list_types():
    list_schema = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:IDREFS"/>
</xs:schema>"""
    imported = _import(list_schema, ["root", ["a", "b"]], format="jsonml")

    # The values bind; the dangling IDREFs are normal validation errors.
    assert str(imported.root) == "a b"
    assert imported.to_data(format="jsonml", fidelity="strict") == ["root", ["a", "b"]]
    assert imported.to_data(format="jsonml", fidelity="strict") == ["root", ["a", "b"]]

    with pytest.raises(PyXSDError, match="container-invalid"):
        _schema(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA).from_data(["root", ["a", "b"]], format="jsonml")


# ---------------------------------------------------------------------------
# Conventional-format edge branches
# ---------------------------------------------------------------------------

NAMESPACED_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:element name="known" type="xs:string"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

LIST_CHILD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="refs" type="xs:IDREFS"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

REPEATED_LIST_CHILD_SCHEMA = LIST_CHILD_SCHEMA.replace(
    '<xs:element name="refs" type="xs:IDREFS"/>',
    '<xs:element name="refs" type="xs:IDREFS" maxOccurs="unbounded"/>',
)


def test_native_explicit_xsi_type_resolves_when_its_prefix_is_bound():
    imported = _import(
        XSITYPE_SCHEMA,
        {
            "@xsi:type": "t:Ext",
            "{urn:example}a": "x",
            "{urn:example}b": 2,
        },
        format="pyxsd",
        namespaces={"t": "urn:example"},
    )

    imported.require_valid()
    assert type(imported.root).__name__ == "Ext"
    assert imported.to_data(format="pyxsd") == {
        "@xsi:type": "t:Ext",
        "{urn:example}a": "x",
        "{urn:example}b": 2,
    }


def test_native_unresolved_xsi_type_keeps_the_binder_report():
    imported = _import(
        XSITYPE_SCHEMA,
        {
            "@xsi:type": "t:Ext",
            "{urn:example}a": "x",
            "{urn:example}b": 2,
        },
        format="pyxsd",
    )

    assert imported.root is not None
    assert any(error.code == "unknown-namespace-prefix" for error in imported.report.errors)


def test_native_duplicate_markers_are_conversion_errors():
    schema = _schema(XSITYPE_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data(
            {"@xsi:type": "t:Ext", f"@{XSI_TYPE}": "t:Ext", "{urn:example}a": "x"},
            format="pyxsd",
        )

    nullable = _schema(NATIVE_NULLABLE_OR_EMPTY_SCHEMA)
    with pytest.raises(PyXSDError, match="marker-conflict"):
        nullable.from_data(
            {"@xsi:nil": "true", f"@{XSI_NIL}": "true"},
            format="pyxsd",
        )


@pytest.mark.parametrize("value", [5, True, ["t:Ext"]])
def test_native_xsi_type_requires_a_lexical_string(value):
    schema = _schema(XSITYPE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"@xsi:type": value}, format="pyxsd")


def test_native_clark_attribute_child_and_root_names_are_validated():
    schema = _schema(ANYTYPE_ROOT_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"{urn:x}bad name": "v"}, format="pyxsd")
    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"@{urn:x}bad name": "v"}, format="pyxsd")
    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({}, format="pyxsd", element="{urn:x}bad name")


def test_native_clark_child_can_reuse_a_global_declaration():
    imported = _import(
        NAMESPACED_WILDCARD_SCHEMA,
        {"{urn:example}known": "v"},
        format="pyxsd",
        element="root",
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd") == {"{urn:example}known": "v"}


def test_native_generic_objects_decode_attributes_children_and_text():
    schema = _schema(ANYTYPE_ROOT_SCHEMA)

    with pytest.warns(PyXSDWarning, match="mixed-content"):
        imported = schema.from_data(
            {"@a": "x", "{urn:run}child": ["v1", "v2"], "$": "text"},
            format="pyxsd",
            fidelity="best-effort",
        )

    imported.require_valid()
    assert imported.root._attribs_ == {"a": "x"}
    assert [child._name_ for child in imported.root._children_] == [
        "{urn:run}child",
        "{urn:run}child",
    ]
    assert "".join(imported.root._value_) == "text"


@pytest.mark.parametrize(
    "data",
    [
        {"one": "x", "two": "y"},
        {"{urn:one}one": "x", "{urn:two}two": "y"},
    ],
)
def test_native_unknown_grouped_names_are_order_ambiguous(data):
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        schema.from_data(data, format="pyxsd")


def test_native_duplicate_expanded_child_keys_conflict():
    schema = _schema(FIXTURES["namespaced"][0])

    with pytest.raises(PyXSDError, match="json-duplicate-key"):
        schema.from_data(
            {"child": "v1", "{urn:example}child": "v2"},
            format="pyxsd",
        )


def test_native_record_layout_ignores_mapping_order():
    imported = _import(RECORD_SCHEMA, {"b": "B", "a": "A"}, format="pyxsd")

    imported.require_valid()
    assert [child._name_ for child in imported.root._children_] == ["a", "b"]


def test_native_element_only_scalar_text_reaches_the_report():
    schema = _schema(SEQUENCE_OPTIONAL_SCHEMA)

    imported = schema.from_data({"$": "oops"}, format="pyxsd")

    assert imported.root is not None
    assert imported.report.has_errors

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data(5, format="pyxsd", element="root")


def test_native_mixed_text_must_be_a_string():
    schema = _schema(MIXED_NO_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data(5, format="pyxsd", element="root")


def test_native_empty_dollar_text_is_ignored():
    imported = _import(EMPTY_SCHEMA, {"$": None}, format="pyxsd")

    imported.require_valid()
    assert imported.to_data(format="pyxsd") == {"$": None}


def test_native_wrong_scalar_type_carries_its_attribute_pointer():
    schema = _schema(SIMPLE_CONTENT_INT_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data({"@id": True, "$": "text"}, format="pyxsd")
    assert "/@id" in str(caught.value)


def test_native_undeclared_attribute_values_must_be_strings():
    schema = _schema(ANYTYPE_ROOT_SCHEMA)

    imported = schema.from_data({"@wild": "x"}, format="pyxsd")
    imported.require_valid()

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"@wild": 5}, format="pyxsd")


def test_native_single_list_valued_child_is_one_element():
    imported = _import(LIST_CHILD_SCHEMA, {"refs": ["a", "b"]}, format="pyxsd")

    assert imported.root is not None
    assert str(imported.root.refs) == "a b"

    with pytest.raises(PyXSDError, match="container-invalid"):
        _schema(REPEATED_LIST_CHILD_SCHEMA).from_data({"refs": ["a", "b"]}, format="pyxsd")


def test_native_qname_lists_check_each_item_prefix():
    imported = _import(
        QNAME_LIST_SCHEMA,
        {"$": ["v:a", "b"]},
        format="pyxsd",
        namespaces={"v": "urn:values"},
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd") == {"$": ["v:a", "b"]}

    with pytest.raises(PyXSDError, match="namespace-unresolved"):
        _schema(QNAME_LIST_SCHEMA).from_data({"$": ["v:a", "b"]}, format="pyxsd")


def test_parker_clark_and_prefixed_names_are_invalid():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"p:child": "v"}, format="parker")
    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"{urn:x}child": "v"}, format="parker")


def test_parker_uses_a_global_declaration_for_a_wildcard_local_name():
    imported = _import(
        SEQUENCE_WILDCARD_SCHEMA,
        {"known": "v"},
        format="parker",
        element="root",
    )

    imported.require_valid()
    assert imported.to_data(format="parker", fidelity="strict") == {"known": "v"}


def test_parker_generic_objects_use_the_documented_name_fallback():
    schema = _schema(ANYTYPE_ROOT_SCHEMA)

    with pytest.warns(PyXSDWarning, match="name-ambiguous"):
        imported = schema.from_data(
            {"child": ["v1", "v2"]},
            format="parker",
            fidelity="best-effort",
        )

    assert imported.root is not None
    assert [child._name_ for child in imported.root._children_] == ["child", "child"]


def test_parker_element_only_scalar_text_reaches_the_report():
    imported = _import(SEQUENCE_OPTIONAL_SCHEMA, "oops", format="parker")

    assert imported.root is not None
    assert imported.report.has_errors


def test_badgerfish_namespace_declaration_errors():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": {"@xmlns": "urn:x"}}, format="badgerfish")
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"root": {"@xmlns": {"p": 5}}}, format="badgerfish")
    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"root": {"@xmlns": {"bad prefix": "urn:x"}}}, format="badgerfish")


def test_badgerfish_unresolved_prefixes_fall_back_with_a_warning():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.warns(PyXSDWarning, match="namespace-unresolved"):
        imported = schema.from_data({"p:root": {}}, format="badgerfish", fidelity="best-effort")
    assert imported.root is None

    declared = _schema(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA)
    with pytest.warns(PyXSDWarning, match="namespace-unresolved"):
        imported = declared.from_data(
            {"root": {"@p:custom": "v", "$": "text"}},
            format="badgerfish",
            fidelity="best-effort",
        )
    assert imported.root is not None
    assert imported.report.has_errors


def test_badgerfish_duplicate_markers_and_invalid_type_values():
    schema = _schema(XSITYPE_SCHEMA)
    base = {
        "@xmlns": {"ns0": "urn:example", "xsi": XSI_NAMESPACE, "y": XSI_NAMESPACE},
        "ns0:a": "x",
    }

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data(
            {"ns0:root": {**base, "@xsi:type": "ns0:Ext", "@y:type": "ns0:Ext"}},
            format="badgerfish",
        )
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data(
            {"ns0:root": {**base, "@xsi:type": 5}},
            format="badgerfish",
        )

    nullable = _schema(SIMPLE_CONTENT_NILABLE_SCHEMA)
    with pytest.raises(PyXSDError, match="marker-conflict"):
        nullable.from_data(
            {
                "root": {
                    "@xmlns": {"xsi": XSI_NAMESPACE, "y": XSI_NAMESPACE},
                    "@xsi:nil": True,
                    "@y:nil": True,
                }
            },
            format="badgerfish",
        )


def test_badgerfish_bare_null_root_is_nil():
    imported = _import(
        NATIVE_NULLABLE_OR_EMPTY_SCHEMA, {"nullableOrEmpty": None}, format="badgerfish"
    )

    imported.require_valid()
    assert imported.to_data(format="badgerfish", fidelity="strict") == {
        "nullableOrEmpty": {"@xmlns": {"xsi": XSI_NAMESPACE}, "@xsi:nil": True}
    }


def test_badgerfish_scalar_content_type_errors():
    mixed = _schema(MIXED_NO_ATTRIBUTE_SCHEMA)
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        mixed.from_data({"root": 5}, format="badgerfish")

    sequence = _schema(SEQUENCE_OPTIONAL_SCHEMA)
    scalar = sequence.from_data({"root": "oops"}, format="badgerfish")
    assert scalar.root is not None
    assert scalar.report.has_errors
    with pytest.raises(PyXSDError, match="container-invalid"):
        sequence.from_data({"root": 5}, format="badgerfish")

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        mixed.from_data({"root": {"$": 5}}, format="badgerfish")


def test_badgerfish_empty_dollar_text_is_ignored():
    imported = _import(MIXED_NO_ATTRIBUTE_SCHEMA, {"root": {"$": None}}, format="badgerfish")

    imported.require_valid()
    assert imported.to_data(format="badgerfish", fidelity="strict") == {"root": {}}


def test_badgerfish_generic_grouped_content_reports_its_losses():
    schema = _schema(ANYTYPE_ROOT_SCHEMA)

    with pytest.warns(PyXSDWarning, match="mixed-content"):
        imported = schema.from_data(
            {"root": {"$": "text", "child": "v"}},
            format="badgerfish",
            fidelity="best-effort",
        )

    imported.require_valid()
    with pytest.warns(PyXSDWarning, match="mixed-content"):
        exported = imported.to_data(format="badgerfish", fidelity="best-effort")
    assert exported == {"root": {"$": "text", "child": "v"}}

    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        generic = schema.from_data(
            {"root": {"one": "x", "two": "y"}},
            format="badgerfish",
            fidelity="best-effort",
        )
    assert generic.root is not None


def test_badgerfish_single_list_valued_child_is_one_element():
    imported = _import(LIST_CHILD_SCHEMA, {"root": {"refs": ["a", "b"]}}, format="badgerfish")

    assert imported.root is not None
    assert str(imported.root.refs) == "a b"

    with pytest.raises(PyXSDError, match="container-invalid"):
        _schema(REPEATED_LIST_CHILD_SCHEMA).from_data(
            {"root": {"refs": ["a", "b"]}}, format="badgerfish"
        )


def test_badgerfish_undeclared_attribute_values_must_be_strings():
    schema = _schema(ANYTYPE_ROOT_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"root": {"@custom": 5}}, format="badgerfish")


def test_jsonml_simple_content_container_errors():
    schema = _schema(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data(["root", "a", "b"], format="jsonml")
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data(["root", {"id": "r"}, {"a": 1}], format="jsonml")
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data(["root", ["a", "b"]], format="jsonml")


def test_jsonml_element_only_whitespace_and_empty_text_are_discarded():
    imported = _import(
        SEQUENCE_OPTIONAL_SCHEMA,
        ["root", "", "  ", ["a", "A"], "\n"],
        format="jsonml",
    )

    imported.require_valid()
    assert imported.to_data(format="jsonml", fidelity="strict") == ["root", ["a", "A"]]


def test_jsonml_content_item_type_errors():
    mixed = _schema(MIXED_NO_ATTRIBUTE_SCHEMA)
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        mixed.from_data(["root", 5], format="jsonml")

    sequence = _schema(SEQUENCE_OPTIONAL_SCHEMA)
    with pytest.raises(PyXSDError, match="container-invalid"):
        sequence.from_data(["root", 5], format="jsonml")

    with pytest.raises(PyXSDError, match="container-invalid"):
        sequence.from_data(["root", {"id": "r"}, {"a": 1}], format="jsonml")


def test_jsonml_nested_tag_and_declaration_errors():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data(["root", []], format="jsonml")
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data(["root", [42]], format="jsonml")
    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data(["root", {"xmlns:bad prefix": "urn:x"}], format="jsonml")
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data(["root", {"xmlns": 5}], format="jsonml")


def test_jsonml_unresolved_attribute_prefixes_fall_back_with_a_warning():
    schema = _schema(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA)

    with pytest.warns(PyXSDWarning, match="namespace-unresolved"):
        imported = schema.from_data(
            ["root", {"p:custom": "v"}, "text"],
            format="jsonml",
            fidelity="best-effort",
        )

    assert imported.root is not None
    assert imported.report.has_errors


def test_jsonml_undeclared_attribute_values_must_be_strings():
    schema = _schema(SIMPLE_CONTENT_WILDCARD_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data(["root", {"custom": 5}, "v"], format="jsonml")


def test_jsonml_invalid_attribute_names_are_name_errors():
    schema = _schema(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data(["root", {"bad name": "v"}, "text"], format="jsonml")


def test_jsonml_duplicate_type_markers_and_invalid_type_values():
    schema = _schema(XSITYPE_SCHEMA)
    attrs = {
        "xmlns:ns0": "urn:example",
        "xmlns:xsi": XSI_NAMESPACE,
        "xmlns:y": XSI_NAMESPACE,
    }

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data(
            [
                "ns0:root",
                {**attrs, "xsi:type": "ns0:Ext", "y:type": "ns0:Ext"},
                ["ns0:a", "x"],
            ],
            format="jsonml",
        )
    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data(
            ["ns0:root", {**attrs, "xsi:type": 5}, ["ns0:a", "x"]],
            format="jsonml",
        )


def test_jsonml_unresolved_xsi_type_falls_back_with_a_warning():
    schema = _schema(XSITYPE_SCHEMA)

    with pytest.warns(PyXSDWarning, match="namespace-unresolved"):
        imported = schema.from_data(
            [
                "ns0:root",
                {"xmlns:ns0": "urn:example", "xmlns:xsi": XSI_NAMESPACE, "xsi:type": "missing:Ext"},
                ["ns0:a", "x"],
            ],
            format="jsonml",
            fidelity="best-effort",
        )

    assert imported.root is not None
    assert any(error.code == "unknown-namespace-prefix" for error in imported.report.errors)


def test_jsonml_empty_roots_and_simple_empty_content():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="root-ambiguous"):
        schema.from_data([], format="jsonml")

    simple = _import(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA, ["root"], format="jsonml")
    simple.require_valid()
    assert simple.to_data(format="jsonml", fidelity="strict") == ["root", ""]


def test_jsonml_xml_prefix_is_implicit():
    imported = _import(
        SIMPLE_CONTENT_DECLARED_WILDCARD_SCHEMA,
        ["root", {"xml:lang": "en"}, "v"],
        format="jsonml",
    )

    imported.require_valid()
    assert imported.to_data(format="jsonml", fidelity="strict") == [
        "root",
        {"xml:lang": "en"},
        "v",
    ]


def test_badgerfish_empty_content_with_multiple_unknown_names_is_ambiguous():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        schema.from_data({"root": {"one": "x", "two": "y"}}, format="badgerfish")


def test_native_mixed_multiple_names_without_text_is_an_order_loss():
    schema = _schema(MIXED_TWO_CHILD_SCHEMA)

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        schema.from_data({"i": "x", "b": "y"}, format="pyxsd")


def test_native_null_with_an_optional_child_model_is_ambiguous():
    schema = _schema(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string" minOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
    )

    with pytest.raises(PyXSDError, match="empty-nil-ambiguous"):
        schema.from_data(None, format="pyxsd")
    with pytest.warns(PyXSDWarning, match="empty-nil-ambiguous"):
        imported = schema.from_data(None, format="pyxsd", fidelity="best-effort")
    imported.require_valid()


def test_native_null_with_a_wildcard_is_ambiguous():
    schema = _schema(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true">
    <xs:complexType>
      <xs:sequence>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
    )

    with pytest.raises(PyXSDError, match="empty-nil-ambiguous"):
        schema.from_data(None, format="pyxsd")


def test_native_null_with_a_required_attribute_selects_nil():
    schema = _schema(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true">
    <xs:complexType>
      <xs:attribute name="id" type="xs:string" use="required"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
    )

    imported = schema.from_data(None, format="pyxsd")

    assert imported.root is not None
    assert any(error.code == "missing-attribute" for error in imported.report.errors)
    assert imported.to_data(format="pyxsd") == {"@xsi:nil": "true"}


def test_native_null_with_a_default_is_ambiguous():
    schema = _schema(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:string" nillable="true" default="fallback"/>
</xs:schema>"""
    )

    with pytest.raises(PyXSDError, match="empty-nil-ambiguous"):
        schema.from_data(None, format="pyxsd")


def test_native_nillable_mixed_and_simple_nulls_are_ambiguous():
    mixed = _schema(MIXED_NILABLE_SCHEMA)
    with pytest.raises(PyXSDError, match="empty-nil-ambiguous"):
        mixed.from_data(None, format="pyxsd")

    string_schema = _schema(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:string" nillable="true"/>
</xs:schema>"""
    )
    with pytest.raises(PyXSDError, match="empty-nil-ambiguous"):
        string_schema.from_data(None, format="pyxsd")


def test_native_derived_attribute_redeclarations_do_not_break_null_checks():
    schema = _schema(
        EXTENDED_ATTRIBUTE_SCHEMA.replace('name="root"', 'name="root" nillable="true"')
    )

    with pytest.raises(PyXSDError, match="empty-nil-ambiguous"):
        schema.from_data(None, format="pyxsd")


def test_native_wildcard_grouped_order_is_ambiguous():
    schema = _schema(WILDCARD_GENERIC_SCHEMA)

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        schema.from_data({"{urn:one}one": "x", "{urn:two}two": "y"}, format="pyxsd")
    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        imported = schema.from_data(
            {"{urn:one}one": "x", "{urn:two}two": "y"},
            format="pyxsd",
            fidelity="best-effort",
        )
    assert imported.root is not None


def test_badgerfish_wildcard_grouped_order_is_ambiguous():
    schema = _schema(WILDCARD_GENERIC_SCHEMA)

    with pytest.raises(PyXSDError, match="order-ambiguous"):
        schema.from_data(
            {
                "root": {
                    "@xmlns": {"p": "urn:one", "q": "urn:two"},
                    "p:one": "x",
                    "q:two": "y",
                }
            },
            format="badgerfish",
        )


def test_native_ambiguous_attribute_locals_across_namespaces(tmp_path):
    (tmp_path / "a.xsd").write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:a">
  <xs:attribute name="attr" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    (tmp_path / "b.xsd").write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:b">
  <xs:attribute name="attr" type="xs:string"/>
</xs:schema>""",
        encoding="utf-8",
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:a="urn:a" xmlns:b="urn:b">
  <xs:import namespace="urn:a" schemaLocation="a.xsd"/>
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:attribute ref="a:attr"/>
      <xs:attribute ref="b:attr"/>
    </xs:complexType>
  </xs:element>
</xs:schema>""",
        encoding="utf-8",
    )
    schema = pyxsd.Schema.compile(str(main))

    with pytest.raises(PyXSDError, match="name-ambiguous"):
        schema.from_data({"@attr": "x"}, format="pyxsd")


def test_native_ambiguous_child_locals_across_namespaces(tmp_path):
    for prefix, uri in (("a", "urn:a"), ("b", "urn:b")):
        (tmp_path / f"{prefix}.xsd").write_text(
            f"""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="{uri}">
  <xs:element name="item" type="xs:string"/>
</xs:schema>""",
            encoding="utf-8",
        )
    main = tmp_path / "main.xsd"
    main.write_text(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:a="urn:a" xmlns:b="urn:b">
  <xs:import namespace="urn:a" schemaLocation="a.xsd"/>
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>""",
        encoding="utf-8",
    )
    schema = pyxsd.Schema.compile(str(main))

    with pytest.raises(PyXSDError, match="name-ambiguous"):
        schema.from_data({"item": "x"}, format="pyxsd", element="root")


def test_native_ambiguous_local_child_in_a_declared_model():
    schema = _schema(PARKER_LOCAL_COLLISION_SCHEMA)

    with pytest.raises(PyXSDError, match="name-ambiguous"):
        schema.from_data({"item": "x"}, format="pyxsd")


def test_native_element_only_string_scalar_reaches_the_report():
    schema = _schema(SEQUENCE_OPTIONAL_SCHEMA)

    imported = schema.from_data("oops", format="pyxsd", element="root")

    assert imported.root is not None
    assert imported.report.has_errors


def test_native_untyped_generic_scalars_accept_native_typed_values():
    schema = _schema(ANYTYPE_ROOT_SCHEMA)

    boolean = schema.from_data(True, format="pyxsd")
    boolean.require_valid()
    assert boolean.to_data(format="pyxsd") == {"$": "true"}

    integer = schema.from_data(7, format="pyxsd")
    integer.require_valid()
    assert integer.to_data(format="pyxsd") == {"$": "7"}

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data(["x"], format="pyxsd")


def test_native_clark_xsi_type_marker_is_skipped_as_an_attribute():
    imported = _import(
        XSITYPE_SCHEMA,
        {
            f"@{XSI_TYPE}": "t:Ext",
            "{urn:example}a": "x",
            "{urn:example}b": 2,
        },
        format="pyxsd",
        namespaces={"t": "urn:example"},
    )

    imported.require_valid()
    assert type(imported.root).__name__ == "Ext"


@pytest.mark.parametrize("value", [5, ["true"], {"a": 1}])
def test_native_nil_marker_requires_a_boolean_or_lexical_string(value):
    schema = _schema(NATIVE_NULLABLE_OR_EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"@xsi:nil": value}, format="pyxsd")


def test_native_non_string_keys_are_conversion_errors():
    schema = _schema(ANYTYPE_ROOT_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({1: "x"}, format="pyxsd")


def test_native_mixed_dollar_text_must_be_a_string():
    schema = _schema(MIXED_NO_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"$": 5}, format="pyxsd")


def test_native_untyped_null_without_a_declaration_is_empty():
    root_schema = _schema(ANYTYPE_ROOT_SCHEMA)

    empty = root_schema.from_data(None, format="pyxsd")
    empty.require_valid()
    assert empty.to_data(format="pyxsd") == {"$": ""}

    wildcard = _schema(WILDCARD_GENERIC_SCHEMA)
    child = wildcard.from_data({"other": None}, format="pyxsd", element="root")
    child.require_valid()
    assert child.to_data(format="pyxsd") == {"other": None}


def test_native_null_with_a_fixed_value_is_ambiguous():
    schema = _schema(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:string" nillable="true" fixed="x"/>
</xs:schema>"""
    )

    with pytest.raises(PyXSDError, match="empty-nil-ambiguous"):
        schema.from_data(None, format="pyxsd")


def test_native_null_with_a_required_fixed_attribute_is_ambiguous():
    schema = _schema(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" nillable="true">
    <xs:complexType>
      <xs:attribute name="id" type="xs:string" use="required" fixed="d"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
    )

    with pytest.raises(PyXSDError, match="empty-nil-ambiguous"):
        schema.from_data(None, format="pyxsd")


def test_badgerfish_non_string_namespace_and_property_keys():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data({"root": {"@xmlns": {1: "urn:x"}}}, format="badgerfish")
    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data({"root": {1: "x"}}, format="badgerfish")


def test_badgerfish_declared_attribute_scalar_errors_carry_the_pointer():
    schema = _schema(NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data(
            {
                "ns0:root": {
                    "@xmlns": {"ns0": "urn:example"},
                    "@ns0:attr": "x",
                    "ns0:child": "v",
                }
            },
            format="badgerfish",
        )
    assert "/@ns0:attr" in str(caught.value)


def test_badgerfish_unknown_bound_type_reaches_the_binder():
    schema = _schema(XSITYPE_SCHEMA)

    imported = schema.from_data(
        {
            "ns0:root": {
                "@xmlns": {"ns0": "urn:example", "xsi": XSI_NAMESPACE},
                "@xsi:type": "ns0:Missing",
                "ns0:a": "x",
            }
        },
        format="badgerfish",
    )

    assert imported.root is not None
    assert any(error.code == "xsi-type" for error in imported.report.errors)


def test_badgerfish_duplicate_expanded_children_are_rejected():
    schema = _schema(WILDCARD_GENERIC_SCHEMA)

    with pytest.raises(PyXSDError, match="json-duplicate-key"):
        schema.from_data(
            {
                "root": {
                    "@xmlns": {"a": "urn:same", "b": "urn:same"},
                    "a:child": "x",
                    "b:child": "y",
                }
            },
            format="badgerfish",
        )


def test_badgerfish_untyped_generic_scalars_must_be_strings():
    schema = _schema(ANYTYPE_ROOT_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data({"root": 5}, format="badgerfish")


def test_badgerfish_record_import_ignores_mapping_order():
    imported = _import(RECORD_SCHEMA, {"root": {"b": "B", "a": "A"}}, format="badgerfish")

    imported.require_valid()
    assert [child._name_ for child in imported.root._children_] == ["a", "b"]


def test_jsonml_non_string_attribute_and_namespace_keys():
    schema = _schema(EMPTY_SCHEMA)

    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data(["root", {1: "urn:x"}], format="jsonml")
    with pytest.raises(PyXSDError, match="name-invalid"):
        schema.from_data(["root", {1: "v"}], format="jsonml")


def test_jsonml_declared_attribute_scalar_errors_carry_the_pointer():
    schema = _schema(NAMESPACED_QUALIFIED_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data(
            [
                "ns0:root",
                {"xmlns:ns0": "urn:example", "ns0:attr": "x"},
                ["ns0:child", "v"],
            ],
            format="jsonml",
        )
    assert "/1/ns0:attr" in str(caught.value)


def test_jsonml_unknown_bound_type_reaches_the_binder():
    schema = _schema(XSITYPE_SCHEMA)

    imported = schema.from_data(
        [
            "ns0:root",
            {
                "xmlns:ns0": "urn:example",
                "xmlns:xsi": XSI_NAMESPACE,
                "xsi:type": "ns0:Missing",
            },
            ["ns0:a", "x"],
        ],
        format="jsonml",
    )

    assert imported.root is not None
    assert any(error.code == "xsi-type" for error in imported.report.errors)


def test_jsonml_null_after_content_conflicts():
    schema = _schema(MIXED_NILABLE_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data(["root", "text", None], format="jsonml")


def test_jsonml_simple_multiple_scalar_items_are_a_container_error():
    schema = _schema(SIMPLE_CONTENT_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="container-invalid"):
        schema.from_data(["root", "a", "b"], format="jsonml")


def test_native_xsd_list_of_non_qnames_needs_no_namespace_check():
    schema = _schema(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="Ints">
    <xs:list itemType="xs:int"/>
  </xs:simpleType>
  <xs:element name="root" type="Ints"/>
</xs:schema>"""
    )

    imported = schema.from_data({"$": [1, 2]}, format="pyxsd")

    imported.require_valid()
    assert imported.to_data(format="pyxsd") == {"$": [1, 2]}


def test_badgerfish_malformed_lexical_names_are_name_errors():
    schema = _schema(EMPTY_SCHEMA)

    for name in ("bad{name", "a:b:c", "bad prefix:local", "a:bad local"):
        with pytest.raises(PyXSDError, match="name-invalid"):
            schema.from_data({"root": {name: "x"}}, format="badgerfish")


def test_native_mixed_scalar_text_is_accepted():
    imported = _import(MIXED_NO_ATTRIBUTE_SCHEMA, "text", format="pyxsd")

    imported.require_valid()
    assert imported.to_data(format="pyxsd", fidelity="strict") == {"$": "text"}


def test_parker_untyped_generic_scalar_values_must_be_strings():
    schema = _schema(ANYTYPE_ROOT_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid"):
        schema.from_data(5, format="parker")


def test_badgerfish_mixed_scalar_text_is_accepted():
    imported = _import(MIXED_NO_ATTRIBUTE_SCHEMA, {"root": "text"}, format="badgerfish")

    imported.require_valid()
    assert imported.to_data(format="badgerfish", fidelity="strict") == {"root": "text"}


def test_jsonml_nil_before_content_conflicts():
    schema = _schema(MIXED_NILABLE_SCHEMA)

    with pytest.raises(PyXSDError, match="marker-conflict"):
        schema.from_data(["root", None, "text"], format="jsonml")


def test_native_qname_lists_accept_unprefixed_items():
    imported = _import(
        QNAME_LIST_SCHEMA,
        {"$": ["a", "b"]},
        format="pyxsd",
    )

    imported.require_valid()
    assert imported.to_data(format="pyxsd") == {"$": ["a", "b"]}


def test_native_open_content_uses_the_effective_content_model():
    schema = _schema(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:openContent mode="interleave">
        <xs:any processContents="lax"/>
      </xs:openContent>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
        <xs:element name="b" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
    )
    generated_type = schema.classes["schema"]()._getElements()[0].getType()
    assert "_instanceContentModel_" in generated_type.__dict__

    imported = schema.from_data({"b": "B", "a": "A"}, format="pyxsd")

    imported.require_valid()
    assert [child._name_ for child in imported.root._children_] == ["a", "b"]


def test_badgerfish_qname_lists_accept_unprefixed_items():
    imported = _import(
        QNAME_LIST_SCHEMA,
        {
            "ns0:root": {
                "@xmlns": {"ns0": "urn:example"},
                "$": ["a", "b"],
            }
        },
        format="badgerfish",
    )

    imported.require_valid()
    assert imported.to_data(format="badgerfish", fidelity="strict") == {
        "ns0:root": {
            "@xmlns": {"ns0": "urn:example"},
            "$": ["a", "b"],
        }
    }


# ---------------------------------------------------------------------------
# Nested diagnostics and unknown content retention
# ---------------------------------------------------------------------------

NESTED_INT_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item">
          <xs:complexType>
            <xs:attribute name="id" type="xs:int"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""


@pytest.mark.parametrize(
    ("format", "data", "pointer"),
    [
        ("pyxsd", {"item": {"@id": True}}, "/item/@id"),
        (
            "badgerfish",
            {"root": {"item": {"@id": "bad"}}},
            "/root/item/@id",
        ),
        (
            "jsonml",
            ["root", ["item", {"id": "bad"}]],
            "/1/1/id",
        ),
    ],
)
def test_nested_conventional_scalar_errors_carry_the_full_pointer(format, data, pointer):
    schema = _schema(NESTED_INT_ATTRIBUTE_SCHEMA)

    with pytest.raises(PyXSDError, match="scalar-type-invalid") as caught:
        schema.from_data(data, format=format)

    assert pointer in str(caught.value)


def test_conventional_unknown_content_is_built_before_binding():
    schema = _schema(EMPTY_SCHEMA)

    native = data_to_element(schema, {"{urn:run}extra": "v"}, format="pyxsd")
    assert native.find("{urn:run}extra") is not None

    with pytest.warns(PyXSDWarning, match="name-ambiguous"):
        parker = data_to_element(schema, {"zzz": "v"}, format="parker", fidelity="best-effort")
    assert parker.find("zzz") is not None

    badgerfish = data_to_element(
        schema,
        {"root": {"@xmlns": {"p": "urn:run"}, "p:extra": "v"}},
        format="badgerfish",
    )
    assert badgerfish.find("{urn:run}extra") is not None

    jsonml = data_to_element(
        schema,
        ["root", ["p:extra", {"xmlns:p": "urn:run"}, "v"]],
        format="jsonml",
    )
    assert jsonml.find("{urn:run}extra") is not None


def test_import_warnings_carry_stable_paths():
    parker_schema = _schema(PARKER_LOCAL_COLLISION_SCHEMA)
    with pytest.warns(PyXSDWarning, match="name-ambiguous") as caught:
        parker_schema.from_data({"item": "x"}, format="parker", fidelity="best-effort")
    assert "'/item'" in str(caught[0].message)

    native_schema = _schema(SEQUENCE_REPEATED_CHOICE_SCHEMA)
    with pytest.warns(PyXSDWarning, match="order-ambiguous") as caught:
        native_schema.from_data({"b": ["x"], "a": [1]}, format="pyxsd", fidelity="best-effort")
    assert "at ''" in str(caught[0].message)

    mixed_schema = _schema(FIXTURES["mixed"][0])
    with pytest.warns(PyXSDWarning, match="mixed-content") as caught:
        mixed_schema.from_data({"$": "text", "i": "x"}, format="pyxsd", fidelity="best-effort")
    assert "at ''" in str(caught[0].message)

    qname_schema = _schema(QNAME_ROOT_SCHEMA)
    with pytest.warns(PyXSDWarning, match="namespace-unresolved") as caught:
        qname_schema.from_data("p:item", format="parker", fidelity="best-effort")
    assert "at ''" in str(caught[0].message)

    nullable = _schema(NATIVE_NULLABLE_OR_EMPTY_SCHEMA)
    with pytest.warns(PyXSDWarning, match="empty-nil-ambiguous") as caught:
        nullable.from_data(None, format="pyxsd", fidelity="best-effort")
    assert "at ''" in str(caught[0].message)


def test_native_always_list_round_trips_a_list_valued_child():
    schema = _schema(LIST_CHILD_SCHEMA)
    document = schema.parse(StringIO("<root><refs>a b</refs></root>"))
    exported = document.to_data(format="pyxsd", always_list=True)
    assert exported == {"refs": [["a", "b"]]}

    imported = schema.from_data(exported, format="pyxsd")

    assert imported.root is not None
    assert str(imported.root.refs) == "a b"
    assert imported.to_data(format="pyxsd", always_list=True) == exported


@pytest.mark.parametrize(
    ("format", "data"),
    [
        ("pyxsd", {"$": "2"}),
        ("parker", "2"),
    ],
)
def test_root_absorbed_formats_select_a_substitution_member_by_local_name(format, data):
    schema = _schema(SUBSTITUTION_SCHEMA)

    imported = schema.from_data(data, format=format, element="special")

    imported.require_valid()
    assert imported.root is not None
    assert imported.root._name_ == "special"


@pytest.mark.parametrize("format", ["badgerfish", "jsonml"])
def test_root_carrying_formats_reject_an_ambiguous_local_root_selection(format, tmp_path):
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
    data: JsonValue = (
        {"ns0:root": {"@xmlns": {"ns0": "urn:a"}, "$": "x"}}
        if format == "badgerfish"
        else ["ns0:root", {"xmlns:ns0": "urn:a"}, "x"]
    )

    with pytest.raises(PyXSDError, match="name-ambiguous"):
        schema.from_data(data, format=format, element="root")


def test_native_typed_false_fixture_round_trips_attributes_and_repeats():
    document = _compile(NATIVE_SCHEMA, NATIVE_INSTANCE)
    exported = document.to_data(format="pyxsd", typed=False)
    assert exported == {"@a": "1", "item": ["1", "2"], "note": "hi"}

    imported = document.schema.from_data(exported, format="pyxsd")

    imported.require_valid()
    assert int(imported.root.a) == 1
    assert [int(item) for item in imported.root.item] == [1, 2]
    assert imported.to_data(format="pyxsd", typed=False) == exported


def test_badgerfish_a_child_may_declare_the_prefix_in_its_own_name():
    schema = _schema(WILDCARD_GENERIC_SCHEMA)
    element = data_to_element(
        schema,
        {
            "root": {
                "p:item": {
                    "@xmlns": {"p": "urn:p"},
                    "q:leaf": {"@xmlns": {"q": "urn:q"}, "$": "v"},
                }
            }
        },
        format="badgerfish",
    )

    item = element.find("{urn:p}item")
    assert item is not None
    leaf = element.find("{urn:p}item/{urn:q}leaf")
    assert leaf is not None and leaf.text == "v"
    assert schema.namespace_context.bindings_for(item) == {"p": "urn:p"}
    assert schema.namespace_context.bindings_for(leaf) == {"p": "urn:p", "q": "urn:q"}


# ---------------------------------------------------------------------------
# Names occupying more than one content-model position
# ---------------------------------------------------------------------------

MULTI_POSITION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string" minOccurs="0"/>
        <xs:element name="b" type="xs:string"/>
        <xs:element name="a" type="xs:string" minOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""


def test_parker_rejects_a_name_at_multiple_sequence_positions():
    schema = _schema(MULTI_POSITION_SCHEMA)

    with pytest.raises(PyXSDError, match="order-ambiguous") as caught:
        schema.from_data({"b": "B", "a": "A"}, format="parker")
    assert str(caught.value).startswith("order-ambiguous at ''")

    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        imported = schema.from_data({"b": "B", "a": "A"}, format="parker", fidelity="best-effort")

    assert imported.root is not None
    assert [child._name_ for child in imported.root._children_] == ["b", "a"]
    imported.require_valid()


def test_native_rejects_a_name_at_multiple_sequence_positions():
    schema = _schema(MULTI_POSITION_SCHEMA)

    with pytest.raises(PyXSDError, match="order-ambiguous") as caught:
        schema.from_data({"a": ["x", "y"], "b": "B"}, format="pyxsd")
    assert str(caught.value).startswith("order-ambiguous at ''")

    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        imported = schema.from_data(
            {"a": ["x", "y"], "b": "B"}, format="pyxsd", fidelity="best-effort"
        )

    assert imported.root is not None
    assert [child._name_ for child in imported.root._children_] == ["a", "a", "b"]
    assert imported.report.has_errors


@pytest.mark.parametrize(
    ("format", "path", "expected"),
    [
        ("pyxsd", "", {"a": ["A", "C"], "b": "B"}),
        ("parker", "", {"a": ["A", "C"], "b": "B"}),
        (
            "badgerfish",
            "/root",
            {"root": {"a": ["A", "C"], "b": "B"}},
        ),
    ],
)
def test_split_position_export_reimports_only_with_an_order_warning(format, path, expected):
    schema = _schema(MULTI_POSITION_SCHEMA)
    document = schema.parse(StringIO("<root><a>A</a><b>B</b><a>C</a></root>"))
    document.require_valid()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PyXSDWarning)
        exported = document.to_data(format=format, fidelity="best-effort")
    assert exported == expected

    with pytest.raises(PyXSDError, match="order-ambiguous") as caught:
        schema.from_data(exported, format=format)
    assert str(caught.value).startswith(f"order-ambiguous at {path!r}")

    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        imported = schema.from_data(exported, format=format, fidelity="best-effort")

    assert imported.root is not None
    assert [child._name_ for child in imported.root._children_] == ["a", "a", "b"]
    assert imported.report.has_errors


@pytest.mark.parametrize("format", ["pyxsd", "parker", "badgerfish"])
@pytest.mark.parametrize("fidelity", ["strict", "best-effort"])
def test_repeatable_list_typed_child_array_is_ambiguous_in_every_mode(format, fidelity):
    schema = _schema(REPEATED_LIST_CHILD_SCHEMA)
    data: JsonValue = (
        {"root": {"refs": ["a", "b"]}} if format == "badgerfish" else {"refs": ["a", "b"]}
    )

    with pytest.raises(PyXSDError, match="container-invalid") as caught:
        schema.from_data(data, format=format, fidelity=fidelity)  # type: ignore[arg-type]

    assert "/refs" in str(caught.value)


def test_a_zero_occurrence_particle_does_not_create_an_extra_position():
    schema = _schema(
        """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
        <xs:element name="b" type="xs:string"/>
        <xs:element name="a" type="xs:string" minOccurs="0" maxOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""
    )

    imported = schema.from_data({"b": "B", "a": "A"}, format="pyxsd")

    imported.require_valid()
    assert [child._name_ for child in imported.root._children_] == ["a", "b"]


def test_badgerfish_rejects_a_name_at_multiple_sequence_positions():
    schema = _schema(MULTI_POSITION_SCHEMA)

    with pytest.raises(PyXSDError, match="order-ambiguous") as caught:
        schema.from_data({"root": {"b": "B", "a": "A"}}, format="badgerfish")
    assert str(caught.value).startswith("order-ambiguous at '/root'")

    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        imported = schema.from_data(
            {"root": {"b": "B", "a": "A"}},
            format="badgerfish",
            fidelity="best-effort",
        )

    assert imported.root is not None
    assert [child._name_ for child in imported.root._children_] == ["b", "a"]
    imported.require_valid()
