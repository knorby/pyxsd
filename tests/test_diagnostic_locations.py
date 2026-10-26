"""Diagnostics identify source occurrences, never equal bound values."""

import io

import pytest

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:simpleType name="Positive"><xs:restriction base="xs:int">
<xs:minInclusive value="0"/></xs:restriction></xs:simpleType>
<xs:element name="root"><xs:complexType><xs:sequence>
<xs:element name="item" type="Positive" maxOccurs="unbounded"/>
</xs:sequence><xs:attribute name="count" type="Positive" use="required"/>
</xs:complexType></xs:element></xs:schema>"""


def compile_schema(mode=ParseModes.NAMESPACED):
    return Schema.compile(io.StringIO(XSD), mode=mode)


@pytest.mark.parametrize(
    "mode,status",
    [(ParseModes.NAMESPACED, "unbound"), (ParseModes.LAX.replace(namespaces="strict"), "bound")],
)
def test_equal_primitive_failures_locate_their_own_occurrences(mode, status):
    schema = compile_schema(mode)
    document = schema.parse(io.StringIO('<root count="1"><item>-1</item><item>-1</item></root>'))
    issues = [issue for issue in document.report.errors if issue.code == "value"]
    assert len(issues) == 2
    assert [issue.location.path[-1].index for issue in issues] == [1, 2]
    assert [issue.location.path[-1].name for issue in issues] == ["item", "item"]
    assert all(issue.location_precision == "exact" for issue in issues)
    assert all(issue.constraint.kind == "minInclusive" for issue in issues)
    found = [document.locate(issue.location) for issue in issues]
    assert [entry.status for entry in found] == [status, status]
    if status == "bound":
        assert found[0].bound is document.root.item[0]
        assert found[1].bound is document.root.item[1]
        assert found[0].bound is not found[1].bound
    else:
        assert all(entry.bound is None for entry in found)


def test_invalid_attribute_has_its_attribute_address_and_constraint():
    document = compile_schema().parse(io.StringIO('<root count="-1"><item>1</item></root>'))
    issue = next(issue for issue in document.report.errors if issue.code == "invalid-attribute")
    assert issue.location.attribute == "count"
    assert [(step.name, step.index) for step in issue.location.path] == [("root", 1)]
    assert issue.constraint.kind == "minInclusive"
    lookup = document.locate(issue.location)
    assert lookup.status == "attribute"
    assert lookup.bound == "-1"


def test_unbound_unknown_child_keeps_a_source_address():
    document = compile_schema().parse(io.StringIO('<root count="1"><item>1</item><extra/></root>'))
    issue = next(issue for issue in document.report.errors if issue.code == "unexpected-element")
    assert issue.location.path[-1].name == "extra"
    assert issue.location_precision == "exact"
    assert document.locate(issue.location).status == "unbound"


def test_missing_child_identifies_container_and_expected_declaration():
    document = compile_schema().parse(io.StringIO('<root count="1"/>'))
    issue = next(issue for issue in document.report.errors if issue.code == "occurrence-min")
    assert [(step.name, step.index) for step in issue.location.path] == [("root", 1)]
    assert issue.location_precision == "container"
    assert issue.details["expected"] == "item"
    assert document.locate(issue.location).bound is document.root


def test_second_parse_does_not_retarget_first_document():
    from pyxsd.locations import IssueLocation, PathStep

    schema = compile_schema(ParseModes.LAX.replace(namespaces="strict"))
    first = schema.parse(io.StringIO('<root count="1"><item>-1</item></root>'))
    issue = next(issue for issue in first.report.errors if issue.code == "value")
    second = schema.parse(io.StringIO('<root count="1"><item>2</item></root>'))
    assert first.locate(issue.location).bound is first.root.item[0]
    assert second.locate(issue.location).bound is second.root.item[0]
    assert int(second.root.item[0]) == 2
    assert first.locate(IssueLocation(None, (PathStep("absent", 1),))).status == "missing"
    assert first.locate(IssueLocation(None, (PathStep("root", 1),), "absent")).status == "missing"


def test_unknown_root_has_unbound_location(tmp_path):
    path = tmp_path / "unknown.xml"
    path.write_text("<unknown/>", encoding="utf-8")
    document = compile_schema().parse(path)
    issue = next(issue for issue in document.report.errors if issue.code == "unknown-root")
    assert issue.location.source == str(path)
    assert issue.location.path[0].name == "unknown"
    assert document.locate(issue.location).status == "unbound"


def test_primitive_root_preserves_constraint_evidence():
    schema = Schema.compile(
        io.StringIO("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
    <xs:element name="root"><xs:simpleType><xs:restriction base="xs:int">
    <xs:minInclusive value="0"/></xs:restriction></xs:simpleType></xs:element></xs:schema>""")
    )
    document = schema.parse(io.StringIO("<root>-1</root>"))
    issue = next(issue for issue in document.report.errors if issue.code == "value")
    assert issue.constraint.kind == "minInclusive"
    assert issue.location.path[0].name == "root"
    assert issue.location_precision == "exact"


def test_assertions_attach_to_the_selected_complex_occurrence():
    schema = Schema.compile(
        io.StringIO("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
    <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="item" maxOccurs="unbounded"><xs:complexType>
    <xs:attribute name="count" type="xs:int"/><xs:assert test="@count gt 0"/>
    </xs:complexType></xs:element></xs:sequence></xs:complexType></xs:element></xs:schema>""")
    )
    assert not schema.report.has_errors
    document = schema.parse(io.StringIO('<root><item count="1"/><item count="0"/></root>'))
    issue = next(issue for issue in document.report.errors if issue.code == "assert-failed")
    assert [(step.name, step.index) for step in issue.location.path] == [("root", 1), ("item", 2)]
    assert issue.location_precision == "exact"
    assert document.locate(issue.location).bound is document.root._children_[1]


def test_duplicate_key_identifies_duplicate_and_first_occurrence():
    schema = Schema.compile(
        io.StringIO("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
    <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="item" type="xs:int" maxOccurs="unbounded"/>
    </xs:sequence></xs:complexType><xs:key name="ids"><xs:selector xpath="item"/>
    <xs:field xpath="."/></xs:key></xs:element></xs:schema>""")
    )
    document = schema.parse(io.StringIO("<root><item>1</item><item>1</item><item>1</item></root>"))
    duplicates = [issue for issue in document.report.errors if issue.code == "identity-key"]
    assert len(duplicates) == 2
    assert all(issue.details["related_locations"][0].path[-1].index == 1 for issue in duplicates)
    issue = duplicates[0]
    assert issue.location.path[-1].index == 2
    assert issue.location.path[-1].name == "item"
    assert issue.location_precision == "exact"
    related = issue.details["related_locations"]
    assert len(related) == 1
    assert related[0].path[-1].index == 1
    assert document.locate(related[0]).bound is document.root.item[0]


def test_colliding_local_names_are_different_source_occurrences():
    schema = Schema.compile(
        io.StringIO("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
    <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="item" type="xs:int" maxOccurs="unbounded"/>
    </xs:sequence></xs:complexType></xs:element></xs:schema>"""),
        mode=ParseModes.NAMESPACED,
    )
    document = schema.parse(
        io.StringIO(
            '<root xmlns:a="https://example.org/a" xmlns:b="https://example.org/b"><item>1</item><a:item/><b:item/></root>'
        )
    )
    issues = [issue for issue in document.report.errors if issue.code == "unexpected-element"]
    assert {issue.location.path[-1].name for issue in issues} == {
        "{https://example.org/a}item",
        "{https://example.org/b}item",
    }
    assert all(issue.location.path[-1].index == 1 for issue in issues)
    assert all(document.locate(issue.location).status == "unbound" for issue in issues)


@pytest.mark.parametrize(
    "attributes,code",
    [
        ('xsi:nil="true"', "nil"),
        ('xsi:type="missing"', "xsi-type"),
    ],
)
def test_nil_and_type_failures_locate_the_child(attributes, code):
    document = compile_schema().parse(
        io.StringIO(
            '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" count="1">'
            f"<item>1</item><item {attributes}>2</item></root>"
        )
    )
    issue = next(issue for issue in document.report.errors if issue.code == code)
    assert issue.location.path[-1].name == "item"
    assert issue.location.path[-1].index == 2
    assert issue.location_precision == "exact"


def test_revalidation_rebuilds_locations_for_current_occurrences():
    from pyxsd.locations import IssueLocation, PathStep

    document = compile_schema(ParseModes.LAX.replace(namespaces="strict")).parse(
        io.StringIO('<root count="1"><item>-1</item></root>')
    )
    import copy

    original = document.root.item[0]
    document.root._children_.append(copy.copy(original))
    again = document.revalidate()
    assert [
        issue.location.path[-1].index for issue in again.report.errors if issue.code == "value"
    ] == [1, 2]
    assert (
        document.locate(IssueLocation(None, (PathStep("root", 1), PathStep("item", 2)))).status
        == "missing"
    )


def test_manually_created_documents_do_not_fabricate_provenance():
    from pyxsd.document import Document
    from pyxsd.locations import IssueLocation, PathStep
    from pyxsd.validation import ValidationReport

    document = Document(compile_schema(), None, None, ValidationReport())
    assert document.locate(IssueLocation(None, (PathStep("root", 1),))).status == "missing"


def test_idref_and_duplicate_id_keep_attribute_locations():
    schema = Schema.compile(
        io.StringIO("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
    <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="item" maxOccurs="unbounded"><xs:complexType>
    <xs:attribute name="id" type="xs:ID"/><xs:attribute name="ref" type="xs:IDREF"/>
    </xs:complexType></xs:element></xs:sequence></xs:complexType></xs:element></xs:schema>""")
    )
    document = schema.parse(io.StringIO('<root><item id="a"/><item id="a" ref="missing"/></root>'))
    duplicate = next(issue for issue in document.report.errors if issue.code == "id-duplicate")
    reference = next(issue for issue in document.report.errors if issue.code == "idref-unresolved")
    assert duplicate.location.path[-1].index == 2
    assert duplicate.location.attribute == "id"
    assert duplicate.details["related_locations"][0].path[-1].index == 1
    assert reference.location.path[-1].index == 2
    assert reference.location.attribute == "ref"


def test_keyref_failure_locates_selected_record():
    schema = Schema.compile(
        io.StringIO("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
    <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="item" maxOccurs="unbounded"><xs:complexType>
    <xs:attribute name="id" type="xs:int"/><xs:attribute name="ref" type="xs:int"/>
    </xs:complexType></xs:element></xs:sequence></xs:complexType>
    <xs:key name="ids"><xs:selector xpath="item"/><xs:field xpath="@id"/></xs:key>
    <xs:keyref name="refs" refer="ids"><xs:selector xpath="item"/><xs:field xpath="@ref"/></xs:keyref>
    </xs:element></xs:schema>""")
    )
    document = schema.parse(io.StringIO('<root><item id="1"/><item id="2" ref="5"/></root>'))
    issue = next(issue for issue in document.report.errors if issue.code == "identity-keyref")
    assert issue.location.path[-1].index == 2
    assert document.locate(issue.location).bound is document.root._children_[1]


def test_missing_attribute_location_is_not_an_existing_attribute():
    document = compile_schema().parse(io.StringIO("<root><item>1</item></root>"))
    issue = next(issue for issue in document.report.errors if issue.code == "missing-attribute")
    assert issue.location.attribute == "count"
    assert issue.location_precision == "container"
    assert document.locate(issue.location).status == "missing"


@pytest.mark.parametrize("severity", ["add_error", "add_warning"])
def test_precision_without_source_evidence_is_unknown(severity):
    from pyxsd.validation import ValidationReport

    report = ValidationReport()
    getattr(report, severity)("no source", code="value", location_precision="exact")
    assert report.issues[0].location is None
    assert report.issues[0].location_precision == "unknown"


def test_location_scope_restores_after_exception_and_keeps_explicit_evidence():
    from pyxsd.locations import IssueLocation, PathStep
    from pyxsd.validation import ValidationReport

    report = ValidationReport()
    outer = IssueLocation(None, (PathStep("outer", 1),))
    inner = IssueLocation(None, (PathStep("inner", 1),))
    with pytest.raises(ValueError), report._at(outer):
        report.add_warning("scoped", code="scope", location_precision="container")
        report.add_warning("explicit", code="scope", location=inner, location_precision="component")
        raise ValueError("stop")
    report.add_error("outside", code="scope")
    assert [(issue.location, issue.location_precision) for issue in report] == [
        (outer, "container"),
        (inner, "component"),
        (None, "unknown"),
    ]


def test_manual_checks_have_no_parse_location_or_cross_parse_lookup():
    from xml.etree import ElementTree as ET

    from pyxsd.schema_context import bound_location

    schema = compile_schema()
    document = schema.parse(io.StringIO('<root count="1"><item>1</item></root>'))
    document.root.checkAttributes({}, ET.fromstring("<root/>"))
    issue = next(issue for issue in schema.report.errors if issue.code == "missing-attribute")
    assert issue.location is None
    assert issue.location_precision == "unknown"
    assert bound_location(document.root) is None
