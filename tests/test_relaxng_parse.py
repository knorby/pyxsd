"""Public validation-only RELAX NG parsing and per-parse report isolation.

These tests pin the Phase 2 source contract: paths, readable streams, and
caller-trusted pre-parsed elements all reach one lexical RELAX NG tree and one
fresh instance report.  Unsupported typed parsing and invalid capture options
are rejected before any input is consumed, schema-report errors are rejected
before instance input is read, and DTD defaults or XSI behavior are never
synthesized.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

import pyxsd.relaxng as relaxng
from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.locations import PathStep
from pyxsd.namespaces import parse_with_namespaces
from pyxsd.relaxng import (
    RelaxNGDocument,
    RelaxNGElement,
    RelaxNGLimitError,
    RelaxNGLimits,
    RelaxNGSchema,
)
from pyxsd.relaxng.diagnostics import MISSING_ATTRIBUTE, UNEXPECTED_ELEMENT
from pyxsd.resource_policy import ResourceData
from pyxsd.schema_base import SchemaBase
from pyxsd.validation import IssueSeverity
from pyxsd.xdm.annotations import UNTYPED, UNTYPED_ATOMIC
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

NS = "http://relaxng.org/ns/structure/1.0"

SIMPLE_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <optional><element name="item"><text/></element></optional>
    </element>
  </start>
</grammar>"""

REQUIRED_ATTRIBUTE_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <attribute name="flag"><text/></attribute>
      <empty/>
    </element>
  </start>
</grammar>"""

MIXED_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <mixed>
        <zeroOrMore><element name="item"><text/></element></zeroOrMore>
      </mixed>
    </element>
  </start>
</grammar>"""

XSI_ATTRIBUTE_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <zeroOrMore><attribute><anyName/></attribute></zeroOrMore>
      <mixed>
        <zeroOrMore><element name="item"><empty/></element></zeroOrMore>
      </mixed>
    </element>
  </start>
</grammar>"""

BROKEN_SCHEMA = f"""<grammar xmlns="{NS}"/>"""

VALID_XML = "<doc/>"
INVALID_XML = "<other/>"
INVALID_CHILD_XML = "<doc><unexpected/></doc>"


def compile_schema(source: str = SIMPLE_SCHEMA) -> RelaxNGSchema:
    return RelaxNGSchema.compile(io.StringIO(source))


class UnreadableStream:
    """A readable-looking stream that records and fails every read."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self, *args: object) -> str:
        self.reads += 1
        raise AssertionError("instance input was consumed")


class FlushRecorder(io.StringIO):
    """A caller-owned writer that records explicit flushes."""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class RecordingPolicy:
    """A minimal trusted policy routing reads and parses through one parser."""

    def __init__(self) -> None:
        self.purposes: list[str] = []

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        del base
        self.purposes.append(purpose)
        return ResourceData(source, Path(source).read_bytes())

    def parse(self, data: ResourceData, context: object) -> ET.Element:
        return parse_with_namespaces(io.BytesIO(data.content), context)  # type: ignore[arg-type]


def test_parse_accepts_paths_streams_and_preparsed_elements(tmp_path):
    schema = compile_schema()
    path = tmp_path / "document.xml"
    path.write_text(VALID_XML, encoding="utf-8")

    from_path = schema.parse(path, typed=False)
    assert from_path.typed is False
    assert isinstance(from_path, RelaxNGDocument)
    assert isinstance(from_path.root, RelaxNGElement)
    assert from_path.root._name_ == "doc"
    assert from_path.source == path
    assert from_path.is_valid
    assert not from_path.report.has_errors

    from_string = os.fspath(path)
    from_named_path = schema.parse(from_string, typed=False)
    assert isinstance(from_named_path.root, RelaxNGElement)
    assert from_named_path.source == from_string

    text_stream = io.StringIO(VALID_XML)
    from_text_stream = schema.parse(text_stream, typed=False)
    assert from_text_stream.source is text_stream
    assert from_text_stream.is_valid

    binary_stream = io.BytesIO(VALID_XML.encode("utf-8"))
    from_binary_stream = schema.parse(binary_stream, typed=False)
    assert from_binary_stream.source is binary_stream
    assert from_binary_stream.is_valid

    element = ET.fromstring(VALID_XML)
    from_element = schema.parse(element, typed=False)
    assert from_element.source is element
    assert from_element.is_valid
    # The caller-trusted boundary ignores parser options, as in XSD parse.
    from_element_with_limits = schema.parse(
        ET.fromstring("<doc><item>x</item></doc>"),
        typed=False,
        xml_limits=XmlLimits(max_elements=1),
    )
    assert from_element_with_limits.is_valid


def test_compilation_resource_policy_governs_instance_reads(tmp_path):
    schema_path = tmp_path / "schema.rng"
    schema_path.write_text(SIMPLE_SCHEMA, encoding="utf-8")
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text("<doc><item>x</item></doc>", encoding="utf-8")
    policy = RecordingPolicy()
    schema = RelaxNGSchema.compile(schema_path, resource_policy=policy)
    assert policy.purposes == ["schema"]

    document = schema.parse(instance_path, typed=False)
    assert document.is_valid

    captured = schema.parse(instance_path, typed=False, capture_xdm=True)
    assert policy.purposes == ["schema", "instance", "instance"]
    assert captured.to_xdm().root_element.name == "doc"

    malformed = tmp_path / "malformed.xml"
    malformed.write_text("<doc>", encoding="utf-8")
    with pytest.raises(PyXSDError, match="not well-formed"):
        schema.parse(malformed, typed=False)

    with pytest.raises(PyXSDError, match="could not be read"):
        schema.parse(tmp_path / "missing.xml", typed=False)

    with pytest.raises(PyXSDError):
        schema.parse(ET.fromstring(VALID_XML), typed=False)

    stream = UnreadableStream()
    with pytest.raises(ValueError, match="resource_policy"):
        schema.parse(stream, typed=False, xml_limits=XmlLimits(max_elements=1))
    assert stream.reads == 0


def test_namespace_and_base_context_reach_the_lexical_tree(tmp_path):
    schema = compile_schema(
        f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc" ns="urn:example">
      <attribute name="xml:base"><text/></attribute>
      <empty/>
    </element>
  </start>
</grammar>"""
    )
    path = tmp_path / "context.xml"
    path.write_text(
        '<p:doc xmlns:p="urn:example" xml:base="https://example.test/base/"/>',
        encoding="utf-8",
    )

    document = schema.parse(path, typed=False)

    assert document.is_valid
    assert document.root._name_ == "{urn:example}doc"
    assert document.root.namespaces is not None
    assert document.root.namespaces["p"] == "urn:example"
    assert document.root.base_uri == "https://example.test/base/"


@pytest.mark.parametrize(("name", "value"), [("base", "parts/"), ("space", "preserve")])
def test_xml_context_attributes_participate_in_the_instance_grammar(name, value):
    schema = compile_schema(
        f'<element xmlns="{NS}" name="doc">'
        f'<attribute name="xml:{name}"><text/></attribute></element>'
    )
    present = schema.parse(io.StringIO(f'<doc xml:{name}="{value}"/>'), typed=False)
    assert present.is_valid
    assert list(present.report) == []


@pytest.mark.parametrize(("name", "value"), [("base", "parts/"), ("space", "preserve")])
def test_undeclared_xml_context_attributes_are_unexpected(name, value):
    unexpected = compile_schema().parse(io.StringIO(f'<doc xml:{name}="{value}"/>'), typed=False)
    assert not unexpected.is_valid
    [issue] = list(unexpected.report)
    assert issue.code == "unexpected-attribute"
    assert issue.location is not None
    assert issue.location.attribute == f"{{http://www.w3.org/XML/1998/namespace}}{name}"


def test_malformed_and_unreadable_inputs_raise_pyxserror(tmp_path):
    schema = compile_schema()

    with pytest.raises(PyXSDError, match="not well-formed"):
        schema.parse(io.StringIO("<doc>"), typed=False)

    missing = tmp_path / "missing.xml"
    with pytest.raises(PyXSDError, match="could not be read"):
        schema.parse(missing, typed=False)

    class FailingStream(UnreadableStream):
        def read(self, *args: object) -> str:
            self.reads += 1
            raise OSError("boom")

    with pytest.raises(PyXSDError, match="could not be read"):
        schema.parse(FailingStream(), typed=False)

    with pytest.raises(PyXSDError, match="could not be read"):
        schema.parse(FailingStream(), typed=False, capture_xdm=True)


def test_xml_limits_apply_to_sources_but_not_preparsed_elements():
    schema = compile_schema()
    limited = XmlLimits(max_elements=1)

    with pytest.raises(PyXSDError):
        schema.parse(io.StringIO("<doc><item>x</item></doc>"), typed=False, xml_limits=limited)

    document = schema.parse(
        ET.fromstring("<doc><item>x</item></doc>"),
        typed=False,
        xml_limits=limited,
    )
    assert document.is_valid


def test_xml_parser_policy_and_instance_limits_are_enforced():
    schema = compile_schema()

    with pytest.raises(PyXSDError):
        schema.parse(
            io.StringIO("<!DOCTYPE doc><doc/>"),
            typed=False,
            xml_policy=XmlParserPolicy.strict(),
        )

    with pytest.raises(RelaxNGLimitError):
        schema.parse(
            io.StringIO(VALID_XML),
            typed=False,
            limits=RelaxNGLimits(max_derivative_states=0),
        )


def test_capture_xdm_uses_the_same_parse_revision(tmp_path):
    schema = compile_schema()
    path = tmp_path / "captured.xml"
    path.write_text("<doc><item>x</item></doc>", encoding="utf-8")

    document = schema.parse(path, typed=False, capture_xdm=True)
    xdm = document.to_xdm()

    assert document.is_valid
    assert xdm.root_element.name == "doc"
    assert [child.name for child in xdm.root_element.children] == ["item"]

    from_element = schema.parse(
        ET.fromstring("<doc><item>x</item></doc>"),
        typed=False,
        capture_xdm=True,
    )
    assert from_element.to_xdm().root_element.name == "doc"

    with pytest.raises(PyXSDError):
        schema.parse(
            io.StringIO("<doc><item>x</item></doc>"),
            typed=False,
            capture_xdm=True,
            xml_limits=XmlLimits(max_bytes=4),
        )


def test_schema_report_errors_are_rejected_before_input_is_consumed():
    schema = RelaxNGSchema.compile(io.StringIO(BROKEN_SCHEMA))
    assert schema.report.has_errors
    stream = UnreadableStream()

    with pytest.raises(ValidationError) as excinfo:
        schema.parse(stream, typed=False)

    assert stream.reads == 0
    assert excinfo.value.report is schema.report


def test_valid_and_invalid_documents_return_generic_roots_and_reports():
    schema = compile_schema()

    valid = schema.parse(io.StringIO(VALID_XML), typed=False)
    assert valid.is_valid
    assert not valid.report.has_errors
    assert isinstance(valid.root, RelaxNGElement)
    assert not isinstance(valid.root, SchemaBase)
    assert valid.root._name_ == "doc"

    invalid = schema.parse(io.StringIO(INVALID_XML), typed=False)
    assert not invalid.is_valid
    assert invalid.report.has_errors
    assert isinstance(invalid.root, RelaxNGElement)
    assert not isinstance(invalid.root, SchemaBase)
    assert invalid.root._name_ == "other"
    issues = list(invalid.report)
    assert len(issues) == 1
    assert issues[0].phase == "instance"
    assert issues[0].code == UNEXPECTED_ELEMENT
    assert issues[0].location is not None
    assert issues[0].location.path == (PathStep("other", 1),)

    invalid_child = schema.parse(io.StringIO(INVALID_CHILD_XML), typed=False)
    assert not invalid_child.is_valid
    assert invalid_child.root._name_ == "doc"
    assert invalid_child.root.children[0]._name_ == "unexpected"
    assert any(
        issue.code == UNEXPECTED_ELEMENT and issue.phase == "instance"
        for issue in invalid_child.report
    )


def test_public_relaxng_exports_include_documents_and_elements():
    assert "RelaxNGDocument" in relaxng.__all__
    assert "RelaxNGElement" in relaxng.__all__
    assert "RelaxNGSchema" in relaxng.__all__
    assert relaxng.RelaxNGDocument is RelaxNGDocument
    assert relaxng.RelaxNGElement is RelaxNGElement
    assert relaxng.RelaxNGSchema is RelaxNGSchema


def test_typed_parsing_is_available_and_validation_only_stays_supported():
    schema = compile_schema()

    typed_document = schema.parse(io.StringIO(VALID_XML))
    assert typed_document.typed is True
    assert isinstance(typed_document.root, RelaxNGElement)
    assert typed_document.is_valid

    validation_document = schema.parse(io.StringIO(VALID_XML), typed=False)
    assert validation_document.typed is False
    assert validation_document.is_valid


def test_invalid_capture_options_are_rejected_before_input_is_consumed():
    schema = compile_schema()

    stream = UnreadableStream()
    with pytest.raises(ValueError, match="patterns"):
        schema.parse(stream, typed=False, capture="patterns")
    assert stream.reads == 0

    for capture in ("bogus", None, 3):
        stream = UnreadableStream()
        with pytest.raises(ValueError, match="capture"):
            schema.parse(stream, typed=False, capture=capture)  # type: ignore[arg-type]
        assert stream.reads == 0


def test_invalid_limits_are_rejected_before_input_is_consumed():
    schema = compile_schema()
    stream = UnreadableStream()

    with pytest.raises(TypeError, match="limits"):
        schema.parse(stream, typed=False, limits=object())  # type: ignore[arg-type]

    assert stream.reads == 0


def test_each_parse_has_an_isolated_report_after_frozen_schema_issues():
    schema = compile_schema()
    schema.report.add_warning("schema advisory", code="schema-advisory", phase="schema")
    schema_issues = tuple(schema.report.issues)
    schema_warning = schema_issues[0]

    first = schema.parse(io.StringIO(INVALID_XML), typed=False)
    second = schema.parse(io.StringIO(INVALID_XML), typed=False)

    assert first.report is not second.report
    assert tuple(schema.report.issues) == schema_issues
    for document in (first, second):
        issues = tuple(document.report.issues)
        assert issues[0] is schema_warning
        assert issues[0].severity is IssueSeverity.WARNING
        assert issues[0].phase == "schema"
        assert issues[1].phase == "instance"
        assert issues[1].code == UNEXPECTED_ELEMENT
        assert document.report.warning_count == 1
        assert document.report.error_count == 1
    assert first.report.issues[1] is not second.report.issues[1]

    valid = schema.parse(io.StringIO(VALID_XML), typed=False)
    assert valid.is_valid
    assert valid.report.issues[0] is schema_warning
    assert valid.report.warning_count == 1
    assert valid.report.error_count == 0


def test_validation_adds_no_defaults_and_retrieves_no_external_dtd(tmp_path):
    """RELAX NG adds no defaults and performs no DTD Compatibility processing.

    ElementTree's pre-existing application of internal-subset defaults is an
    input-infoset behavior, not RELAX NG synthesis; this regression neither
    depends on nor asserts against that parser behavior.
    """
    schema = compile_schema(REQUIRED_ATTRIBUTE_SCHEMA)

    plain = schema.parse(io.StringIO("<doc/>"), typed=False)
    assert plain.root.attribute_lexical("flag") is None
    assert {issue.code for issue in plain.report} == {MISSING_ATTRIBUTE}

    defaults = tmp_path / "defaults.dtd"
    defaults.write_text('<!ATTLIST doc flag CDATA "yes">', encoding="utf-8")
    path = tmp_path / "document.xml"
    path.write_text(f'<!DOCTYPE doc SYSTEM "{defaults.name}"><doc/>', encoding="utf-8")

    external = schema.parse(path, typed=False)

    assert not external.is_valid
    assert external.root.attribute_lexical("flag") is None
    assert {issue.code for issue in external.report} == {MISSING_ATTRIBUTE}


def test_writers_preserve_mixed_content_tails_without_xsd_writer_behavior(tmp_path):
    schema = compile_schema(MIXED_SCHEMA)
    document = schema.parse(io.StringIO("<doc>a<item>x</item>b</doc>"), typed=False)
    expected = "<doc>a<item>x</item>b</doc>"

    assert document.is_valid
    assert document.to_string() == expected

    path = tmp_path / "mixed.xml"
    document.write(path)
    assert path.read_text(encoding="utf-8") == expected

    stream = FlushRecorder()
    document.write(stream)
    assert stream.getvalue() == expected
    assert stream.flushes == 1
    assert not stream.closed


def test_serialization_preserves_xsi_attributes_and_untyped_xdm():
    schema = compile_schema(XSI_ATTRIBUTE_SCHEMA)
    xml = (
        '<doc xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:nil="true" xsi:type="xs:string" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema"><item/></doc>'
    )
    document = schema.parse(io.StringIO(xml), typed=False, capture_xdm=True)
    root = document.root
    attributes_before = dict(root.attributes)
    revision = root.revision

    serialized = document.to_string()

    assert document.is_valid
    assert 'xsi:nil="true"' in serialized
    assert 'xsi:type="xs:string"' in serialized
    assert dict(root.attributes) == attributes_before
    assert root.revision is revision
    assert not revision.dirty
    assert document.interpretations_current

    xdm = document.to_xdm()
    assert xdm.root_element.type_name == UNTYPED
    assert xdm.root_element.children[0].type_name == UNTYPED
    assert xdm.root_element.attributes
    assert {attribute.type_name for attribute in xdm.root_element.attributes} == {UNTYPED_ATOMIC}


def test_validation_only_documents_reject_annotations_and_revalidate_freshly():
    schema = compile_schema()
    document = schema.parse(io.StringIO("<doc><item>x</item></doc>"), typed=False)

    assert document.typed is False
    assert document.interpretations_current is True

    with pytest.raises(PyXSDError, match="pattern capture is unavailable"):
        _ = document.annotations

    document.root.set_text("changed")
    assert document.interpretations_current is False

    fresh = document.revalidate()
    assert fresh is not document
    assert fresh.interpretations_current is True
    assert fresh.root.text_value() == "changed"


def test_data_conversion_methods_fail_explicitly_without_xsd_dispatch(monkeypatch):
    schema = compile_schema()
    document = schema.parse(io.StringIO("<doc><item>x</item></doc>"), typed=False)
    attributes_before = dict(document.root.attributes)
    revision = document.root.revision
    report = document.report

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("an XSD conversion path was reached")

    monkeypatch.setattr(Document, "to_dict", fail)
    monkeypatch.setattr(Document, "to_data", fail)
    monkeypatch.setattr(Document, "to_json", fail)

    calls = (
        lambda: document.to_dict(),
        lambda: document.to_dict(typed=False),
        lambda: document.to_data(),
        lambda: document.to_data(format="jsonml", typed=False),
        lambda: document.to_json(),
        lambda: document.to_json(typed=False),
    )
    for call in calls:
        with pytest.raises(PyXSDError, match="Phase 5"):
            call()

    assert dict(document.root.attributes) == attributes_before
    assert document.root.revision is revision
    assert not revision.dirty
    assert document.report is report


def test_direct_document_construction_enforces_typed_evidence_invariants():
    schema = compile_schema()
    parsed = schema.parse(io.StringIO(VALID_XML), typed=False)
    common = {
        "schema": parsed.schema,
        "root": parsed.root,
        "source": parsed.source,
        "report": parsed.report,
    }

    with pytest.raises(PyXSDError, match="binding evidence"):
        RelaxNGDocument(**common, typed=True)

    with pytest.raises(TypeError, match="bool"):
        RelaxNGDocument(**common, typed=1)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="binding_evidence"):
        RelaxNGDocument(**common, typed=True, binding_evidence=object())  # type: ignore[arg-type]

    typed_parsed = schema.parse(io.StringIO(VALID_XML), typed=True)
    typed_common = {
        "schema": typed_parsed.schema,
        "root": typed_parsed.root,
        "source": typed_parsed.source,
        "report": typed_parsed.report,
    }
    rebuilt = RelaxNGDocument(
        **typed_common,
        typed=True,
        binding_evidence=typed_parsed.binding_evidence,
    )
    assert rebuilt.typed is True
    assert rebuilt.binding_evidence is typed_parsed.binding_evidence
    assert rebuilt.root.scalar_value() is None

    invalid = schema.parse(io.StringIO(INVALID_CHILD_XML), typed=True)
    assert not invalid.is_valid
    with pytest.raises(ValueError, match="invalid"):
        RelaxNGDocument(
            schema=invalid.schema,
            root=invalid.root,
            source=invalid.source,
            report=invalid.report,
            typed=True,
            binding_evidence=typed_parsed.binding_evidence,
        )

    with pytest.raises(ValueError, match="binding evidence"):
        RelaxNGDocument(
            **common,
            typed=False,
            binding_evidence=typed_parsed.binding_evidence,
        )

    for capture in ("patterns", "bogus", None):
        with pytest.raises(ValueError, match="capture"):
            RelaxNGDocument(**common, typed=False, capture=capture)
