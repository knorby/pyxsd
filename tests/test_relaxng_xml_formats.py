"""RELAX NG native, Parker, BadgerFish, and JsonML export contracts.

The fixtures are compiled RELAX NG grammars, not synthetic element stand-ins,
so layouts, attributes, mixed content, namespaces, and scalar evidence come
from the same route planner production uses.
"""

from __future__ import annotations

import io
import json
import warnings
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

import pyxsd
from pyxsd.exceptions import PyXSDError, PyXSDWarning
from pyxsd.relaxng import AlternativeValue, RelaxNGDocument, RelaxNGSchema, xml_conversion
from pyxsd.relaxng.json_values import scalar_tag
from pyxsd.xsd_data_types import QName, qname_context

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"

INTEGER = '<data type="integer"/>'
STRING = '<data type="string"/>'
A = '<element name="a"><data type="integer"/></element>'
B = '<element name="b"><data type="string"/></element>'
MIXED_I = (
    '<mixed><zeroOrMore><element name="i"><data type="string"/></element></zeroOrMore></mixed>'
)


def schema_for(content: str, *, name: str = 'name="r"') -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" datatypeLibrary="{XSD}" {name}>{content}</element>')
    )
    assert not schema.report.has_errors, schema.report.issues
    return schema


def parse(
    content: str,
    xml: str = "<r/>",
    *,
    name: str = 'name="r"',
    typed: bool = True,
) -> RelaxNGDocument:
    document = schema_for(content, name=name).parse(io.StringIO(xml), typed=typed)
    assert isinstance(document, RelaxNGDocument)
    return document


def valid(
    content: str,
    xml: str = "<r/>",
    *,
    name: str = 'name="r"',
) -> RelaxNGDocument:
    document = parse(content, xml, name=name)
    assert not document.report.has_errors, document.report.issues
    return document


def warning_texts(record: list[Any]) -> list[str]:
    return [str(warning.message) for warning in record if isinstance(warning.message, PyXSDWarning)]


def conversion_error(code: str, pointer: str, action: Any) -> None:
    with pytest.raises(PyXSDError) as caught:
        action()
    message = str(caught.value)
    assert message.startswith(f"{code} at {pointer!r}: "), message


# ---------------------------------------------------------------------------
# Root retention, absorption, and scalar policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "xml", "raw", "native", "parker", "badgerfish", "jsonml"),
    [
        (INTEGER, "<r>12</r>", {"$": 12}, {"$": 12}, 12, {"r": 12}, ["r", 12]),
        (
            '<data type="boolean"/>',
            "<r>true</r>",
            {"$": True},
            {"$": True},
            True,
            {"r": True},
            ["r", True],
        ),
        (
            '<data type="decimal"/>',
            "<r>1.20</r>",
            {"$": Decimal("1.20")},
            {"$": "1.20"},
            "1.20",
            {"r": "1.20"},
            ["r", "1.20"],
        ),
        (STRING, "<r>x</r>", {"$": "x"}, {"$": "x"}, "x", {"r": "x"}, ["r", "x"]),
    ],
)
def test_all_formats_retain_or_absorb_the_root(
    content: str,
    xml: str,
    raw: Any,
    native: Any,
    parker: Any,
    badgerfish: Any,
    jsonml: Any,
) -> None:
    document = valid(content, xml)

    assert document.to_dict() == raw
    assert document.to_data() == native
    assert document.to_data(format="parker") == parker
    assert document.to_data(format="badgerfish") == badgerfish
    assert document.to_data(format="jsonml") == jsonml
    assert document.to_json(format="pyxsd") == json.dumps(native)
    assert json.loads(document.to_json(format="pyxsd")) == native
    assert json.loads(document.to_json(format="parker")) == parker
    assert json.loads(document.to_json(format="badgerfish")) == badgerfish
    assert json.loads(document.to_json(format="jsonml")) == jsonml


def test_native_raw_wrapper_and_json_safe_tag_are_distinct() -> None:
    document = valid(f'<choice>{INTEGER}<data type="string"/></choice>', "<r>12</r>")
    raw = document.to_dict()
    wrapper = raw["$"]

    assert isinstance(wrapper, AlternativeValue)
    assert wrapper.lexical == "12"
    assert document.to_data()["$"] == scalar_tag(wrapper)
    assert document.to_data(typed=False) == {"$": "12"}
    assert document.to_json() == json.dumps({"$": scalar_tag(wrapper)})


@pytest.mark.parametrize(
    ("content", "xml", "lexical"),
    [
        ("<text/>", "<r>12</r>", "12"),
        (f"<choice>{INTEGER}{STRING}</choice>", "<r>12</r>", "12"),
        (f"<choice>{INTEGER}{STRING}</choice>", "<r>word</r>", "word"),
    ],
)
def test_conventional_formats_keep_ambiguous_or_nondecodable_lexical_text(
    content: str, xml: str, lexical: str
) -> None:
    document = valid(content, xml)

    assert document.to_data(format="parker") == lexical
    assert document.to_data(format="badgerfish") == {"r": lexical}
    assert document.to_data(format="jsonml") == ["r", lexical]
    assert document.to_data(typed=False) == {"$": lexical}


def test_unique_scalars_use_ordinary_values_not_lexical_strings() -> None:
    decimal = valid('<data type="decimal"/>', "<r>1.20</r>")
    boolean = valid('<data type="boolean"/>', "<r>true</r>")
    date = valid('<data type="date"/>', "<r>2026-01-02</r>")

    assert decimal.to_data() == {"$": "1.20"}
    assert boolean.to_data() == {"$": True}
    assert date.to_data() == {"$": "2026-01-02"}
    assert decimal.to_data(typed=False) == {"$": "1.20"}
    assert boolean.to_data(typed=False) == {"$": "true"}
    assert date.to_data(typed=False) == {"$": "2026-01-02"}


def test_binary_values_fall_back_to_their_lexical_text() -> None:
    document = valid('<data type="base64Binary"/>', "<r>AQI=</r>")
    raw = document.to_dict()["$"]

    assert raw == b"\x01\x02"
    assert document.to_data() == {"$": "AQI="}
    assert document.to_data(format="parker") == "AQI="
    assert document.to_data(format="badgerfish") == {"r": "AQI="}
    assert document.to_data(format="jsonml") == ["r", "AQI="]


def test_list_values_keep_their_declared_array_shapes() -> None:
    integers = valid('<data type="IDREFS"/>', "<r>a b</r>")

    assert integers.to_data(format="parker") == ["a", "b"]
    assert integers.to_data(format="badgerfish") == {"r": ["a", "b"]}
    assert integers.to_data(format="jsonml") == ["r", ["a", "b"]]


# ---------------------------------------------------------------------------
# Attributes, empty content, and always_list
# ---------------------------------------------------------------------------


def test_attributes_and_empty_content_across_formats() -> None:
    with_attribute = valid(
        '<optional><attribute name="z"><data type="integer"/></attribute></optional><empty/>',
        '<r z="3"/>',
    )
    empty = valid("<empty/>")
    scalar_empty = valid(STRING)

    assert with_attribute.to_dict() == {"@z": 3}
    assert with_attribute.to_data() == {"@z": 3}
    assert with_attribute.to_data(format="badgerfish") == {"r": {"@z": 3}}
    assert with_attribute.to_data(format="jsonml") == ["r", {"z": 3}]
    conversion_error(
        "attributes-lost",
        "",
        lambda: with_attribute.to_data(format="parker", fidelity="strict"),
    )
    with pytest.warns(PyXSDWarning, match="attributes-lost"):
        assert with_attribute.to_data(format="parker") == {}

    assert empty.to_dict() == {"$": None}
    assert empty.to_data() == {"$": None}
    assert empty.to_data(typed=False) == {"$": ""}
    assert empty.to_data(format="parker") == {}
    assert empty.to_data(format="badgerfish") == {"r": {}}
    assert empty.to_data(format="jsonml") == ["r"]

    assert scalar_empty.to_data() == {"$": ""}
    assert scalar_empty.to_data(format="parker") == ""
    assert scalar_empty.to_data(format="badgerfish") == {"r": ""}
    assert scalar_empty.to_data(format="jsonml") == ["r", ""]


def test_native_always_list_wraps_every_child_group() -> None:
    document = valid(
        f"<zeroOrMore>{A}</zeroOrMore>",
        "<r><a>1</a></r>",
    )
    two = valid(f"<zeroOrMore>{A}</zeroOrMore>", "<r><a>1</a><a>2</a></r>")

    assert document.to_dict() == {"a": 1}
    assert document.to_dict(always_list=True) == {"a": [1]}
    assert document.to_data(always_list=True) == {"a": [1]}
    assert two.to_dict() == {"a": [1, 2]}
    assert two.to_dict(always_list=True) == {"a": [1, 2]}


def test_parker_arrays_only_for_observed_repeats_badgerfish_uses_cardinality() -> None:
    two = valid(f"<zeroOrMore>{A}</zeroOrMore>", "<r><a>1</a><a>2</a></r>")
    one = valid(f"<zeroOrMore>{A}</zeroOrMore>", "<r><a>1</a></r>")
    empty = valid(f"<zeroOrMore>{A}</zeroOrMore>")

    assert two.to_data(format="parker") == {"a": [1, 2]}
    assert one.to_data(format="parker") == {"a": 1}
    assert empty.to_data(format="parker") == {}
    assert two.to_data(format="badgerfish") == {"r": {"a": [1, 2]}}
    assert one.to_data(format="badgerfish") == {"r": {"a": [1]}}
    assert empty.to_data(format="badgerfish") == {"r": {}}
    assert two.to_data(format="jsonml") == ["r", ["a", 1], ["a", 2]]
    assert one.to_data(format="jsonml") == ["r", ["a", 1]]
    assert empty.to_data(format="jsonml") == ["r"]


def test_interleave_is_a_record_and_jsonml_keeps_document_order() -> None:
    document = valid(f"<interleave>{A}{B}</interleave>", "<r><b>B</b><a>1</a></r>")

    assert document.to_dict() == {"b": "B", "a": 1}
    assert document.to_data(format="parker") == {"a": 1, "b": "B"}
    assert document.to_data(format="badgerfish") == {"r": {"a": 1, "b": "B"}}
    assert document.to_data(format="jsonml") == ["r", ["b", "B"], ["a", 1]]


@pytest.mark.parametrize(
    ("format", "pointer"),
    [("pyxsd", "/a"), ("parker", ""), ("badgerfish", "/r")],
)
def test_strict_grouped_formats_reject_unreconstructable_interleaving(
    format: str, pointer: str
) -> None:
    content = f"<zeroOrMore><choice>{A}{B}</choice></zeroOrMore>"
    document = valid(content, "<r><a>1</a><b>x</b><a>2</a></r>")

    conversion_error(
        "order-ambiguous",
        pointer,
        lambda: document.to_data(format=format, fidelity="strict"),
    )


def test_best_effort_groups_interleaved_children() -> None:
    content = f"<zeroOrMore><choice>{A}{B}</choice></zeroOrMore>"
    document = valid(content, "<r><a>1</a><b>x</b><a>2</a></r>")

    assert document.to_data(format="pyxsd") == {"a": [1, 2], "b": "x"}
    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        assert document.to_data(format="parker") == {"a": [1, 2], "b": "x"}
    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        assert document.to_data(format="badgerfish") == {"r": {"a": [1, 2], "b": ["x"]}}
    assert document.to_data(format="jsonml") == [
        "r",
        ["a", 1],
        ["b", "x"],
        ["a", 2],
    ]


# ---------------------------------------------------------------------------
# Mixed content and text/tail positions
# ---------------------------------------------------------------------------


def test_mixed_content_positions_and_tails() -> None:
    document = valid(MIXED_I, "<r>a<i>x</i>b<i>y</i>c</r>")

    assert document.to_data() == {"i": ["x", "y"], "$": "abc"}
    with pytest.raises(PyXSDError, match="mixed-content"):
        document.to_data(format="pyxsd", fidelity="strict")
    with pytest.warns(PyXSDWarning, match="mixed-content"):
        assert document.to_data(format="parker") == {"$": "abc", "i": ["x", "y"]}
    with pytest.warns(PyXSDWarning, match="mixed-content"):
        assert document.to_data(format="badgerfish") == {"r": {"$": "abc", "i": ["x", "y"]}}
    assert document.to_data(format="jsonml") == [
        "r",
        "a",
        ["i", "x"],
        "b",
        ["i", "y"],
        "c",
    ]


def test_text_only_mixed_content_is_a_scalar() -> None:
    document = valid(MIXED_I, "<r>text</r>")

    assert document.to_data(format="parker", fidelity="strict") == "text"
    assert document.to_data(format="badgerfish", fidelity="strict") == {"r": "text"}
    assert document.to_data(format="jsonml") == ["r", "text"]
    assert document.to_data() == {"$": scalar_tag(document.root.scalar_value())}


def test_empty_mixed_content_stays_empty_in_grouped_formats() -> None:
    document = valid(MIXED_I, "<r/>")

    assert document.to_data(format="parker", fidelity="strict") == {}
    assert document.to_data(format="badgerfish", fidelity="strict") == {"r": {}}
    assert document.to_data(format="jsonml") == ["r"]
    assert document.to_data(typed=False) == {"$": ""}


def test_mixed_distinct_child_names_lose_order_strictly() -> None:
    document = valid(
        '<mixed><zeroOrMore><choice><element name="i"><data type="string"/></element>'
        '<element name="b"><data type="string"/></element></choice></zeroOrMore></mixed>',
        "<r><i>x</i><b>y</b></r>",
    )

    conversion_error(
        "order-ambiguous",
        "",
        lambda: document.to_data(format="parker", fidelity="strict"),
    )
    conversion_error(
        "order-ambiguous",
        "/r",
        lambda: document.to_data(format="badgerfish", fidelity="strict"),
    )
    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        assert document.to_data(format="parker") == {"i": "x", "b": "y"}
    with pytest.warns(PyXSDWarning, match="order-ambiguous"):
        assert document.to_data(format="badgerfish") == {"r": {"i": ["x"], "b": ["y"]}}
    assert document.to_data(format="jsonml") == ["r", ["i", "x"], ["b", "y"]]


# ---------------------------------------------------------------------------
# Namespaces, prefixes, QNames, and ordinary XSI data
# ---------------------------------------------------------------------------


def test_namespaced_elements_and_attributes() -> None:
    document = valid(
        '<element name="child" ns="urn:a"><data type="string"/></element>'
        '<attribute name="lang" ns="http://www.w3.org/XML/1998/namespace">'
        '<data type="string"/></attribute>',
        '<r xmlns="urn:a" xml:lang="en"><child>v</child></r>',
        name='name="r" ns="urn:a"',
    )

    assert document.to_dict() == {
        "{urn:a}child": "v",
        "@{http://www.w3.org/XML/1998/namespace}lang": "en",
    }
    conversion_error(
        "namespace-lost",
        "/child",
        lambda: document.to_data(format="parker", fidelity="strict"),
    )
    with pytest.warns(PyXSDWarning, match="namespace-lost"):
        assert document.to_data(format="parker") == {"child": "v"}

    badgerfish = document.to_data(format="badgerfish")
    assert list(badgerfish) == ["ns0:r"]
    assert badgerfish["ns0:r"]["@xmlns"] == {"ns0": "urn:a"}
    assert badgerfish["ns0:r"]["@xml:lang"] == "en"
    assert badgerfish["ns0:r"]["ns0:child"] == "v"

    assert document.to_data(format="jsonml") == [
        "ns0:r",
        {"xmlns:ns0": "urn:a", "xml:lang": "en"},
        ["ns0:child", "v"],
    ]


@pytest.mark.parametrize(
    "name_class",
    [
        "<anyName/>",
        '<nsName ns="urn:wild"/>',
        "<anyName><except><name>skip</name></except></anyName>",
    ],
)
def test_wildcard_name_classes_are_known_prefix_seeds(name_class: str) -> None:
    document = valid(
        f'<zeroOrMore><element>{name_class}<data type="string"/></element></zeroOrMore>',
        '<r xmlns:w="urn:wild"><w:item>v</w:item></r>',
    )
    prefix = "ns0" if "nsName" in name_class else "ns_" + b"urn:wild".hex()

    assert document.to_data(format="badgerfish") == {
        "r": {"@xmlns": {prefix: "urn:wild"}, f"{prefix}:item": "v"}
    }
    assert document.to_data(format="jsonml") == [
        "r",
        {"xmlns:" + prefix: "urn:wild"},
        [f"{prefix}:item", "v"],
    ]


def test_xsi_attributes_are_ordinary_data_and_never_synthesize_nil() -> None:
    document = valid(
        f'<optional><attribute name="nil" ns="{XSI}"><data type="string"/></attribute>'
        f'</optional><optional><attribute name="type" ns="{XSI}">'
        '<data type="string"/></attribute></optional><empty/>',
        f'<r xmlns:xsi="{XSI}" xsi:nil="true" xsi:type="t:Thing" xmlns:t="urn:types"/>',
    )

    native = document.to_dict()
    assert native == {
        f"@{{{XSI}}}nil": "true",
        f"@{{{XSI}}}type": "t:Thing",
    }
    assert "@xsi:nil" not in native
    badgerfish = document.to_data(format="badgerfish")
    declarations = badgerfish["r"]["@xmlns"]
    type_key = next(key for key in badgerfish["r"] if key.endswith(":nil"))
    assert badgerfish["r"][type_key] == "true"
    assert any(uri == XSI for uri in declarations.values())
    jsonml = document.to_data(format="jsonml")
    assert jsonml[0] == "r"
    assert set(jsonml[1].values()) >= {"true", "t:Thing"}


def test_qname_values_keep_namespace_bindings_in_namespace_capable_formats() -> None:
    document = valid(
        '<data type="QName"/>',
        '<r xmlns:p="urn:example">p:v</r>',
    )
    prefix = "ns_" + b"urn:example".hex()

    conversion_error(
        "namespace-lost",
        "/$",
        lambda: document.to_data(format="pyxsd", fidelity="strict"),
    )
    assert document.to_data() == {"$": "p:v"}
    conversion_error(
        "namespace-lost",
        "",
        lambda: document.to_data(format="parker", fidelity="strict"),
    )
    with pytest.warns(PyXSDWarning, match="namespace-lost"):
        assert document.to_data(format="parker") == "p:v"

    assert document.to_data(format="badgerfish") == {
        "r": {"@xmlns": {prefix: "urn:example"}, "$": f"{prefix}:v"}
    }
    assert document.to_data(format="jsonml") == [
        "r",
        {"xmlns:" + prefix: "urn:example"},
        f"{prefix}:v",
    ]


def test_unqualified_qname_values_need_no_declaration() -> None:
    document = valid('<data type="QName"/>', "<r>v</r>")

    assert document.to_data(format="parker", fidelity="strict") == "v"
    assert document.to_data(format="badgerfish") == {"r": "v"}
    assert document.to_data(format="jsonml") == ["r", "v"]


def test_unresolved_qname_helpers_report_the_expected_codes() -> None:
    value = QName("p:v")

    conversion_error(
        "namespace-lost",
        "/x",
        lambda: xml_conversion._parker_qname(value, fidelity="strict", path=("x",)),
    )
    conversion_error(
        "namespace-unresolved",
        "/x",
        lambda: xml_conversion._lexical_qname(
            value, path=("x",), names=xml_conversion._LexicalNames([])
        ),
    )
    conversion_error(
        "namespace-lost",
        "/$",
        lambda: xml_conversion._check_native_qnames(value, ("$",), mode="json-safe"),
    )
    assert xml_conversion._reserve_value_names(value, xml_conversion._LexicalNames([])) is None


# ---------------------------------------------------------------------------
# JsonML for invalid, dirty, generic, and attribute-light trees
# ---------------------------------------------------------------------------


def test_jsonml_exports_invalid_and_dirty_trees_lexically() -> None:
    invalid = parse(INTEGER, "<r>oops</r>")
    assert invalid.report.has_errors
    assert invalid.to_data(format="jsonml") == ["r", "oops"]

    dirty = valid(INTEGER, "<r>12</r>")
    dirty.root.set_text("13")
    assert dirty.interpretations_current is False
    assert dirty.to_data(format="jsonml") == ["r", "13"]
    assert dirty.to_data(format="pyxsd", typed=False) == {"$": "13"}


def test_jsonml_preserves_explicit_empty_text_and_stale_tails() -> None:
    document = valid(STRING, "<r>x</r>")
    document.root.set_text("")
    document.root.set_tail("outside")

    assert document.to_data(format="jsonml") == ["r", ""]


def test_jsonml_keeps_the_current_names_after_a_dirty_rename() -> None:
    document = valid(INTEGER, "<r>12</r>")
    document.root.set_name("other")

    assert document.to_data(format="jsonml") == ["other", "12"]
    assert document.to_data(format="pyxsd", typed=False) == {"$": "12"}


def test_jsonml_merges_root_declarations_before_existing_attributes() -> None:
    document = valid(
        '<optional><attribute name="id">' + STRING + "</attribute></optional><empty/>",
        '<r xmlns="urn:a" id="r"/>',
        name='name="r" ns="urn:a"',
    )

    assert document.to_data(format="jsonml") == [
        "ns0:r",
        {"xmlns:ns0": "urn:a", "id": "r"},
    ]


def test_jsonml_reserves_qname_names_before_root_declaration_shift() -> None:
    document = valid(
        '<data type="QName"/>',
        '<r xmlns:p="urn:example">p:v</r>',
    )
    prefix = "ns_" + b"urn:example".hex()

    assert document.to_data(format="jsonml") == [
        "r",
        {"xmlns:" + prefix: "urn:example"},
        f"{prefix}:v",
    ]


def test_native_typed_false_is_lexical_even_for_ambiguous_values() -> None:
    document = valid('<choice><text/><data type="string"/></choice>', "<r> 12 </r>")

    assert document.to_data(typed=False) == {"$": " 12 "}
    assert document.to_dict(typed=False) == {"$": " 12 "}
    assert document.to_data()["$"]["#value"]["candidates"]


# ---------------------------------------------------------------------------
# Strict container errors and generic fallbacks
# ---------------------------------------------------------------------------


def test_grouped_formats_reject_content_their_layout_cannot_carry() -> None:
    empty = valid("<empty/>")
    empty.root.append_child(type(empty.root)._new("x", empty.root.revision))

    conversion_error(
        "container-invalid",
        "",
        lambda: xml_conversion.export_parker(empty, fidelity="strict"),
    )
    conversion_error(
        "container-invalid",
        "/r",
        lambda: xml_conversion.export_badgerfish(empty, fidelity="strict"),
    )

    scalar_children = valid(STRING)
    scalar_children.root.append_child(
        type(scalar_children.root)._new("x", scalar_children.root.revision)
    )
    conversion_error(
        "container-invalid",
        "",
        lambda: xml_conversion.export_parker(scalar_children, fidelity="strict"),
    )
    conversion_error(
        "container-invalid",
        "/r",
        lambda: xml_conversion.export_badgerfish(scalar_children, fidelity="strict"),
    )


def test_element_only_non_whitespace_text_is_a_container_error() -> None:
    document = valid(f"<optional>{A}</optional>")
    document.root.set_text("oops")

    conversion_error(
        "container-invalid",
        "",
        lambda: xml_conversion.export_parker(document, fidelity="strict"),
    )
    conversion_error(
        "container-invalid",
        "/r",
        lambda: xml_conversion.export_badgerfish(document, fidelity="strict"),
    )


def test_native_leaf_empty_and_scalar_layout_conventions() -> None:
    empty = valid("<empty/>")
    optional = valid(f"<optional>{A}</optional>")
    scalar = valid(STRING)

    assert empty.to_dict() == {"$": None}
    assert optional.to_dict() == {"$": None}
    assert scalar.to_dict() == {"$": ""}
    assert empty.to_dict(typed=False) == {"$": ""}


# ---------------------------------------------------------------------------
# XSD output remains untouched
# ---------------------------------------------------------------------------


XSD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" type="xs:int"/>
      </xs:sequence>
      <xs:attribute name="a" type="xs:int"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""


def test_xsd_conventional_output_is_unchanged() -> None:
    document = pyxsd.Schema.compile(io.StringIO(XSD_SCHEMA)).parse(
        io.StringIO('<root a="1"><item>2</item></root>')
    )

    assert document.to_dict() == {"@a": 1, "item": 2}
    assert document.to_data() == {"@a": 1, "item": 2}
    with pytest.warns(PyXSDWarning, match="attributes-lost"):
        assert document.to_data(format="parker") == {"item": 2}
    assert document.to_data(format="badgerfish", fidelity="strict") == {
        "root": {"@a": 1, "item": 2}
    }
    assert document.to_data(format="jsonml") == ["root", {"a": 1}, ["item", 2]]
    assert document.to_data(format="pyxsd-json") == {"root": [{"@a": 1}, {"item": 2}]}


# ---------------------------------------------------------------------------
# Defensive and schema-shape edge coverage
# ---------------------------------------------------------------------------


def test_unsupported_adapter_error_remains_an_explicit_type() -> None:
    from pyxsd.relaxng.conversion import RelaxNGUnsupportedAdapterError

    error = RelaxNGUnsupportedAdapterError("parker")

    assert error.format == "parker"
    assert "not implemented" in str(error)


def test_root_route_helper_keeps_the_required_and_lexical_modes() -> None:
    document = valid("<empty/>")
    document.root.set_name("other")

    with pytest.raises(PyXSDError, match="root"):
        xml_conversion._root_or_none(document, require=True)
    assert xml_conversion._root_or_none(document, require=False) is None


def test_grouped_order_helper_handles_empty_and_reordered_plans() -> None:
    assert xml_conversion._check_grouped_order(None, (), (), "strict") is None

    document = valid(f"{A}{B}", "<r><a>1</a><b>x</b></r>")
    plan = document.schema.projection
    route = plan.routes[plan.roots[0]]
    reordered = dict(reversed(list(route.children.items())))
    bad_route = replace(route, children=reordered)

    conversion_error(
        "order-ambiguous",
        "",
        lambda: xml_conversion._check_grouped_order(
            bad_route, document.root.children, (), "strict"
        ),
    )


def test_scalar_helper_defensive_branches() -> None:
    assert xml_conversion._needs_lexical_fallback([b"x"]) is True
    assert xml_conversion._needs_lexical_fallback((1, 2)) is False
    assert xml_conversion._native_json_scalar(None, "x", ()) is None
    conversion_error(
        "scalar-type-invalid",
        "/x",
        lambda: xml_conversion._native_json_scalar(object(), "x", ("x",)),
    )

    ambiguous_list = valid(
        '<list><choice><data type="integer"/><data type="string"/></choice></list>',
        "<r>12</r>",
    )
    value = ambiguous_list.root.scalar_value()
    interpretation = value.interpretations[0]
    assert (
        xml_conversion._conventional_scalar(
            interpretation,
            "12",
            format="parker",
            fidelity="strict",
            path=(),
        )
        == "12"
    )
    assert (
        xml_conversion._conventional_scalar(
            [b"x"],
            "lexical",
            format="parker",
            fidelity="strict",
            path=(),
        )
        == "lexical"
    )
    conversion_error(
        "scalar-type-invalid",
        "/x",
        lambda: xml_conversion._conventional_scalar(
            object(),
            "lexical",
            format="parker",
            fidelity="strict",
            path=("x",),
        ),
    )


def test_parker_qname_best_effort_returns_the_original_lexical() -> None:
    with pytest.warns(PyXSDWarning, match="namespace-lost"):
        assert (
            xml_conversion._parker_qname(QName("p:v"), fidelity="best-effort", path=("x",)) == "p:v"
        )


def test_name_choice_root_seeds_prefixes() -> None:
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}"><choice><name>r</name><name>s</name></choice><empty/></element>'
        )
    )
    document = schema.parse(io.StringIO("<r/>"), typed=True)
    assert not document.report.has_errors

    assert document.to_data(format="badgerfish") == {"r": {}}
    assert document.to_data(format="jsonml") == ["r"]


@pytest.mark.parametrize(
    ("content", "xml"),
    [
        (
            '<list><oneOrMore><data type="QName"/></oneOrMore></list>',
            '<r xmlns:p="urn:example">p:a p:b</r>',
        ),
        (
            '<choice><data type="QName"/><data type="string"/></choice>',
            '<r xmlns:p="urn:example">p:v</r>',
        ),
        (
            '<list><choice><data type="QName"/><data type="string"/></choice></list>',
            '<r xmlns:p="urn:example">p:v</r>',
        ),
    ],
)
def test_qname_wrappers_and_lists_are_scanned_without_decoding(content: str, xml: str) -> None:
    document = valid(content, xml)

    if content.startswith("<list><oneOrMore>"):
        conversion_error(
            "namespace-lost",
            "/$",
            lambda: document.to_data(format="pyxsd", fidelity="strict"),
        )
    else:
        native = document.to_data(format="pyxsd", fidelity="strict")
        assert native["$"]["#value"]["context"]
    expected_prefix = (
        "ns_" + b"urn:example".hex() if content.startswith("<list><oneOrMore>") else "p"
    )
    assert document.to_data(format="jsonml")[1] == {"xmlns:" + expected_prefix: "urn:example"}
    assert document.to_data(format="badgerfish")["r"]["@xmlns"] == {expected_prefix: "urn:example"}
    with pytest.warns(PyXSDWarning, match="namespace-lost"):
        assert document.to_data(format="parker") in ("p:a p:b", "p:v", ["p:a", "p:b"])


def test_native_strict_recurses_through_child_only_mixed_content() -> None:
    document = valid(MIXED_I, "<r><i>x</i></r>")

    assert document.to_data(format="pyxsd", fidelity="strict") == {"i": "x"}
    assert document.to_dict() == {"i": "x"}


def test_native_strict_checks_attributes_and_nested_objects() -> None:
    attributed = valid(
        '<attribute name="a"><data type="integer"/></attribute>'
        '<element name="child"><optional><attribute name="id"><data type="string"/>'
        '</attribute></optional><element name="leaf"><data type="string"/></element>'
        "</element>",
        '<r a="1"><child id="c"><leaf>x</leaf></child></r>',
    )

    assert attributed.to_data(format="pyxsd", fidelity="strict") == {
        "@a": 1,
        "child": {"@id": "c", "leaf": "x"},
    }
    assert attributed.to_dict() == {
        "@a": 1,
        "child": {
            "@id": "c",
            "leaf": "x",
        },
    }


def test_native_typed_false_attributes_and_invalid_text() -> None:
    document = valid(
        '<attribute name="a"><data type="integer"/></attribute>'
        '<optional><element name="item"><data type="string"/></element></optional>',
        '<r a="1"><item>x</item></r>',
    )
    document.root.set_text("changed")

    assert document.to_data(typed=False) == {"@a": "1", "item": "x", "$": "changed"}
    document.root.set_text("   ")
    assert "$" not in document.to_data(typed=False)


def test_native_mixed_text_pieces_handle_absent_and_empty_positions() -> None:
    tail_only = valid(MIXED_I, "<r><i>x</i>tail</r>")
    text_only = valid(MIXED_I, "<r>intro<i>x</i></r>")
    dirty = valid(MIXED_I, "<r><i>x</i></r>")
    dirty.root.set_text("")

    assert tail_only.to_data() == {"i": "x", "$": "tail"}
    assert text_only.to_data() == {"i": "x", "$": "intro"}
    assert dirty.to_data(typed=False) == {"i": "x", "$": ""}


def test_parker_name_ambiguity_after_namespace_warnings() -> None:
    document = valid(
        '<interleave><element name="item" ns="urn:a"><data type="string"/></element>'
        '<element name="item" ns="urn:b"><data type="string"/></element></interleave>',
        '<r xmlns:a="urn:a" xmlns:b="urn:b"><b:item>B</b:item><a:item>A</a:item></r>',
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert document.to_data(format="parker") == {"item": ["B", "A"]}

    texts = warning_texts(caught)
    assert any("namespace-lost" in text for text in texts)
    assert any("name-ambiguous" in text and "at '/item'" in text for text in texts)


def test_parker_and_badgerfish_helpers_handle_no_route_and_lexical_mode() -> None:
    document = valid(
        '<attribute name="a"><data type="integer"/></attribute>'
        '<optional><element name="item"><data type="string"/></element></optional>',
        '<r a="1"><item>x</item></r>',
    )
    plan = document.schema.projection

    assert xml_conversion._parker_children(
        document, document.root, None, plan, (), fidelity="strict"
    ) == {"item": "x"}
    names = xml_conversion._LexicalNames([])
    assert xml_conversion._badgerfish_children(
        document,
        document.root,
        None,
        plan,
        ("r",),
        fidelity="strict",
        names=names,
    ) == {"item": "x"}
    assert xml_conversion._badgerfish_attributes(
        document, document.root, None, ("r",), names=names
    ) == {"@a": 1}

    validation_only = schema_for(STRING).parse(io.StringIO("<r/>"), typed=False)
    assert xml_conversion.export_parker(validation_only, fidelity="strict") == ""
    assert xml_conversion.export_badgerfish(validation_only, fidelity="strict") == {"r": ""}


def test_badgerfish_scalar_with_attributes_and_mixed_text_only() -> None:
    scalar = valid(
        '<attribute name="a"><data type="integer"/></attribute>' + STRING,
        '<r a="3">x</r>',
    )
    mixed = valid(
        '<group><attribute name="id"><data type="string"/></attribute>'
        '<mixed><zeroOrMore><element name="i"><data type="string"/></element>'
        "</zeroOrMore></mixed></group>",
        '<r id="p">text</r>',
    )

    assert scalar.to_data(format="badgerfish") == {"r": {"@a": 3, "$": "x"}}
    assert mixed.to_data(format="badgerfish") == {"r": {"@id": "p", "$": "text"}}


def test_jsonml_dirty_attributes_and_qname_lists_reserve_names() -> None:
    dirty = valid(
        '<attribute name="a"><data type="integer"/></attribute><empty/>',
        '<r a="3"/>',
    )
    dirty.root.set_attribute("a", "4")
    assert dirty.to_data(format="jsonml") == ["r", {"a": "4"}]

    qnames = valid(
        '<list><oneOrMore><data type="QName"/></oneOrMore></list>',
        '<r xmlns:p="urn:example">p:a p:b</r>',
    )
    prefix = "ns_" + b"urn:example".hex()
    assert qnames.to_data(format="jsonml") == [
        "r",
        {"xmlns:" + prefix: "urn:example"},
        [f"{prefix}:a", f"{prefix}:b"],
    ]


def test_name_class_helper_walks_choices_and_open_classes() -> None:
    from pyxsd.relaxng.names import AnyName, Name, NameChoice

    choice = NameChoice((Name("urn:a", "item"), Name(None, "item")))
    assert xml_conversion._name_class_names(choice) == ["{urn:a}item", "item"]
    assert xml_conversion._name_class_names(AnyName()) == []


def test_native_strict_lexical_mode_skips_typed_qname_checks() -> None:
    document = valid(MIXED_I, "<r>intro<i>x</i></r>")
    document.root.set_text("intro")

    conversion_error(
        "mixed-content",
        "",
        lambda: document.to_data(format="pyxsd", fidelity="strict", typed=False),
    )
    assert valid(INTEGER, "<r>12</r>").to_data(format="pyxsd", fidelity="strict", typed=False) == {
        "$": "12"
    }
    assert valid(f"{A}{B}", "<r><a>1</a><b>x</b></r>").to_data(
        format="pyxsd", fidelity="strict", typed=False
    ) == {"a": "1", "b": "x"}
    with qname_context({}):
        unqualified = QName("v")
    assert xml_conversion._check_native_qnames(unqualified, (), mode="json-safe") is None
    assert xml_conversion._check_native_qnames(QName("p:v"), (), mode="raw") is None


def test_wrapper_qname_declarations_keep_original_prefixes_and_conflicts() -> None:
    unprefixed = valid(
        '<choice><data type="QName"/><data type="string"/></choice>',
        "<r>a</r>",
    )
    assert xml_conversion._lexical_qname_prefixes(unprefixed.root.scalar_value()) == []

    document = valid(
        '<list><choice><data type="QName"/><data type="string"/></choice></list>',
        '<r xmlns:p="urn:example">p:v</r>',
    )
    wrapper = document.root.scalar_value()

    assert xml_conversion._lexical_qname_prefixes(wrapper) == [("p", "urn:example")]
    assert (
        xml_conversion._conventional_scalar(
            wrapper,
            wrapper.lexical,
            format="badgerfish",
            fidelity="strict",
            path=(),
            names=None,
        )
        == wrapper.lexical
    )
    assert (
        xml_conversion._conventional_scalar(
            wrapper.interpretations[0],
            wrapper.lexical,
            format="badgerfish",
            fidelity="strict",
            path=(),
            names=xml_conversion._LexicalNames([]),
        )
        == wrapper.lexical
    )
    assert (
        xml_conversion._conventional_scalar(
            wrapper.interpretations[0],
            wrapper.lexical,
            format="badgerfish",
            fidelity="strict",
            path=(),
            names=None,
        )
        == wrapper.lexical
    )
    assert document.to_data(format="badgerfish") == {
        "r": {"@xmlns": {"p": "urn:example"}, "$": "p:v"}
    }
    assert document.to_data(format="jsonml") == [
        "r",
        {"xmlns:p": "urn:example"},
        "p:v",
    ]

    prefixed = valid(
        '<choice><data type="QName"/><data type="string"/></choice>',
        '<r xmlns:p="urn:example">p:v</r>',
    ).root.scalar_value()
    missing = replace(prefixed, context=type(prefixed.context)(None, {}))
    assert xml_conversion._lexical_qname_prefixes(missing) == []
    assert xml_conversion._parker_wrapper_qnames(missing, fidelity="strict", path=()) is None

    conflict = valid(
        '<zeroOrMore><element name="item"><choice><data type="QName"/>'
        '<data type="string"/></choice></element></zeroOrMore>',
        '<r><item xmlns:p="urn:a">p:v</item><item xmlns:p="urn:b">p:v</item></r>',
    )
    conversion_error(
        "namespace-conflict",
        "/r/item/1",
        lambda: xml_conversion.export_badgerfish(conflict, fidelity="strict"),
    )
    conversion_error(
        "namespace-conflict",
        "",
        lambda: xml_conversion.export_jsonml(conflict, fidelity="strict"),
    )


def test_lexical_name_declaration_conflicts() -> None:
    names = xml_conversion._LexicalNames([])
    assert names.declare("xml", XML_NS, ()) is None
    conversion_error(
        "namespace-conflict",
        "",
        lambda: names.declare("xml", "urn:wrong", ()),
    )
    names.declare("p", "urn:a", ())
    conversion_error(
        "namespace-conflict",
        "",
        lambda: names.declare("p", "urn:b", ()),
    )

    generated = xml_conversion._LexicalNames(["{urn:a}item"])
    generated.lexical("{urn:a}item")
    conversion_error(
        "namespace-conflict",
        "",
        lambda: generated.declare("ns0", "urn:b", ()),
    )

    late = xml_conversion._LexicalNames(["{urn:a}item"])
    late.declare("ns0", "urn:b", ())
    late.lexical("{urn:a}item")
    conversion_error("namespace-conflict", "", late.declarations)


def test_ambiguous_unprefixed_qname_keeps_the_default_namespace() -> None:
    document = valid(
        '<zeroOrMore><element name="item" ns="urn:example"><choice>'
        '<data type="QName"/><data type="string"/></choice></element></zeroOrMore>',
        '<r><item xmlns="urn:example">v</item></r>',
    )

    assert document.to_data(format="badgerfish") == {
        "r": {
            "@xmlns": {"": "urn:example", "ns0": "urn:example"},
            "ns0:item": ["v"],
        }
    }
    assert document.to_data(format="jsonml") == [
        "r",
        {"xmlns": "urn:example", "xmlns:ns0": "urn:example"},
        ["ns0:item", "v"],
    ]
    with pytest.warns(PyXSDWarning, match="default namespace"):
        assert document.to_data(format="parker") == {"item": "v"}


def test_jsonml_handles_an_open_wildcard_route() -> None:
    document = valid(
        '<zeroOrMore><element><anyName/><data type="integer"/></element></zeroOrMore>',
        "<r><x>12</x></r>",
    )

    assert document.to_data(format="jsonml") == ["r", ["x", 12]]
    assert document.to_data() == {"x": 12}
