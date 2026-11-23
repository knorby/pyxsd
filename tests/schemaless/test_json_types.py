"""Typed native JSON views and policy-aware XDM conversion."""

import io
from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import UUID

import pytest

from pyxsd import xsd_data_types as xsd
from pyxsd.locations import DataLocation
from pyxsd.schemaless import (
    JsonDocument,
    ScalarAdapter,
    ScalarTypeError,
    TypePolicy,
    from_data,
    from_json,
)
from pyxsd.xdm.values import XdmArray, XdmAtomic, XdmMap

XSD_NS = "http://www.w3.org/2001/XMLSchema"


class _Named(io.StringIO):
    """A readable text stream with a caller-supplied ``name`` attribute."""

    def __init__(self, text: str, name: str) -> None:
        super().__init__(text)
        self.name = name


def _member(document: JsonDocument, key: str) -> object:
    return document.to_xdm()[0].get(XdmAtomic.string(key))[0]


def test_json_native_kinds_are_not_reinferred():
    doc = from_json('{"n":"42","i":42}', types=TypePolicy.infer())
    assert doc.value_at("/n") == "42"
    assert isinstance(doc.value_at("/n", typed=True), int)
    assert isinstance(doc.value_at("/i", typed=True), int)


def test_json_pin_changes_only_the_typed_xdm_view():
    doc = from_json('{"d":"2026-11-22"}')
    doc.pin_type("/d", "xs:date")
    item = doc.to_xdm()[0].get(XdmAtomic.string("d"))[0]
    assert item.type_name == "{http://www.w3.org/2001/XMLSchema}date"
    assert doc.to_data() == {"d": "2026-11-22"}


def test_native_policy_is_the_default_and_keeps_strings_as_strings():
    doc = from_json('{"n":"42","i":42,"b":true,"d":1.5}')
    assert doc.types == TypePolicy.native()
    assert doc.value_at("/n", True) == "42"
    assert type(doc.value_at("/n", typed=True)) is str
    assert type(doc.value_at("/i", typed=True)) is int
    assert doc.value_at("/b", typed=True) is True
    assert doc.value_at("/d", typed=True) == Decimal("1.5")
    assert _member(doc, "n").type_name == f"{{{XSD_NS}}}string"
    assert _member(doc, "i").type_name == f"{{{XSD_NS}}}integer"


def test_explicit_pin_overrides_inference_in_the_view_and_xdm():
    doc = from_json('{"x":"42","y":"true"}', types=TypePolicy.infer())
    assert type(doc.value_at("/x", typed=True)) is xsd.Integer
    assert doc.value_at("/x", typed=True) == 42
    assert doc.value_at("/y", typed=True) is True
    assert _member(doc, "x").type_name == f"{{{XSD_NS}}}integer"
    assert _member(doc, "y").type_name == f"{{{XSD_NS}}}boolean"

    assert not doc.pin_type("/x", "xs:string").has_errors
    assert doc.value_at("/x", typed=True) == "42"
    assert doc.value_at("/y", typed=True) is True
    assert _member(doc, "x") == XdmAtomic.string("42")
    assert doc.to_data() == {"x": "42", "y": "true"}


def test_inference_refines_only_string_leaves():
    source = {
        "i": 42,
        "b": True,
        "d": Decimal("1.5"),
        "f": 1.5,
        "s": "42",
        "leading": "001",
        "empty": "",
    }
    doc = from_data(source, types=TypePolicy.infer())
    typed = doc.value_at("", typed=True)
    assert typed == {
        "i": 42,
        "b": True,
        "d": Decimal("1.5"),
        "f": 1.5,
        "s": 42,
        "leading": "001",
        "empty": "",
    }
    assert type(typed["s"]) is xsd.Integer
    assert type(typed["i"]) is int and type(typed["b"]) is bool
    assert type(typed["d"]) is Decimal and type(typed["f"]) is float
    root = doc.to_xdm()[0]
    assert root.get(XdmAtomic.string("s"))[0].type_name == f"{{{XSD_NS}}}integer"
    assert root.get(XdmAtomic.string("leading"))[0].type_name == f"{{{XSD_NS}}}string"
    assert root.get(XdmAtomic.string("i"))[0].type_name == f"{{{XSD_NS}}}integer"
    assert root.get(XdmAtomic.string("b"))[0].type_name == f"{{{XSD_NS}}}boolean"
    assert root.get(XdmAtomic.string("d"))[0].type_name == f"{{{XSD_NS}}}decimal"
    assert root.get(XdmAtomic.string("f"))[0].type_name == f"{{{XSD_NS}}}double"
    assert doc.to_data() == source


def test_lexical_json_policy_keeps_strings_but_still_honors_pins():
    doc = from_json('{"n":"42"}', types=TypePolicy.lexical())
    assert doc.value_at("/n", typed=True) == "42"
    assert _member(doc, "n").type_name == f"{{{XSD_NS}}}string"
    assert not doc.pin_type("/n", "xs:integer").has_errors
    assert doc.value_at("/n", typed=True) == 42
    assert _member(doc, "n").type_name == f"{{{XSD_NS}}}integer"


def test_failing_pin_changes_only_typed_reads_and_names_its_pointer():
    doc = from_json(_Named('{"d":"not-a-date","n":"42"}', "in.json"))
    report = doc.pin_type("/d", "xs:date")
    location = DataLocation("in.json", "/d")
    assert report.error_count == 1
    issue = report.issues[0]
    assert issue.code == "scalar-type-invalid"
    assert issue.location == location
    assert issue.location_precision == "exact"
    assert issue.format().startswith("error: [scalar-type-invalid] in.json#/d:")
    assert doc.types.pins["/d"] is TypePolicy.resolve("xs:date")

    with pytest.raises(ScalarTypeError) as failure:
        doc.value_at("/d", typed=True)
    assert failure.value.location == location
    with pytest.raises(ScalarTypeError):
        doc.to_xdm()
    assert doc.value_at("/n", typed=True) == "42"
    assert doc.to_data() == {"d": "not-a-date", "n": "42"}
    assert doc.to_json() == '{"d": "not-a-date", "n": "42"}'


def test_type_testing_reports_without_installing_a_pin():
    doc = from_json('{"d":"2026-11-22","bad":"x"}')
    assert not doc.test_type("/d", "xs:date").has_errors
    assert doc.types.pins == {}
    assert doc.value_at("/d", typed=True) == "2026-11-22"

    report = doc.test_type("/bad", "xs:date")
    assert report.error_count == 1
    assert report.issues[0].code == "scalar-type-invalid"
    assert report.issues[0].location == DataLocation("<memory>", "/bad")
    assert doc.types.pins == {}
    assert _member(doc, "bad").type_name == f"{{{XSD_NS}}}string"


@pytest.mark.parametrize(
    ("pointer", "type_ref", "code"),
    [
        ("d", "xs:date", "invalid-json-pointer"),
        ("/d~2", "xs:date", "invalid-json-pointer"),
        ("/absent", "xs:date", "unknown-json-pointer"),
        ("/d/x", "xs:date", "unknown-json-pointer"),
        ("/a/1", "xs:date", "unknown-json-pointer"),
        ("/a/01", "xs:date", "unknown-json-pointer"),
        ("/o", "xs:date", "non-scalar-json-value"),
        ("/a", "xs:date", "non-scalar-json-value"),
        ("/n", "xs:date", "non-scalar-json-value"),
        ("/d", "xs:UUID", "unknown-scalar-type"),
    ],
)
def test_invalid_or_non_scalar_selectors_leave_the_policy_unchanged(pointer, type_ref, code):
    doc = from_json('{"d":"2026-11-22","o":{},"a":[],"n":null}')
    assert not doc.pin_type("/d", "xs:date").has_errors
    before = doc.types
    for operation in (doc.test_type, doc.pin_type):
        report = operation(pointer, type_ref)
        assert report.error_count == 1
        assert report.issues[0].code == code
        assert doc.types is before
    assert doc.types.pins == {"/d": TypePolicy.resolve("xs:date")}
    assert doc.value_at("/o", typed=True) == {}
    assert doc.value_at("/a", typed=True) == []
    assert doc.value_at("/n", typed=True) is None
    assert doc.to_data() == {"d": "2026-11-22", "o": {}, "a": [], "n": None}


def test_a_later_pin_at_the_same_pointer_replaces_the_earlier_one():
    doc = from_json('{"x":"42"}')
    assert not doc.pin_type("/x", "xs:integer").has_errors
    assert doc.value_at("/x", typed=True) == 42
    assert not doc.pin_type("/x", "xs:string").has_errors
    assert doc.value_at("/x", typed=True) == "42"
    assert _member(doc, "x") == XdmAtomic.string("42")


def test_custom_adapter_supplies_typed_values_and_xdm_output():
    raw = "12345678-1234-5678-9abc-123456789abc"
    value = UUID(raw)
    adapter = ScalarAdapter(
        parse=UUID,
        format=str,
        xdm=lambda item: XdmAtomic.string(f"urn:uuid:{item}"),
    )
    doc = from_json(f'{{"id":"{raw}"}}')
    assert not doc.pin_type("/id", adapter).has_errors
    assert doc.value_at("/id", typed=True) == value
    assert _member(doc, "id") == XdmAtomic.string(f"urn:uuid:{value}")
    assert doc.to_data() == {"id": raw}


def test_pins_convert_native_scalar_kinds_without_float_round_trips():
    doc = from_json('{"b":true,"i":42,"d":1e2,"n":"7"}')
    assert not doc.pin_type("/b", "xs:boolean").has_errors
    assert not doc.pin_type("/i", "xs:integer").has_errors
    assert not doc.pin_type("/d", "xs:decimal").has_errors
    assert not doc.pin_type("/n", "xs:string").has_errors

    assert doc.value_at("/b", typed=True) is True
    assert doc.value_at("/i", typed=True) == 42
    assert doc.value_at("/d", typed=True) == Decimal("1E+2")
    assert doc.value_at("/n", typed=True) == "7"
    assert _member(doc, "d").type_name == f"{{{XSD_NS}}}decimal"
    assert _member(doc, "n") == XdmAtomic.string("7")
    assert doc.to_data() == {"b": True, "i": 42, "d": Decimal("1E+2"), "n": "7"}

    host = from_data({"f": 1.5})
    assert not host.pin_type("/f", "xs:double").has_errors
    assert type(host.value_at("/f", typed=True)) is xsd.Double
    assert _member(host, "f").type_name == f"{{{XSD_NS}}}double"


def test_root_scalar_pins_affect_the_typed_xdm_sequence():
    doc = from_json('"42"')
    assert doc.value_at("", typed=True) == "42"
    assert not doc.pin_type("", "xs:integer").has_errors
    assert doc.value_at("", typed=True) == 42
    [item] = doc.to_xdm()
    assert item.type_name == f"{{{XSD_NS}}}integer"
    assert item.value == 42
    assert doc.to_data() == "42"
    assert doc.to_json() == '"42"'


def test_root_null_stays_empty_and_rejects_a_root_pin():
    doc = from_json("null")
    report = doc.pin_type("", "xs:integer")
    assert report.error_count == 1
    assert report.issues[0].code == "non-scalar-json-value"
    assert doc.types.pins == {}
    assert doc.value_at("", typed=True) is None
    assert doc.to_xdm() == ()


def test_pointer_escaping_addresses_each_member_independently():
    doc = from_json('{"a/b":"2026-11-22","a~b":"2026-11-23","":"root"}')
    assert doc.value_at("/a~1b", typed=True) == "2026-11-22"
    assert doc.value_at("/a~0b", typed=True) == "2026-11-23"
    assert doc.value_at("/", typed=True) == "root"

    assert not doc.pin_type("/a~1b", "xs:date").has_errors
    assert doc.value_at("/a~1b", typed=True) == xsd.Date("2026-11-22")
    assert doc.value_at("/a~0b", typed=True) == "2026-11-23"
    assert _member(doc, "a/b").type_name == f"{{{XSD_NS}}}date"
    assert _member(doc, "a~b").type_name == f"{{{XSD_NS}}}string"
    assert _member(doc, "").type_name == f"{{{XSD_NS}}}string"


def test_containers_remain_native_containers_with_typed_scalar_leaves():
    doc = from_json('{"o":{"n":"1","z":null},"a":["2",{"n":"3"}]}', types=TypePolicy.infer())
    typed = doc.value_at("", typed=True)
    assert isinstance(typed, dict)
    assert isinstance(typed["o"], dict)
    assert isinstance(typed["a"], list)
    assert typed == {"o": {"n": 1, "z": None}, "a": [2, {"n": 3}]}

    root = doc.to_xdm()[0]
    assert isinstance(root, XdmMap)
    array = root.get(XdmAtomic.string("a"))[0]
    assert isinstance(array, XdmArray)
    assert array.get(1)[0] == XdmAtomic.integer(2)
    assert array.get(2)[0].get(XdmAtomic.string("n"))[0] == XdmAtomic.integer(3)

    assert not doc.pin_type("/o/n", "xs:boolean").has_errors
    assert doc.value_at("/o", typed=True) == {"n": True, "z": None}
    outer = doc.to_xdm()[0].get(XdmAtomic.string("o"))[0]
    assert outer.get(XdmAtomic.string("n"))[0] == XdmAtomic.boolean(True)

    report = doc.pin_type("/o", "xs:date")
    assert report.error_count == 1
    assert report.issues[0].code == "non-scalar-json-value"
    assert doc.value_at("/o", typed=True) == {"n": True, "z": None}
    assert doc.to_data() == {"o": {"n": "1", "z": None}, "a": ["2", {"n": "3"}]}


def test_json_policies_are_document_local_and_immutable():
    policy = TypePolicy.infer()
    first = from_json('{"x":"1"}', types=policy)
    second = from_json('{"x":"1"}', types=policy)
    assert first.types is second.types is policy

    assert not first.pin_type("/x", "xs:string").has_errors
    assert first.types is not policy
    assert second.types is policy
    assert first.value_at("/x", typed=True) == "1"
    assert second.value_at("/x", typed=True) == 1

    with pytest.raises(FrozenInstanceError):
        first.types.mode = "native"
    with pytest.raises(TypeError):
        first.types.pins["/x"] = ScalarAdapter(parse=str, format=str)  # type: ignore[index]
    with pytest.raises(AttributeError):
        first.types = policy  # type: ignore[misc]

    direct = JsonDocument({"x": "1"}, types=policy)
    assert direct.types is policy
    assert direct.value_at("/x", typed=True) == 1


def test_non_string_pointer_reports_an_invalid_selector():
    doc = from_json('{"x":"1"}')
    report = doc.test_type(1, "xs:integer")  # type: ignore[arg-type]
    assert report.error_count == 1
    assert report.issues[0].code == "invalid-json-pointer"
    assert doc.types.pins == {}


def test_schema_assertion_hook_sits_between_pins_and_inference():
    class Guided(JsonDocument):
        """A stand-in for the later schema-guidance layer."""

        def _asserted_adapter(self, pointer, value):
            return TypePolicy.resolve("xs:boolean") if pointer == "/x" else None

    doc = Guided({"x": "true", "y": "true"})
    assert doc.value_at("/x", typed=True) is True
    assert _member(doc, "x").type_name == f"{{{XSD_NS}}}boolean"
    assert doc.value_at("/y", typed=True) == "true"
    assert _member(doc, "y").type_name == f"{{{XSD_NS}}}string"

    assert not doc.pin_type("/x", "xs:string").has_errors
    assert doc.value_at("/x", typed=True) == "true"
    assert _member(doc, "x").type_name == f"{{{XSD_NS}}}string"


def test_json_policy_precedence_is_pin_then_assertion_then_inference_then_native():
    policy = TypePolicy.infer()
    asserted = TypePolicy.resolve("xs:boolean")
    inferred = policy.json_adapter("/x", "true")
    assert inferred is not None and inferred.parse("true") is True
    assert policy.json_adapter("/x", "true", asserted=asserted) is asserted
    assert policy.json_adapter("/x", 7) is None
    pinned = policy.with_pin("/x", "xs:string")
    assert pinned.json_adapter("/x", "true", asserted=asserted) is pinned.pins["/x"]
    assert TypePolicy.native().json_adapter("/x", "true") is None
