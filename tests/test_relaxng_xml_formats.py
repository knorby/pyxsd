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
from itertools import permutations, product
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
    [("pyxsd", "/a/1"), ("parker", ""), ("badgerfish", "/r")],
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
    assert conflict.to_data(format="badgerfish", fidelity="strict") == {
        "r": {
            "item": [
                {"@xmlns": {"p": "urn:a"}, "$": "p:v"},
                {"@xmlns": {"p": "urn:b"}, "$": "p:v"},
            ]
        }
    }
    assert conflict.to_data(format="jsonml", fidelity="strict") == [
        "r",
        ["item", {"xmlns:p": "urn:a"}, "p:v"],
        ["item", {"xmlns:p": "urn:b"}, "p:v"],
    ]


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
            "@xmlns": {"ns0": "urn:example"},
            "ns0:item": [{"@xmlns": {"": "urn:example"}, "$": "v"}],
        }
    }
    assert document.to_data(format="jsonml") == [
        "r",
        {"xmlns:ns0": "urn:example"},
        ["ns0:item", {"xmlns": "urn:example"}, "v"],
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


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    "content,text",
    [(INTEGER, "12"), (STRING, ""), (STRING, "  "), (f"<choice>{INTEGER}{STRING}</choice>", "12")],
)
def test_attributed_native_scalar_keeps_typed_content(content, text, nested):
    pattern = '<attribute name="a"><data type="string"/></attribute>' + content
    xml = f'<r a="x">{text}</r>'
    if nested:
        pattern = f'<element name="item">{pattern}</element>'
        xml = f'<r><item a="x">{text}</item></r>'
    document = valid(pattern, xml)
    node = document.root.children[0] if nested else document.root
    expected = xml_conversion._whole_element_scalar_value(node)
    raw = document.to_dict()
    safe = document.to_data()
    if nested:
        raw, safe = raw["item"], safe["item"]
    assert raw["$"] == expected
    if isinstance(expected, AlternativeValue):
        assert safe["$"] == scalar_tag(expected)
    else:
        assert safe["$"] == expected
    lexical = document.to_dict(typed=False)
    assert (lexical["item"] if nested else lexical)["$"] == text


@pytest.mark.parametrize("order", ["<a/><b/>", "<b/><a/>"])
def test_native_rejects_choice_of_distinct_orders(order):
    a, b = '<element name="a"><empty/></element>', '<element name="b"><empty/></element>'
    document = valid(
        f"<choice><group>{a}{b}</group><group>{b}{a}</group></choice>", f"<r>{order}</r>"
    )
    conversion_error("order-ambiguous", "", lambda: document.to_data(fidelity="strict"))


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("tail", ["LOST", " \n "])
def test_native_lexical_strict_audits_element_only_tails(nested, tail):
    pattern = '<element name="a"><empty/></element>'
    xml = "<r><a/></r>"
    if nested:
        pattern = f'<element name="item">{pattern}</element>'
        xml = "<r><item><a/></item></r>"
    document = valid(pattern, xml)
    owner = document.root.children[0] if nested else document.root
    owner.children[0].set_tail(tail)
    conversion_error(
        "mixed-content",
        "/item" if nested else "",
        lambda: document.to_data(typed=False, fidelity="strict"),
    )
    with pytest.warns(PyXSDWarning, match="mixed-content"):
        document.to_data(typed=False)


@pytest.mark.parametrize("attribute", [False, True])
@pytest.mark.parametrize("default", [False, True])
@pytest.mark.parametrize("datatype", ["QName", "NOTATION"])
def test_native_lexical_qname_context_loss(attribute, default, datatype):
    declaration = 'xmlns="urn:p"' if default else 'xmlns:p="urn:p"'
    text = "v" if default else "p:v"
    pattern = f'<data type="{datatype}"/>'
    xml = f"<r {declaration}>{text}</r>"
    name = 'name="r" ns="urn:p"' if default else 'name="r"'
    if attribute:
        pattern = f'<attribute name="q"><data type="{datatype}"/></attribute><empty/>'
        xml = f'<r {declaration} q="{text}"/>'
    document = valid(pattern, xml, name=name)
    pointer = "/@q" if attribute else "/$"
    conversion_error(
        "namespace-lost", pointer, lambda: document.to_data(typed=False, fidelity="strict")
    )


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("attribute", [False, True])
def test_jsonml_lexical_qnames_keep_frames_when_untyped_or_dirty(typed, attribute):
    pattern = (
        '<attribute name="q"><data type="QName"/></attribute><empty/>'
        if attribute
        else '<data type="QName"/>'
    )
    xml = '<r xmlns:p="urn:p" q="p:v"/>' if attribute else '<r xmlns:p="urn:p">p:v</r>'
    document = parse(pattern, xml, typed=typed)
    assert document.is_valid
    if typed:
        if attribute:
            document.root.set_attribute("q", "p:w")
        else:
            document.root.set_text("p:w")
    result = document.to_data(format="jsonml")
    assert result[1]["xmlns:p"] == "urn:p"
    assert (result[1]["q"] if attribute else result[2]) == ("p:w" if typed else "p:v")


@pytest.mark.parametrize("format", ["badgerfish", "jsonml"])
def test_authored_ns0_does_not_collide_with_generated_root_prefix(format):
    document = valid(
        f'<choice><data type="QName"/>{STRING}</choice>',
        '<r xmlns="urn:a" xmlns:ns0="urn:b">ns0:v</r>',
        name='name="r" ns="urn:a"',
    )
    result = document.to_data(format=format)
    if format == "badgerfish":
        assert result == {"ns1:r": {"@xmlns": {"ns0": "urn:b", "ns1": "urn:a"}, "$": "ns0:v"}}
    else:
        assert result == ["ns1:r", {"xmlns:ns0": "urn:b", "xmlns:ns1": "urn:a"}, "ns0:v"]


@pytest.mark.parametrize("always_list", [False, True])
def test_native_repeated_error_pointer_contains_group_index(always_list):
    document = valid(
        '<zeroOrMore><element name="item"><attribute name="q"><data type="QName"/>'
        "</attribute><empty/></element></zeroOrMore>",
        '<r xmlns:p="urn:p"><item q="v"/><item q="p:v"/></r>',
    )
    conversion_error(
        "namespace-lost",
        "/item/1/@q",
        lambda: document.to_data(fidelity="strict", always_list=always_list),
    )


@pytest.mark.parametrize(
    "pattern,xml",
    [
        (
            '<group><element name="b"><empty/></element><element name="a"><empty/></element></group>',
            "<r><b/><a/></r>",
        ),
        (
            '<group><optional><element name="b"><empty/></element></optional><element name="a"><empty/></element></group>',
            "<r><a/></r>",
        ),
        (
            '<group><optional><element name="b"><empty/></element></optional><element name="a"><empty/></element></group>',
            "<r><b/><a/></r>",
        ),
        (
            '<interleave><element name="a"><empty/></element><element name="b"><empty/></element></interleave>',
            "<r><a/><b/></r>",
        ),
        (
            '<interleave><element name="a"><empty/></element><element name="b"><empty/></element></interleave>',
            "<r><b/><a/></r>",
        ),
    ],
)
def test_native_model_order_does_not_reject_fixed_optional_or_interleave_records(pattern, xml):
    document = valid(pattern, xml)
    assert document.to_data(fidelity="strict") == {
        child._name_: None for child in document.root.children
    }


@pytest.mark.parametrize("attribute", [False, True])
def test_native_lexical_repeated_qnames_have_indexed_pointers(attribute):
    scalar = '<data type="QName"/>'
    content = f'<attribute name="q">{scalar}</attribute><empty/>' if attribute else scalar
    document = valid(
        f'<oneOrMore><element name="item">{content}</element></oneOrMore>',
        '<r xmlns:p="urn:p"><item q="v"/><item q="p:v"/></r>'
        if attribute
        else '<r xmlns:p="urn:p"><item>v</item><item>p:v</item></r>',
    )
    pointer = "/item/1/@q" if attribute else "/item/1"
    conversion_error(
        "namespace-lost", pointer, lambda: document.to_data(typed=False, fidelity="strict")
    )
    with pytest.warns(PyXSDWarning, match="namespace-lost"):
        assert document.to_data(typed=False)["item"][1] == ({"@q": "p:v"} if attribute else "p:v")


def test_native_always_list_single_child_qname_pointer():
    document = valid(
        '<element name="item"><data type="QName"/></element>',
        '<r xmlns:p="urn:p"><item>p:v</item></r>',
    )
    conversion_error(
        "namespace-lost", "/item/0", lambda: document.to_data(fidelity="strict", always_list=True)
    )


def test_namespace_scopes_reset_default_for_unqualified_descendants():
    document = valid(
        '<element name="item" ns="urn:p"><attribute name="q"><choice>'
        '<data type="QName"/><data type="string"/></choice></attribute>'
        '<element name="leaf" ns=""><empty/></element></element>',
        '<r><item xmlns="urn:p" q="v"><leaf xmlns=""/></item></r>',
    )
    assert document.to_data(format="jsonml") == [
        "r",
        {"xmlns:ns0": "urn:p"},
        ["ns0:item", {"xmlns": "urn:p", "q": "v"}, ["leaf", {"xmlns": ""}]],
    ]
    assert document.to_data(format="badgerfish") == {
        "r": {
            "@xmlns": {"ns0": "urn:p"},
            "ns0:item": {"@xmlns": {"": "urn:p"}, "@q": "v", "leaf": {"@xmlns": {"": ""}}},
        }
    }


def test_jsonml_stale_and_generic_nodes_keep_authoritative_namespace_context():
    document = valid(
        "<zeroOrMore><element><anyName/><text/></element></zeroOrMore>",
        '<r xmlns:p="urn:p"><item>p:v</item></r>',
    )
    stale = document.transform(lambda root: root)
    assert stale.to_data(format="jsonml") == [
        "r",
        {"xmlns:p": "urn:p"},
        ["item", {"xmlns:p": "urn:p"}, "p:v"],
    ]


@pytest.mark.parametrize("attribute", [False, True])
def test_native_lexical_qname_lists_require_context(attribute):
    scalar = '<list><oneOrMore><data type="QName"/></oneOrMore></list>'
    content = f'<attribute name="q">{scalar}</attribute><empty/>' if attribute else scalar
    document = valid(
        content,
        '<r xmlns:p="urn:p" q="p:v p:w"/>' if attribute else '<r xmlns:p="urn:p">p:v p:w</r>',
    )
    conversion_error(
        "namespace-lost",
        "/@q" if attribute else "/$",
        lambda: document.to_data(typed=False, fidelity="strict"),
    )


def test_qname_attribute_does_not_make_string_content_namespace_dependent():
    document = valid(
        '<attribute name="q"><data type="QName"/></attribute><data type="string"/>',
        '<r xmlns:p="urn:p" q="v">p:ordinary-string</r>',
    )
    assert document.to_data(typed=False, fidelity="strict") == {"@q": "v", "$": "p:ordinary-string"}


def test_native_order_repeat_and_dirty_unknown_root():
    document = valid(
        f"<oneOrMore><choice>{A}{B}</choice></oneOrMore>", "<r><a>1</a><a>2</a><b>x</b></r>"
    )
    conversion_error("order-ambiguous", "", lambda: document.to_data(fidelity="strict"))
    unknown = valid(f"{A}{B}", "<r><a>1</a><b>x</b></r>")
    unknown.root.set_name("unknown")
    conversion_error("order-ambiguous", "", lambda: unknown.to_data(typed=False, fidelity="strict"))


@pytest.mark.parametrize(
    "scalar,text",
    [
        ('<list><group><data type="integer"/><data type="integer"/></group></list>', "1 2"),
        ('<list><choice><data type="integer"/><data type="string"/></choice></list>', "word"),
        ('<list><optional><data type="integer"/></optional></list>', "1"),
    ],
)
def test_native_lexical_nonqname_lists_do_not_claim_context_loss(scalar, text):
    document = valid(scalar, f'<r xmlns:p="urn:p">{text}</r>')
    assert document.to_data(typed=False, fidelity="strict") == {"$": text}


@pytest.mark.parametrize("format", ["badgerfish", "jsonml"])
def test_wildcard_generated_prefix_avoids_an_authored_hex_prefix(format):
    prefix = "ns_" + b"urn:wild".hex()
    document = valid(
        '<element><anyName/><choice><data type="QName"/><data type="string"/></choice></element>',
        f'<r><item xmlns="urn:wild" xmlns:{prefix}="urn:other">{prefix}:v</item></r>',
    )
    data = document.to_data(format=format)
    if format == "badgerfish":
        assert data == {
            "r": {
                "@xmlns": {prefix + "_": "urn:wild"},
                prefix + "_:item": {"@xmlns": {prefix: "urn:other"}, "$": prefix + ":v"},
            }
        }
    else:
        assert data == [
            "r",
            {"xmlns:" + prefix + "_": "urn:wild"},
            [prefix + "_:item", {"xmlns:" + prefix: "urn:other"}, prefix + ":v"],
        ]


def test_inconsistent_dirty_default_frame_raises_instead_of_changing_root_namespace():
    document = valid('<data type="QName"/>', "<r>v</r>")
    document.root.set_namespace_bindings({"": "urn:p"})
    conversion_error("namespace-conflict", "", lambda: document.to_data(format="jsonml"))


def test_scoped_namespace_declarations_do_not_mutate_shared_allocations():
    document = valid("<empty/>")
    names = xml_conversion._LexicalNames([])
    scoped = names.at(document.root)
    scoped.declare("p", "urn:p", ())
    assert scoped.declarations() == {"p": "urn:p"}
    assert names.declarations() == {}


def test_native_order_summary_visits_shared_model_dag_once(monkeypatch):
    from pyxsd.relaxng.projection import ContentContract

    document = valid(f"{A}{B}", "<r><a>1</a><b>x</b></r>")
    plan = document.schema.projection
    route = plan.routes[plan.roots[0]]
    model = route.content.model
    for _ in range(24):
        model = ContentContract("choice", (model, model))
    calls = 0
    original = xml_conversion._model_order

    def bounded(model, counts, analysis):
        nonlocal calls
        calls += 1
        assert calls < 100, "shared model traversal lost its memoization"
        return original(model, counts, analysis)

    monkeypatch.setattr(xml_conversion, "_model_order", bounded)
    changed = replace(route, content=replace(route.content, model=model))
    assert xml_conversion._native_order(changed, document.root.children, ()) is None
    assert calls < 60


@pytest.mark.parametrize("attribute", [False, True])
@pytest.mark.parametrize("text", ["text", ""])
@pytest.mark.parametrize("nested", [False, True])
def test_native_mixed_leaf_keeps_scalar_interpretation_with_attributes(attribute, text, nested):
    content = f'<optional><attribute name="id">{STRING}</attribute></optional>{MIXED_I}'
    attrs = ' id="x"' if attribute else ""
    xml = f"<r{attrs}>{text}</r>"
    if nested:
        content = f'<element name="item">{content}</element>'
        xml = f"<r><item{attrs}>{text}</item></r>"
    document = valid(content, xml)
    node = document.root.children[0] if nested else document.root
    # Explicit empty text must be distinguished from an absent character node.
    if text == "":
        node.set_text("")
        document = document.revalidate()
        node = document.root.children[0] if nested else document.root
    value = xml_conversion._whole_element_scalar_value(node)
    assert isinstance(value, AlternativeValue)
    raw = document.to_dict()
    safe = document.to_data()
    if nested:
        raw, safe = raw["item"], safe["item"]
    if nested and not attribute:
        assert raw == value
        assert safe == scalar_tag(value)
    else:
        assert raw["$"] == value
        assert safe["$"] == scalar_tag(value)


@pytest.mark.parametrize("iterations", [1, 2, 3])
@pytest.mark.parametrize("typed", [False, True])
def test_native_strict_fixed_repeated_group_uses_observed_counts(iterations, typed):
    content = f"<oneOrMore><group>{A}{B}</group></oneOrMore>"
    document = valid(content, "<r>" + "<a>1</a><b>x</b>" * iterations + "</r>")
    a, b = (1 if typed else "1"), "x"
    assert document.to_data(typed=typed, fidelity="strict") == {
        "a": a if iterations == 1 else [a] * iterations,
        "b": b if iterations == 1 else [b] * iterations,
    }


@pytest.mark.parametrize("order", ["<a>1</a>", "<b>x</b>", "<a>1</a><b>x</b>"])
def test_native_strict_choice_counts_ignore_unavailable_names(order):
    content = f"<choice><group>{A}{B}</group><group>{B}{A}</group>{A}{B}</choice>"
    document = valid(content, f"<r>{order}</r>")
    if order.count("</") == 2:
        conversion_error("order-ambiguous", "", lambda: document.to_data(fidelity="strict"))
    else:
        assert document.to_data(fidelity="strict") == ({"a": 1} if "<a>" in order else {"b": "x"})


@pytest.mark.parametrize("dirty", [False, True])
def test_native_strict_lexical_checks_observed_sequence_against_model(dirty):
    document = parse(f"{A}{B}", "<r><b>x</b><a>1</a></r>", typed=False)
    if dirty:
        document = valid(f"{A}{B}", "<r><a>1</a><b>x</b></r>")
        document.root.children[0].set_name("b")
        document.root.children[1].set_name("a")
    conversion_error(
        "order-ambiguous", "", lambda: document.to_data(typed=False, fidelity="strict")
    )


@pytest.mark.parametrize("attribute", [False, True])
@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("text,loss", [("v p:ordinary", False), ("p:v ordinary", True)])
def test_native_lexical_mixed_qname_list_audits_only_qname_tokens(attribute, typed, text, loss):
    scalar = '<list><data type="QName"/><data type="string"/></list>'
    content = f'<attribute name="q">{scalar}</attribute><empty/>' if attribute else scalar
    xml = f'<r xmlns:p="urn:p" q="{text}"/>' if attribute else f'<r xmlns:p="urn:p">{text}</r>'
    document = parse(content, xml, typed=typed)
    assert document.is_valid
    if loss:
        conversion_error(
            "namespace-lost",
            "/@q" if attribute else "/$",
            lambda: document.to_data(typed=False, fidelity="strict"),
        )
    else:
        assert document.to_data(typed=False, fidelity="strict") == {
            "@q" if attribute else "$": text
        }


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("typed", [False, True])
def test_native_lexical_wildcard_attribute_contracts_match_observed_name(exact, typed):
    string_name = 'name="s:a"' if exact else ""
    string_class = "" if exact else '<nsName ns="urn:s"/>'
    string_attr = f'<attribute {string_name} xmlns:s="urn:s">{string_class}{STRING}</attribute>'
    if not exact:
        string_attr = f"<oneOrMore>{string_attr}</oneOrMore>"
    content = (
        f"<interleave>{string_attr}"
        '<zeroOrMore><attribute><nsName ns="urn:q"/><data type="QName"/></attribute>'
        "</zeroOrMore></interleave>"
    )
    document = parse(content, '<r xmlns:s="urn:s" xmlns:p="urn:p" s:a="p:ordinary"/>', typed=typed)
    assert document.is_valid
    assert document.to_data(typed=False, fidelity="strict") == {"@{urn:s}a": "p:ordinary"}


def test_native_strict_lexical_rejects_incomplete_fixed_sequence():
    document = parse(f"{A}{B}", "<r><a>1</a></r>", typed=False)
    conversion_error(
        "order-ambiguous", "", lambda: document.to_data(typed=False, fidelity="strict")
    )


def test_native_strict_long_fixed_repetition_does_not_recurse_per_child():
    document = valid(f"<oneOrMore>{A}</oneOrMore>", "<r>" + "<a>1</a>" * 300 + "</r>")
    assert document.to_data(fidelity="strict") == {"a": [1] * 300}


@pytest.mark.parametrize(
    "order,ambiguous",
    [
        ("<a>1</a>", False),
        ("<a>1</a><b>x</b>", False),
        ("<a>1</a><b>x</b><a>2</a>", True),
        ("<a>1</a><b>x</b><a>2</a><b>y</b>", False),
    ],
)
def test_native_strict_repeated_optional_group_uses_counts(order, ambiguous):
    document = valid(
        f"<oneOrMore><group>{A}<optional>{B}</optional></group></oneOrMore>", f"<r>{order}</r>"
    )
    if ambiguous:
        conversion_error("order-ambiguous", "/a/1", lambda: document.to_data(fidelity="strict"))
    else:
        assert document.to_data(fidelity="strict") == document.to_data()


@pytest.mark.parametrize(
    "content",
    [
        A,
        f"{A}{B}",
        f"<optional>{A}</optional><optional>{B}</optional>",
        f"<choice><group>{A}{B}</group><group>{B}{A}</group></choice>",
        f"<oneOrMore><group>{A}{B}</group></oneOrMore>",
        f"<oneOrMore><optional>{A}</optional></oneOrMore>",
        f"<interleave><oneOrMore>{A}</oneOrMore><optional>{B}</optional></interleave>",
        f"<interleave><group>{A}{A}</group>{B}</interleave>",
        "<element><anyName/><text/></element>",
        "<text/>",
        "<notAllowed/>",
    ],
)
def test_native_order_analysis_agrees_with_compiled_sequence_language(content):
    schema = schema_for(content)
    plan = schema.projection
    model = plan.routes[plan.roots[0]].content.model
    analysis = xml_conversion._OrderAnalysis(("a", "b"), ())
    for a_count, b_count in product(range(3), repeat=2):
        observed_counts = (a_count, b_count)
        orders = set(permutations(("a",) * a_count + ("b",) * b_count))
        accepted = set()
        for order in orders:
            xml = (
                "<r>"
                + "".join(f"<{name}>{'1' if name == 'a' else 'x'}</{name}>" for name in order)
                + "</r>"
            )
            valid_order = schema.parse(io.StringIO(xml), typed=False).is_valid
            assert xml_conversion._order_matches(model, order, analysis) == valid_order
            # Repeat queries prove cache results retain the same verdict.
            assert xml_conversion._order_matches(model, order, analysis) == valid_order
            if valid_order:
                accepted.add(order)
        reconstructed = xml_conversion._model_order(model, observed_counts, analysis)
        if "interleave" not in content:
            assert len(reconstructed) == min(2, len(accepted))
            assert reconstructed <= accepted
        else:
            assert len(reconstructed) == bool(accepted)
            assert reconstructed <= accepted


def test_native_order_analysis_budget_fails_closed_at_owning_pointer():
    document = valid(f"{A}{B}", "<r><a>1</a><b>x</b></r>")
    plan = document.schema.projection
    model = plan.routes[plan.roots[0]].content.model
    analysis = xml_conversion._OrderAnalysis(("a", "b"), ("r",), remaining=1)
    conversion_error(
        "order-ambiguous", "/r", lambda: xml_conversion._model_order(model, (1, 1), analysis)
    )


def test_native_lexical_unknown_leaf_root_does_not_need_child_order():
    document = valid("<text/>", "<r>text</r>")
    document.root.set_name("unknown")
    assert document.to_data(typed=False, fidelity="strict") == {"$": "text"}


@pytest.mark.parametrize(
    "text,bindings",
    [
        ("v p:ordinary", []),
        ("p:v ordinary", [("p", "urn:p")]),
    ],
)
def test_qname_list_wrappers_declare_only_qname_token_bindings(text, bindings):
    document = valid(
        '<choice><list><choice><data type="QName"/><data type="NOTATION"/></choice>'
        '<data type="string"/></list>'
        '<data type="integer"/></choice>',
        f'<r xmlns:p="urn:p">{text}</r>',
    )
    value = document.root.scalar_value()
    assert isinstance(value, AlternativeValue)
    assert xml_conversion._lexical_qname_prefixes(value) == bindings
    assert xml_conversion._lexical_qname_prefixes(value.interpretations[0]) == bindings
    expected = {"@xmlns": dict(bindings), "$": text} if bindings else text
    assert document.to_data(format="badgerfish", fidelity="strict") == {"r": expected}


@pytest.mark.parametrize("state", ["current", "dirty", "stale", "untyped"])
@pytest.mark.parametrize("attribute", [False, True])
def test_lexical_list_qname_audit_never_uses_stale_values_or_decoders(
    state, attribute, monkeypatch
):
    scalar = '<list><data type="QName"/><data type="string"/></list>'
    content = f'<attribute name="q">{scalar}</attribute><empty/>' if attribute else scalar
    xml = (
        '<r xmlns:p="urn:p" q="v p:ordinary"/>'
        if attribute
        else ('<r xmlns:p="urn:p">v p:ordinary</r>')
    )
    document = parse(content, xml, typed=state != "untyped")
    if state == "dirty":
        if attribute:
            document.root.set_attribute("q", "p:v ordinary")
        else:
            document.root.set_text("p:v ordinary")
    elif state == "stale":
        document = document.transform(lambda root: root)

    def forbidden(*args, **kwargs):
        pytest.fail("lexical QName audit read typed values or invoked a decoder")

    from pyxsd.relaxng.datatypes import ScalarDatatype
    from pyxsd.relaxng.tree import RelaxNGElement

    monkeypatch.setattr(RelaxNGElement, "scalar_value", forbidden)
    monkeypatch.setattr(RelaxNGElement, "attribute_value", forbidden)
    monkeypatch.setattr(ScalarDatatype, "decode", forbidden)
    if state == "dirty":
        conversion_error(
            "namespace-lost",
            "/@q" if attribute else "/$",
            lambda: document.to_data(typed=False, fidelity="strict"),
        )
    else:
        assert document.to_data(typed=False, fidelity="strict") == {
            "@q" if attribute else "$": "v p:ordinary"
        }


@pytest.mark.parametrize(
    "content,text",
    [
        ('<list><data type="string"/><data type="string"/></list>', "p:v p:w"),
        ('<list><optional><data type="string"/></optional></list>', "p:v"),
        ('<choice><data type="QName"/><data type="string"/></choice>', "p:v ordinary"),
    ],
)
def test_lexical_string_tokens_do_not_acquire_qname_candidates(content, text):
    document = valid(content, f'<r xmlns:p="urn:p">{text}</r>')
    assert document.to_data(typed=False, fidelity="strict") == {"$": text}


@pytest.mark.parametrize("order", ["<a>1</a><b>x</b>", "<b>x</b><a>1</a>"])
def test_native_strict_choice_ambiguity_survives_a_grouped_suffix(order):
    document = valid(
        f"<group><choice><group>{A}{B}</group><group>{B}{A}</group></choice>"
        '<element name="c"><empty/></element></group>',
        f"<r>{order}<c/></r>",
    )
    conversion_error("order-ambiguous", "", lambda: document.to_data(fidelity="strict"))


@pytest.mark.parametrize(
    "scalar,expected",
    [
        (STRING, ""),
        ('<list><zeroOrMore><data type="integer"/></zeroOrMore></list>', []),
    ],
)
@pytest.mark.parametrize("attribute", [False, True])
def test_native_mixed_leaf_preserves_absent_empty_scalar_evidence(scalar, expected, attribute):
    content = (
        f'<optional><attribute name="id">{STRING}</attribute></optional>'
        f'<choice>{scalar}<element name="i"><empty/></element></choice>'
    )
    document = valid(content, '<r id="x"/>' if attribute else "<r/>")
    assert document.root.text_value() is None
    assert xml_conversion._whole_element_scalar_value(document.root) == expected
    assert document.to_dict()["$"] == expected
    assert document.to_data()["$"] == expected


def test_native_attribute_object_does_not_invent_scalar_for_ignored_whitespace():
    document = valid(
        f'<optional><attribute name="id">{STRING}</attribute></optional>'
        f"<zeroOrMore>{A}</zeroOrMore>",
        '<r id="x"> \n </r>',
    )
    assert document.root.scalar_value() is None
    assert document.to_dict() == {"@id": "x"}
    assert document.to_data() == {"@id": "x"}


@pytest.mark.parametrize("choice", [False, True])
@pytest.mark.parametrize("order", list(permutations(("z", "a", "b"))))
def test_native_strict_nested_interleave_association_is_not_order_loss(choice, order):
    z, a, b = (f'<element name="{name}"><empty/></element>' for name in ("z", "a", "b"))
    left = f"<interleave><interleave>{z}{a}</interleave>{b}</interleave>"
    right = f"<interleave>{z}<interleave>{a}{b}</interleave></interleave>"
    content = f"<choice>{left}{right}</choice>" if choice else left
    document = valid(content, "<r>" + "".join(f"<{name}/>" for name in order) + "</r>")
    assert document.to_data(fidelity="strict") == dict.fromkeys(order)
    plan = document.schema.projection
    model = plan.routes[plan.roots[0]].content.model
    assert xml_conversion._model_order(
        model, (1, 1, 1), xml_conversion._OrderAnalysis(("a", "b", "z"), ())
    ) == {("a", "b", "z")}


@pytest.mark.parametrize("reverse", [False, True])
def test_native_nested_interleave_preserves_group_and_repeat_constraints(reverse):
    z, a, b, c = (f'<element name="{name}"><empty/></element>' for name in ("z", "a", "b", "c"))
    group = f"<oneOrMore><group>{z}{a}</group></oneOrMore>"
    left = f"<interleave><interleave>{group}{b}</interleave>{c}</interleave>"
    right = f"<interleave>{group}<interleave>{b}{c}</interleave></interleave>"
    xml = "<r><z/><b/><a/><z/><c/><a/></r>"
    if reverse:
        xml = "<r><a/><b/><z/><z/><c/><a/></r>"
    document = parse(f"<choice>{left}{right}</choice>", xml, typed=not reverse)
    if reverse:
        assert not document.is_valid
        conversion_error(
            "order-ambiguous", "/a/1", lambda: document.to_data(typed=False, fidelity="strict")
        )
    else:
        assert document.is_valid
        assert document.to_data(fidelity="strict") == {
            "z": [None, None],
            "b": None,
            "a": [None, None],
            "c": None,
        }


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("attribute", [False, True])
@pytest.mark.parametrize("listed", [False, True])
@pytest.mark.parametrize("datatype", ["QName", "NOTATION"])
@pytest.mark.parametrize("branch", ["s", "q"])
def test_native_lexical_qname_audit_observes_whole_document_branch(
    typed,
    attribute,
    listed,
    datatype,
    branch,
):
    def body(kind, datatype):
        scalar = f'<data type="{datatype}"/>'
        if listed:
            scalar = f"<list>{scalar}{STRING}</list>"
        position = f'<attribute name="x">{scalar}</attribute><empty/>' if attribute else scalar
        return f'<group><attribute name="kind"><value>{kind}</value></attribute>{position}</group>'

    content = f"<choice>{body('q', datatype)}{body('s', 'string')}</choice>"
    text = "p:ordinary rest" if listed else "p:ordinary"
    xml = (
        f'<r xmlns:p="urn:p" kind="{branch}" x="{text}"/>'
        if attribute
        else (f'<r xmlns:p="urn:p" kind="{branch}">{text}</r>')
    )
    document = parse(content, xml, typed=typed)
    assert document.is_valid
    snapshot = document.root.revision._typed_snapshot
    evidence = document.binding_evidence
    if branch == "q":
        conversion_error(
            "namespace-lost",
            "/@x" if attribute else "/$",
            lambda: document.to_data(typed=False, fidelity="strict"),
        )
    else:
        assert document.to_data(typed=False, fidelity="strict") == {
            "@kind": "s",
            "@x" if attribute else "$": text,
        }
    assert document.binding_evidence is evidence
    assert document.root.revision._typed_snapshot is snapshot
    assert not document.root.revision.dirty
    assert document.typed is typed


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("attribute", [False, True])
@pytest.mark.parametrize("last_branch", ["s", "q"])
def test_native_lexical_correlated_list_positions_are_not_matched_by_value(
    typed,
    attribute,
    last_branch,
):
    def body(kind, datatype):
        scalar = f'<list><data type="{datatype}"/>{STRING}</list>'
        position = f'<attribute name="x">{scalar}</attribute><empty/>' if attribute else scalar
        return f'<group><attribute name="kind"><value>{kind}</value></attribute>{position}</group>'

    content = (
        f'<oneOrMore><element name="item"><choice>'
        f"{body('q', 'QName')}{body('s', 'string')}</choice></element></oneOrMore>"
    )

    def item(kind):
        return (
            f'<item kind="{kind}" x="p:ordinary rest"/>'
            if attribute
            else (f'<item kind="{kind}">p:ordinary rest</item>')
        )

    document = parse(content, f'<r xmlns:p="urn:p">{item("s")}{item(last_branch)}</r>', typed=typed)
    assert document.is_valid
    if last_branch == "q":
        conversion_error(
            "namespace-lost",
            "/item/1/@x" if attribute else "/item/1/$",
            lambda: document.to_data(typed=False, fidelity="strict"),
        )
    else:
        assert document.to_data(typed=False, fidelity="strict") == {
            "item": [{"@kind": "s", "@x" if attribute else "$": "p:ordinary rest"}] * 2
        }


@pytest.mark.parametrize("typed", [False, True])
def test_lexical_qname_audit_uses_existing_or_one_guarded_whole_document_view(typed, monkeypatch):
    content = (
        '<choice><group><attribute name="kind"><value>q</value></attribute>'
        '<attribute name="x"><data type="QName"/></attribute></group>'
        '<group><attribute name="kind"><value>s</value></attribute>'
        '<attribute name="x"><data type="string"/></attribute></group></choice>'
    )
    document = parse(content, '<r xmlns:p="urn:p" kind="s" x="p:ordinary"/>', typed=typed)
    original = RelaxNGSchema._parse_guarded_instance
    views = []

    def guarded(schema, source, **kwargs):
        assert kwargs["typed"] is True
        assert kwargs["capture"] == "none"
        assert kwargs["capture_xdm"] is False
        assert kwargs["limits"] is document._limits
        assert kwargs["declarations"] is document._declarations
        view = original(schema, source, **kwargs)
        views.append(view)
        return view

    def forbidden(*args, **kwargs):
        pytest.fail("valid lexical audit used an isolated scalar scheme or decoded a value")

    from pyxsd.relaxng.datatypes import ScalarDatatype

    monkeypatch.setattr(RelaxNGSchema, "_parse_guarded_instance", guarded)
    monkeypatch.setattr(xml_conversion, "build_binding_evidence", forbidden)
    monkeypatch.setattr(ScalarDatatype, "decode", forbidden)
    report, snapshot = document.report, document.root.revision._typed_snapshot
    assert document.to_data(typed=False, fidelity="strict") == {"@kind": "s", "@x": "p:ordinary"}
    assert len(views) == (0 if typed else 1)
    if views:
        assert views[0].root is not document.root
        assert views[0].root.revision is not document.root.revision
        assert views[0].binding_evidence is not None
    assert document.report is report
    assert document.root.revision._typed_snapshot is snapshot
    assert document.interpretations_current


@pytest.mark.parametrize("branch", ["s", "q"])
@pytest.mark.parametrize("state", ["dirty", "stale"])
def test_lexical_correlated_audit_uses_current_tree_without_refreshing_user_view(branch, state):
    content = (
        '<choice><group><attribute name="kind"><value>q</value></attribute>'
        '<data type="QName"/></group><group>'
        '<attribute name="kind"><value>s</value></attribute><data type="string"/></group></choice>'
    )
    document = valid(content, f'<r xmlns:p="urn:p" kind="{"q" if branch == "s" else "s"}">p:v</r>')
    document.root.set_attribute("kind", branch)
    if state == "stale":
        document = document.transform(lambda root: root)
    report, snapshot, evidence = (
        document.report,
        document.root.revision._typed_snapshot,
        document.binding_evidence,
    )
    if branch == "q":
        conversion_error(
            "namespace-lost", "/$", lambda: document.to_data(typed=False, fidelity="strict")
        )
    else:
        assert document.to_data(typed=False, fidelity="strict") == {"@kind": "s", "$": "p:v"}
    assert not document.interpretations_current
    assert document.binding_evidence is evidence
    assert document.report is report
    assert document.root.revision._typed_snapshot is snapshot


@pytest.mark.parametrize(
    "text,loss",
    [
        ("p:v ordinary", True),
        ("v p:ordinary", False),
        ("p:v", False),
    ],
)
def test_unavailable_whole_audit_fails_closed_only_for_local_token_potential(text, loss):
    content = (
        '<attribute name="kind"><value>ok</value></attribute>'
        '<attribute name="x"><list><data type="QName"/><data type="string"/></list></attribute>'
    )
    document = parse(content, f'<r xmlns:p="urn:p" kind="invalid" x="{text}"/>', typed=False)
    assert not document.is_valid
    if loss:
        with pytest.raises(PyXSDError) as caught:
            document.to_data(typed=False, fidelity="strict")
        assert str(caught.value).startswith("namespace-lost at '/@x': ")
        assert "no accepting whole-document derivation" in str(caught.value)
        with pytest.warns(PyXSDWarning, match="no accepting whole-document derivation"):
            assert document.to_data(typed=False)["@x"] == text
    else:
        assert document.to_data(typed=False, fidelity="strict") == {"@kind": "invalid", "@x": text}


@pytest.mark.parametrize("unavailable", ["busy", "limits", "missing"])
def test_lexical_qname_audit_reports_guard_budget_and_missing_evidence(unavailable, monkeypatch):
    from pyxsd.relaxng.limits import RelaxNGLimits
    from pyxsd.relaxng.schema import _parse_guard

    schema = schema_for('<data type="QName"/>')
    limits = RelaxNGLimits(max_evidence_states=0) if unavailable == "limits" else None
    document = schema.parse(io.StringIO('<r xmlns:p="urn:p">p:v</r>'), typed=False, limits=limits)
    assert document.is_valid
    guard = _parse_guard(schema)
    if unavailable == "busy":
        assert guard.acquire(blocking=False)
    if unavailable == "missing":
        original = RelaxNGSchema._parse_guarded_instance

        def missing(self, source, **kwargs):
            result = original(self, source, **kwargs)
            result._binding_evidence = None
            return result

        monkeypatch.setattr(RelaxNGSchema, "_parse_guarded_instance", missing)
    try:
        with pytest.raises(PyXSDError) as caught:
            document.to_data(typed=False, fidelity="strict")
        message = str(caught.value)
        assert message.startswith("namespace-lost at '/$': ")
        assert {
            "busy": "concurrently or reentrantly",
            "limits": "max_evidence_states",
            "missing": "accepting evidence is unavailable",
        }[unavailable] in message
    finally:
        if unavailable == "busy":
            guard.release()
    assert document.binding_evidence is None
    assert document.root.revision._typed_snapshot is None


def test_whole_document_lexical_audit_preserves_declarations_and_scoped_namespaces(monkeypatch):
    content = (
        '<attribute name="entity"><data type="ENTITY"/></attribute>'
        '<choice><group><attribute name="kind"><value>q</value></attribute>'
        '<oneOrMore><element name="item"><data type="QName"/></element></oneOrMore></group>'
        '<group><attribute name="kind"><value>s</value></attribute>'
        '<oneOrMore><element name="item"><data type="string"/></element></oneOrMore></group></choice>'
    )
    document = parse(
        content,
        "<!DOCTYPE r [<!ENTITY foo SYSTEM 'foo.bin' NDATA data>]>"
        '<r kind="s" entity="foo"><item xmlns:p="urn:a">p:v</item>'
        '<item xmlns:p="urn:b">p:v</item></r>',
        typed=False,
    )
    assert document.is_valid

    def forbidden(*args, **kwargs):
        pytest.fail("audit attempted to recollect declarations or create an isolated scalar scheme")

    monkeypatch.setattr("pyxsd.relaxng.schema.collect_document_declarations", forbidden)
    monkeypatch.setattr(xml_conversion, "build_binding_evidence", forbidden)
    frames = [dict(node.namespaces) for node in document.root.children]
    assert document.to_data(typed=False, fidelity="strict") == {
        "@kind": "s",
        "@entity": "foo",
        "item": ["p:v", "p:v"],
    }
    assert [dict(node.namespaces) for node in document.root.children] == frames
    assert document.binding_evidence is None


def test_invalid_document_with_atomic_alternatives_reports_potential_qname_loss():
    content = (
        '<attribute name="kind"><value>ok</value></attribute>'
        '<attribute name="x"><choice><data type="QName"/><data type="string"/></choice></attribute>'
    )
    document = parse(content, '<r xmlns:p="urn:p" kind="invalid" x="p:v"/>', typed=False)
    assert not document.is_valid
    with pytest.raises(PyXSDError) as caught:
        document.to_data(typed=False, fidelity="strict")
    assert str(caught.value).startswith("namespace-lost at '/@x': ")
    assert "no accepting whole-document derivation" in str(caught.value)


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("listed", [False, True])
@pytest.mark.parametrize("branch", ["s", "q"])
def test_lexical_whole_document_audit_includes_scalar_branches_of_structural_routes(
    typed,
    listed,
    branch,
):
    scalar = '<data type="QName"/>'
    if listed:
        scalar = f"<list>{scalar}{STRING}</list>"
    content = (
        '<choice><group><attribute name="kind"><value>q</value></attribute>'
        f'{scalar}</group><group><attribute name="kind"><value>s</value></attribute>'
        f'<element name="i">{STRING}</element></group></choice>'
    )
    text = "p:v rest" if listed else "p:v"
    inner = text if branch == "q" else f"<i>{text}</i>"
    document = parse(content, f'<r xmlns:p="urn:p" kind="{branch}">{inner}</r>', typed=typed)
    assert document.is_valid
    if branch == "q":
        conversion_error(
            "namespace-lost", "/$", lambda: document.to_data(typed=False, fidelity="strict")
        )
    else:
        assert document.to_data(typed=False, fidelity="strict") == {"@kind": "s", "i": text}
