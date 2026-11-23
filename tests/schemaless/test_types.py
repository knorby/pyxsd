"""Document-local scalar interpretations of authoritative XML text."""

import io
import math
from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import UUID
from xml.etree import ElementTree as ET

import pytest

from pyxsd import xsd_data_types as xsd
from pyxsd._lexical import clone_lexical
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.schemaless import (
    ScalarAdapter,
    ScalarTypeError,
    SchemalessDocument,
    SchemalessElement,
    TypePolicy,
    parse_xml,
)
from pyxsd.validation import ValidationReport
from pyxsd.xdm.annotations import UNTYPED, UNTYPED_ATOMIC
from pyxsd.xdm.values import XdmAtomic


def test_inference_is_conservative_and_lexical_storage_is_unchanged():
    doc = parse_xml(
        io.StringIO('<r a="02138"><i>12</i><b>true</b><s>001</s></r>'), types=TypePolicy.infer()
    )
    assert isinstance(doc.root.children[0].scalar_value(), int)
    assert doc.root.children[1].scalar_value() is True
    assert doc.root.children[2].scalar_value() == "001"
    assert doc.root.attribute_value("a") == "02138"
    assert doc.to_string() == '<r a="02138"><i>12</i><b>true</b><s>001</s></r>'


def test_generic_clone_reads_lexically_even_when_the_source_is_pinned():
    policy = TypePolicy.infer().with_pin("/r/@a", "xs:integer")
    doc = parse_xml(b'<r a="001">12</r>', types=policy)
    clone = doc.transform(clone_lexical)

    assert clone.root.scalar_value() == "12"
    assert clone.root.attribute_value("a") == "001"
    assert clone.root.attribute_value("absent") is None
    assert doc.root.scalar_value() == 12
    assert doc.root.attribute_value("a") == 1


def test_generic_clone_pin_is_validated_and_isolated_from_the_source():
    policy = TypePolicy.lexical().with_pin("/r/@a", "xs:integer")
    doc = parse_xml(b'<r a="001">1</r>', types=policy)
    clone = doc.transform(clone_lexical)
    xdm = clone.to_xdm()

    assert not clone.pin_type("/r", "xs:boolean").has_errors
    clone_policy = clone.root.revision.types
    for path, type_ref, code in (
        ("/r[0]", "xs:integer", "invalid-scalar-path"),
        ("/r", "xs:UUID", "unknown-scalar-type"),
        ("/r/@missing", "xs:integer", "unknown-scalar-path"),
    ):
        report = clone.pin_type(path, type_ref)
        assert [issue.code for issue in report.issues] == [code]
        assert clone.root.revision.types is clone_policy

    assert clone.root.scalar_value() is True
    assert clone.revalidate().root.scalar_value() is True
    assert clone.to_xdm() is xdm
    assert not clone.root.revision.dirty
    assert doc.root.revision.types is policy
    assert doc.root.scalar_value() == "1"
    assert doc.root.attribute_value("a") == 1
    assert doc.report_current


def test_explicit_pin_reports_failures_and_never_falls_back():
    doc = parse_xml(io.StringIO("<r><x>1</x><x>bad</x></r>"))
    report = doc.pin_type("/r/x", "xs:integer")
    assert report.error_count == 1
    assert doc.root.children[0].scalar_value() == 1
    with pytest.raises(ScalarTypeError) as failure:
        doc.root.children[1].scalar_value()
    location = IssueLocation(doc.root.source_location.source, (PathStep("r", 1), PathStep("x", 2)))
    assert failure.value.location == report.issues[0].location == location
    assert report.issues[0].location_precision == "exact"
    assert doc.report.error_count == 0
    assert doc.root.children[1].text_value() == "bad"


def test_type_test_reports_each_exact_failure_without_installing_pin():
    doc = parse_xml(b"<r><n>256</n><n>-1</n><n>001</n></r>", types=TypePolicy.infer())
    report = doc.test_type("/r/n", "xs:unsignedByte")
    assert [issue.location.path[-1] for issue in report.issues] == [
        PathStep("n", 1),
        PathStep("n", 2),
    ]
    assert [node.scalar_value() for node in doc.root.children] == ["256", "-1", "001"]
    assert doc.report_current


def test_pin_failure_does_not_fall_back_to_a_valid_inferred_value():
    doc = parse_xml(b"<r>42</r>", types=TypePolicy.infer())
    assert doc.root.scalar_value() == 42
    assert doc.pin_type("/r", "xs:boolean").error_count == 1
    with pytest.raises(ScalarTypeError):
        doc.root.scalar_value()


def test_inference_agrees_across_parents_and_distinct_paths():
    doc = parse_xml(
        b'<r><g><n a="1">1</n></g><g><n a="2.5">2.5</n></g>'
        b'<other><n a="3">3</n></other><m>1</m><m>no</m></r>',
        types=TypePolicy.infer(),
    )
    first, second, other = [group.children[0] for group in doc.root.children[:3]]
    for child, expected in [(first, Decimal(1)), (second, Decimal("2.5"))]:
        assert isinstance(child.scalar_value(), Decimal)
        assert isinstance(child.attribute_value("a"), Decimal)
        assert child.scalar_value() == child.attribute_value("a") == expected
    assert type(other.scalar_value()) is xsd.Integer
    assert [child.scalar_value() for child in doc.root.children[3:]] == ["1", "no"]


@pytest.mark.parametrize(
    ("lexical", "kind", "value"),
    [
        ("false", bool, False),
        ("0", xsd.Integer, 0),
        ("1", xsd.Integer, 1),
        ("2e2", xsd.Double, 200.0),
        ("+0.2", xsd.Decimal, Decimal("0.2")),
        (".5", xsd.Decimal, Decimal("0.5")),
        ("2024-02-29Z", xsd.Date, "2024-02-29Z"),
        ("2024-02-29T11:22:33Z", xsd.DateTime, "2024-02-29T11:22:33Z"),
        ("11:22:33.5+02:00", xsd.Time, "11:22:33.5+02:00"),
        ("True", str, "True"),
        (" true ", str, " true "),
        (" 001 ", str, " 001 "),
        ("+01.2", str, "+01.2"),
        ("-01.2e3", str, "-01.2e3"),
        ("NaN", str, "NaN"),
        ("INF", str, "INF"),
        ("-INF", str, "-INF"),
        ("1e999", str, "1e999"),
        ("2023-02-29", str, "2023-02-29"),
    ],
)
def test_inference_candidate_order_and_conservative_lexical_forms(lexical, kind, value):
    doc = parse_xml(io.StringIO(f"<r>{lexical}</r>"), types=TypePolicy.infer())
    actual = doc.root.scalar_value()
    assert type(actual) is kind
    assert actual == value


def test_pins_use_xsd_lexical_rules_and_datatypes():
    doc = parse_xml(b'<r b=" 1 " n="001.2" i="INF" token="  a  b  "/>', types=TypePolicy.infer())
    assert not doc.pin_type("/r/@b", "xs:boolean").has_errors
    assert not doc.pin_type("/r/@n", "xs:decimal").has_errors
    assert not doc.pin_type("/r/@i", "xs:double").has_errors
    assert not doc.pin_type("/r/@token", "xs:token").has_errors
    assert doc.root.attribute_value("b") is True
    assert doc.root.attribute_value("n") == Decimal("1.2")
    assert math.isinf(doc.root.attribute_value("i"))
    assert doc.root.attribute_value("token") == "a b"
    assert doc.root.attribute_lexical("n") == "001.2"


def test_xml_scalar_rules_do_not_inherit_schema_whitespace_compatibility():
    doc = parse_xml(b"<r><n>&#160;12&#160;</n></r>", types=TypePolicy.infer())
    with xsd.whitespace_mode("compat"):
        assert doc.root.children[0].scalar_value() == "\u00a012\u00a0"
        assert doc.pin_type("/r/n", "xs:integer").error_count == 1
        with pytest.raises(ScalarTypeError):
            doc.root.children[0].scalar_value()


def test_indexed_pin_takes_precedence_over_path_wide_pin():
    doc = parse_xml(b"<r><x>001</x><other/><x>0</x><x>003</x></r>")
    first, _, second, third = doc.root.children
    assert not doc.pin_type("/r/x[2]", "xs:boolean").has_errors
    assert [first.scalar_value(), second.scalar_value(), third.scalar_value()] == [
        "001",
        False,
        "003",
    ]
    assert not doc.pin_type("/r/x", "xs:integer").has_errors
    assert [first.scalar_value(), second.scalar_value(), third.scalar_value()] == [1, False, 3]
    assert second.scalar_value() is False
    assert not doc.pin_type("/r/x[2]", "xs:string").has_errors
    assert second.scalar_value() == "0"


def test_expanded_names_with_slashes_and_indexed_attribute_locations():
    doc = parse_xml(
        b'<p:r xmlns:p="https://example.test/a/b"><p:x p:id="bad"/>'
        b'<p:x p:id="001"/><x p:id="002"/></p:r>'
    )
    uri = "{https://example.test/a/b}"
    name = uri + "id"
    first, second, unqualified = doc.root.children
    path = f"/{uri}r/{uri}x"
    assert not doc.pin_type(f"/{uri}r[1]/{uri}x[2]/@{name}", "xs:integer").has_errors
    assert first.attribute_value(name) == "bad"
    assert second.attribute_value(name) == 1
    assert unqualified.attribute_value(name) == "002"
    report = doc.pin_type(f"{path}/@{name}", "xs:integer")
    assert report.error_count == 1
    assert report.issues[0].location.path[-1] == PathStep(uri + "x", 1)
    assert report.issues[0].location.attribute == name
    with pytest.raises(ScalarTypeError) as failure:
        first.attribute_value(name)
    assert failure.value.location == report.issues[0].location


@pytest.mark.parametrize(
    ("path", "type_ref", "code"),
    [
        ("/r/absent", "xs:integer", "unknown-scalar-path"),
        ("/r/x[2]", "xs:integer", "unknown-scalar-path"),
        ("/other/x", "xs:integer", "unknown-scalar-path"),
        ("/r/@absent", "xs:integer", "unknown-scalar-path"),
        ("/r/@a[1]", "xs:integer", "invalid-scalar-path"),
        ("/r/@a/x", "xs:integer", "invalid-scalar-path"),
        ("/@a", "xs:integer", "invalid-scalar-path"),
        ("/r/x[0]", "xs:integer", "invalid-scalar-path"),
        ("/r//x", "xs:integer", "invalid-scalar-path"),
        ("", "xs:integer", "invalid-scalar-path"),
        ("/r[2]", "xs:integer", "unknown-scalar-path"),
        ("r/x", "xs:integer", "invalid-scalar-path"),
        ("/r/x", "xs:UUID", "unknown-scalar-type"),
        ("/r/x", "xs:IDREFS", "unknown-scalar-type"),
        ("/r/x", "{urn:other}integer", "unknown-scalar-type"),
    ],
)
def test_unknown_or_invalid_selectors_return_diagnostics_without_changing_policy(
    path, type_ref, code
):
    doc = parse_xml(b'<r a="003"><x>001</x></r>')
    assert not doc.pin_type("/r/@a", "xs:integer").has_errors
    for operation in (doc.test_type, doc.pin_type):
        report = operation(path, type_ref)
        assert report.error_count == 1
        assert report.issues[0].code == code
    assert doc.root.children[0].scalar_value() == "001"
    assert doc.root.attribute_value("a") == 3
    assert doc.root.attribute_value("absent") is None


def test_custom_uuid_adapter_is_explicit_and_preserves_lexical_storage():
    raw = "12345678-1234-5678-9ABC-123456789ABC"
    uuid = UUID(raw)
    adapter = ScalarAdapter(parse=UUID, format=str)
    doc = parse_xml(io.StringIO(f"<r><id>{raw}</id><id>bad</id></r>"), types=TypePolicy.infer())
    assert adapter.test(raw) and not adapter.test("bad")
    assert adapter.format(uuid) == "12345678-1234-5678-9abc-123456789abc"
    assert adapter.to_xdm(uuid) == XdmAtomic.string("12345678-1234-5678-9abc-123456789abc")
    assert doc.root.children[0].scalar_value() == raw
    assert doc.pin_type("/r/id", adapter).error_count == 1
    assert doc.root.children[0].scalar_value() == uuid
    with pytest.raises(ScalarTypeError):
        doc.root.children[1].scalar_value()
    assert doc.root.children[0].text_value() == raw


def test_builtin_adapters_format_and_build_xdm_atomics():
    policy = TypePolicy.native().with_pin("/r", "xs:boolean")
    boolean = policy.resolve("xs:boolean")
    assert boolean.parse("1") is True
    assert boolean.format(False) == "false"
    assert boolean.to_xdm(True) == XdmAtomic.boolean(True)
    assert boolean.to_xdm(True).type_name == "{http://www.w3.org/2001/XMLSchema}boolean"
    integer = policy.resolve("{http://www.w3.org/2001/XMLSchema}integer")
    assert integer.format(integer.parse("001")) == "1"
    assert integer.to_xdm(2) == XdmAtomic.integer(2)
    assert policy.resolve("xs:double").format(float("inf")) == "INF"


def test_custom_adapter_can_supply_an_xdm_conversion():
    adapter = ScalarAdapter(parse=int, format=str, xdm=XdmAtomic.integer)
    assert (
        adapter.to_xdm(adapter.parse("001")).type_name
        == "{http://www.w3.org/2001/XMLSchema}integer"
    )


def test_policy_replacement_is_immutable_and_local_to_the_document():
    original = TypePolicy.lexical()
    configured = original.with_pin("/r", "xs:integer")
    with pytest.raises(FrozenInstanceError):
        configured.mode = "infer"
    with pytest.raises(TypeError):
        configured.pins["/r"] = ScalarAdapter(parse=str, format=str)

    first = parse_xml(ET.fromstring("<r>001</r>"), types=configured)
    second = parse_xml(b"<r>001</r>", types=configured)
    untouched = parse_xml(b"<r>001</r>", types=original)
    assert first.root.revision.types is second.root.revision.types is configured
    with pytest.raises(AttributeError):
        first.root.revision.types = original
    assert first.root.scalar_value() == second.root.scalar_value() == 1
    assert not first.pin_type("/r", "xs:string").has_errors
    assert first.root.revision.types is not configured
    assert first.root.scalar_value() == untouched.root.scalar_value() == "001"
    assert second.root.scalar_value() == 1
    assert parse_xml(b"<r>12</r>", types=TypePolicy.native()).root.scalar_value() == "12"


def test_pin_only_changes_python_reads_leaving_xdm_current_and_untyped():
    doc = parse_xml(b'<r a="001">002</r>', types=TypePolicy.infer())
    xdm = doc.to_xdm()
    assert not doc.pin_type("/r", "xs:integer").has_errors
    assert not doc.pin_type("/r/@a", "xs:integer").has_errors
    assert doc.report_current and not doc.root.revision.dirty
    assert doc.to_xdm() is xdm
    assert doc.root.scalar_value() == 2
    assert doc.root.attribute_value("a") == 1
    assert xdm.root_element.annotation.type_name == UNTYPED
    assert xdm.root_element.string_value == "002"
    assert xdm.root_element.attributes[0].annotation.type_name == UNTYPED_ATOMIC
    assert xdm.root_element.attributes[0].string_value == "001"


def test_typed_exports_use_scalar_and_attribute_pins_for_supported_values():
    doc = parse_xml(b'<r a="001"><n>002</n></r>')
    assert not doc.pin_type("/r/@a", "xs:integer").has_errors
    assert not doc.pin_type("/r/n", "xs:integer").has_errors
    assert doc.to_dict() == doc.to_data() == {"@a": 1, "n": 2}
    assert doc.to_dict(typed=False) == doc.to_data(typed=False) == {"@a": "001", "n": "002"}


def test_typed_reads_recompute_after_text_attribute_and_occurrence_edits():
    doc = parse_xml(b'<r><x a="001">1</x><x a="002">2</x></r>', types=TypePolicy.infer())
    first, second = doc.root.children
    assert first.scalar_value() == 1
    second.set_text("bad")
    assert first.scalar_value() == "1"
    assert not doc.pin_type("/r/x[2]/@a", "xs:integer").has_errors
    assert second.attribute_value("a") == 2
    second.set_attribute("a", "009")
    assert second.attribute_value("a") == 9
    doc.root.insert_child(0, doc.root.remove_child(second))
    assert second.attribute_value("a") == "009"
    assert first.attribute_value("a") == 1
    first.delete_attribute("a")
    assert first.attribute_value("a") is None


def test_empty_element_counts_as_an_occurrence_in_inference_and_pins():
    doc = parse_xml(b"<r><n>2</n><n/></r>", types=TypePolicy.infer())
    first, second = doc.root.children
    assert first.scalar_value() == "2"
    assert second.scalar_value() is None
    report = doc.pin_type("/r/n", "xs:integer")
    assert report.error_count == 1
    with pytest.raises(ScalarTypeError):
        second.scalar_value()
    second.set_text("4")
    assert second.scalar_value() == 4
    second.clear_text()
    assert second.text_value() is None
    assert not doc.pin_type("/r/n[2]", "xs:string").has_errors
    assert second.scalar_value() == ""


def test_revalidation_keeps_the_policy_but_recomputes_from_lexical_values():
    doc = parse_xml(b"<r><n>001</n><m>true</m></r>", types=TypePolicy.infer())
    assert not doc.pin_type("/r/n", "xs:integer").has_errors
    doc.root.children[0].set_text("002")
    fresh = doc.revalidate()
    assert fresh.root.children[0].scalar_value() == 2
    assert fresh.root.children[1].scalar_value() is True
    assert fresh.to_xdm().root_element.string_value == "002true"
    assert not fresh.pin_type("/r/n", "xs:string").has_errors
    assert doc.root.children[0].scalar_value() == 2


def test_invalid_preconfigured_xml_path_is_not_silently_ignored():
    with pytest.raises(ValueError, match="XML scalar path"):
        parse_xml(b"<r/>", types=TypePolicy.lexical().with_pin("/r[0]", "xs:integer"))


def test_builtin_qname_pin_uses_the_occurrence_namespace_bindings():
    doc = parse_xml(b'<r xmlns:p="urn:p"><x>p:value</x><x>q:unbound</x></r>')
    report = doc.pin_type("/r/x", "xs:QName")
    assert report.error_count == 1
    assert report.issues[0].location.path[-1] == PathStep("x", 2)
    value = doc.root.children[0].scalar_value()
    assert value.to_expanded_name(strict=True) == "{urn:p}value"
    assert TypePolicy.lexical().resolve("xs:QName").to_xdm(value).value is value
    assert not doc.pin_type("/r/x[2]", "xs:string").has_errors
    assert doc.root.children[1].scalar_value() == "q:unbound"


def test_caller_built_document_without_source_reports_the_pinned_occurrence():
    root = SchemalessElement.from_etree(ET.fromstring("<r><n>bad</n></r>"))
    doc = SchemalessDocument(root, None, ValidationReport())
    location = doc.pin_type("/r/n", "xs:integer").issues[0].location
    assert location == IssueLocation(None, (PathStep("r", 1), PathStep("n", 1)))
    with pytest.raises(ScalarTypeError) as failure:
        root.children[0].scalar_value()
    assert failure.value.location == location


def test_detachment_does_not_apply_a_pin_to_an_unrelated_root():
    doc = parse_xml(b"<n>001<n>002</n></n>", types=TypePolicy.infer())
    assert not doc.pin_type("/n", "xs:integer").has_errors
    detached = doc.root.remove_child(0)
    assert detached.scalar_value() == "002"
    detached.set_text("3")
    assert detached.scalar_value() == 3


def test_transformed_subtree_shares_the_revision_pins_and_absolute_paths():
    doc = parse_xml(b"<r><n>001</n></r>")
    child_doc = doc.transform(lambda root: root.children[0])
    assert not child_doc.pin_type("/r/n", "xs:integer").has_errors
    assert child_doc.root.scalar_value() == doc.root.children[0].scalar_value() == 1


def test_indexed_path_is_one_occurrence_and_equivalent_paths_use_the_latest_pin():
    doc = parse_xml(b"<r><g><n>001</n><n>1</n></g><g><n>003</n><n>0</n></g></r>")
    first, second = doc.root.children
    assert not doc.pin_type("/r/g/n[2]", "xs:boolean").has_errors
    assert first.children[1].scalar_value() is True
    assert second.children[1].scalar_value() == "0"
    assert not doc.pin_type("/r/g[1]/n[2]", "xs:integer").has_errors
    assert type(first.children[1].scalar_value()) is xsd.Integer
    assert not doc.pin_type("/r/g/n[2]", "xs:boolean").has_errors
    assert first.children[1].scalar_value() is True
