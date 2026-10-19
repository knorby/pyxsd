"""Coverage tests for the instance-binding error and fallback paths.

Covers the root-name mismatch guard, ``xsi:nil`` on complex/ur-type roots,
the primitive-root fixed-value path, the substitution-group head type
lookup, and the namespaced schema-location rewrite.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pyxsd.binding import ParseModes
from pyxsd.instance_binding import generate_correct_schema_tags
from pyxsd.schema import Schema
from pyxsd.validation import ValidationReport

XSD_HEAD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'"
XSD_TAIL = "</xs:schema>"


def _compile(tmp_path: Path, schema_body: str, xml: str, *, version: str = "1.1"):
    """Compile *schema_body* and bind *xml*, returning the document."""
    schema_path = tmp_path / "schema.xsd"
    instance_path = tmp_path / "instance.xml"
    schema_path.write_text(schema_body, encoding="utf-8")
    instance_path.write_text(xml, encoding="utf-8")
    schema = Schema.compile(str(schema_path), mode=ParseModes.NAMESPACED, xsd_version=version)
    return schema, schema.parse(str(instance_path))


def _codes(document):
    return [issue.code for issue in document.report.errors]


# ---------------------------------------------------------------------------
# Root-name mismatch
# ---------------------------------------------------------------------------


def test_a_matching_top_level_element_reference_binds_no_root(tmp_path):
    """A top-level ``ref`` site whose expanded name matches the root returns ``None``.

    The declaration walk already reported the misplaced reference; binding
    must not fall through to ``unknown-root`` (or a bound tree) for the
    reference site whose bookkeeping name differs from the instance tag.
    """
    schema_body = (
        XSD_HEAD
        + " xmlns:u='urn:y' targetNamespace='urn:x' xmlns:t='urn:x' elementFormDefault='qualified'>"
        "<xs:element name='g' type='xs:string'/>"
        "<xs:element ref='u:g'/>" + XSD_TAIL
    )
    schema, document = _compile(tmp_path, schema_body, "<g xmlns='urn:y'>hello</g>")

    assert document.root is None
    assert "unknown-root" not in _codes(document)
    ref_errors = [
        issue for issue in document.report.errors if "must not carry a ref" in issue.message
    ]
    assert len(ref_errors) == 1
    assert any("must not carry a ref" in issue.message for issue in schema.report.errors)


# ---------------------------------------------------------------------------
# Nilled roots
# ---------------------------------------------------------------------------


def test_nilled_complex_root_with_a_fixed_value_reports_nil(tmp_path):
    schema_body = (
        XSD_HEAD + "><xs:element name='root' fixed='x' nillable='true'>"
        "<xs:complexType><xs:sequence>"
        "<xs:element name='a' type='xs:string' minOccurs='0'/>"
        "</xs:sequence></xs:complexType></xs:element>" + XSD_TAIL
    )
    _schema, document = _compile(
        tmp_path,
        schema_body,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>',
    )

    nil_errors = [issue for issue in document.report.errors if issue.code == "nil"]
    assert len(nil_errors) == 1
    assert "fixed value" in nil_errors[0].message


def test_anytype_root_with_children_binds_them_through_the_wildcard(tmp_path):
    _schema, document = _compile(
        tmp_path,
        XSD_HEAD + "><xs:element name='root'/>" + XSD_TAIL,
        "<root><child>v</child></root>",
    )

    assert document.report.errors == []
    assert type(document.root).__name__ == "AnyType"
    assert [child._name_ for child in document.root._children_] == ["child"]


def test_anytype_nil_on_a_non_nillable_root_reports_nil_and_binds_children(tmp_path):
    _schema, document = _compile(
        tmp_path,
        XSD_HEAD + "><xs:element name='root'/>" + XSD_TAIL,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"><c/></root>',
    )

    nil_errors = [issue for issue in document.report.errors if issue.code == "nil"]
    assert len(nil_errors) == 1
    assert "not nillable but carries xsi:nil" in nil_errors[0].message
    # ``nilled`` is reset, so the ur-type content path still binds the child.
    assert type(document.root).__name__ == "AnyType"
    assert [child._name_ for child in document.root._children_] == ["c"]


def test_nilled_anytype_root_with_children_reports_nil_and_binds_empty(tmp_path):
    _schema, document = _compile(
        tmp_path,
        XSD_HEAD + "><xs:element name='root' nillable='true'/>" + XSD_TAIL,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"><c/></root>',
    )

    nil_errors = [issue for issue in document.report.errors if issue.code == "nil"]
    assert len(nil_errors) == 1
    assert "contains child elements" in nil_errors[0].message
    assert document.root._value_ is None
    assert document.root._children_ == []


def test_nilled_primitive_root_with_child_elements_is_reported(tmp_path):
    _schema, document = _compile(
        tmp_path,
        XSD_HEAD + "><xs:element name='root' type='xs:string' nillable='true'/>" + XSD_TAIL,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"><c/></root>',
    )

    nil_errors = [issue for issue in document.report.errors if issue.code == "nil"]
    assert len(nil_errors) == 1
    assert "contains child elements" in nil_errors[0].message
    assert document.root._value_ is None


# ---------------------------------------------------------------------------
# Primitive-root fixed values
# ---------------------------------------------------------------------------


def test_a_fixed_value_that_is_invalid_for_the_type_is_not_enforced(tmp_path):
    """An unconvertible fixed value leaves the instance value untouched.

    The declaration walk reports the invalid ``fixed``; binding must not
    add a ``fixed-element`` conflict on top of it.
    """
    _schema, document = _compile(
        tmp_path,
        XSD_HEAD + "><xs:element name='root' type='xs:int' fixed='notanint'/>" + XSD_TAIL,
        "<root>5</root>",
    )

    assert type(document.root).__name__ == "Int"
    assert document.root._value_ == ["5"]
    assert "fixed-element" not in _codes(document)


# ---------------------------------------------------------------------------
# Substitution-group head types
# ---------------------------------------------------------------------------


def test_an_untyped_member_takes_the_heads_type(tmp_path):
    schema_body = (
        XSD_HEAD + "><xs:element name='marker' type='xs:string'/>"
        "<xs:element name='dot' substitutionGroup='marker'/>" + XSD_TAIL
    )
    _schema, document = _compile(tmp_path, schema_body, "<dot>x</dot>")

    assert document.report.errors == []
    assert type(document.root).__name__ == "String"


def test_a_member_without_a_head_declaration_binds_as_the_ur_type(tmp_path):
    """A dangling ``substitutionGroup`` leaves the head type unresolved."""
    schema_body = (
        XSD_HEAD + "><xs:element name='dot' substitutionGroup='marker'/>"
        "<xs:element name='decoy' type='xs:string'/>" + XSD_TAIL
    )
    _schema, document = _compile(tmp_path, schema_body, "<dot>x</dot>")

    assert type(document.root).__name__ == "AnyType"
    assert "unknown-substitution-head" in _codes(document)


def test_a_member_of_an_untyped_head_binds_as_the_ur_type(tmp_path):
    schema_body = (
        XSD_HEAD + "><xs:element name='marker'/>"
        "<xs:element name='dot' substitutionGroup='marker'/>" + XSD_TAIL
    )
    _schema, document = _compile(tmp_path, schema_body, "<dot>x</dot>")

    assert document.report.errors == []
    assert type(document.root).__name__ == "AnyType"


# ---------------------------------------------------------------------------
# Namespaced schema-location rewrite
# ---------------------------------------------------------------------------


def test_known_defect_single_schema_location_hint_is_rewritten_to_the_no_namespace_form():
    """Pins today's single-hint rewrite (KNOWN DEFECTIVE behaviour, not a contract).

    The namespace is re-read only after the ``schemaLocation`` attribute
    has been deleted, so a namespaced hint is rewritten to the
    ``noNamespaceSchemaLocation`` form instead of ``schemaLocation``. This is
    a pyxsd bug; the test exists only to pin today's output so a fix is
    visible. See
    ``docs/superpowers/ledgers/2026-10-18-coverage-burndown/task-6-report.md``.
    """
    # Pins current (wrong) behaviour; a namespaced hint must not decay to the
    # no-namespace form. See the task report for the tracked defect.
    root = ET.fromstring(
        '<t:root xmlns:t="urn:x"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:schemaLocation="urn:x schema.xsd"/>'
    )
    report = ValidationReport()

    assert generate_correct_schema_tags(None, root, report) is None

    assert root.attrib["xmlns:xsi"] == "http://www.w3.org/2001/XMLSchema-instance"
    assert root.attrib["xsi:noNamespaceSchemaLocation"] == "urn:x schema.xsd"
    assert "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation" not in root.attrib
    assert report.errors == []

    # Positive invariant: the rewrite is confined to the hint attributes, so
    # the rewritten document is still well-formed XML with its root intact.
    rewritten = ET.fromstring(ET.tostring(root))
    assert rewritten.tag == "{urn:x}root"


def test_a_document_with_both_schema_location_hints_binds_its_root(tmp_path):
    """Both hints present: the namespace branch of the rewrite runs.

    ``schema_location_info`` prefers the no-namespace tag for the tag
    name, so only that attribute is deleted and the re-read at the
    namespace step still sees ``xsi:schemaLocation`` — the namespace
    branch executes and the root still binds.
    """
    schema_body = XSD_HEAD + "><xs:element name='root' type='xs:string'/>" + XSD_TAIL
    xml = (
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:schemaLocation="urn:x schema.xsd"'
        ' xsi:noNamespaceSchemaLocation="schema.xsd">hi</root>'
    )
    _schema, document = _compile(tmp_path, schema_body, xml)

    assert document.root is not None
    assert type(document.root).__name__ == "String"
    assert document.report.errors == []


def test_known_defect_both_schema_location_hints_emit_an_invalid_namespace_declaration():
    """Pins today's both-hint rewrite (KNOWN DEFECTIVE behaviour, not a contract).

    With both hints present the rewrite keeps the original
    ``xsi:schemaLocation`` Clark key, adds a *literal* ``xsi:schemaLocation``
    key carrying the other value, and emits ``xmlns:urn:x`` — a namespace
    declaration whose prefix contains a colon, which is invalid XML. This is
    a pyxsd bug; the test exists only to pin today's output so a fix is
    visible. See
    ``docs/superpowers/ledgers/2026-10-18-coverage-burndown/task-6-report.md``.
    """
    # Pins current (wrong) behaviour; the emitted namespace declaration cannot
    # be serialized back to well-formed XML. See the task report.
    root = ET.fromstring(
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:schemaLocation="urn:x schema.xsd"'
        ' xsi:noNamespaceSchemaLocation="schema.xsd"/>'
    )
    report = ValidationReport()

    assert generate_correct_schema_tags(None, root, report) is None

    assert (
        root.attrib["{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"]
        == "urn:x schema.xsd"
    )
    assert root.attrib["xsi:schemaLocation"] == "schema.xsd"
    assert root.attrib["xmlns"] == "urn:x"
    assert root.attrib["xmlns:urn:x"] == "urn:x"
    assert report.errors == []

    # Positive invariant: the rewrite only adds attributes, so the element's
    # own name survives. The document still binds via the companion test
    # ``test_a_document_with_both_schema_location_hints_binds_its_root``.
    assert root.tag == "root"
    # Defect consequence: the rewritten tree cannot be re-parsed as XML.
    with pytest.raises(ET.ParseError):
        ET.fromstring(ET.tostring(root))
