"""Compiled stylesheets exposed as pyxsd document transforms.

``CompiledStylesheet.as_transform`` wires one stylesheet to the
runtime-checkable ``DocumentTransform`` protocol, so
``Document.transform`` dispatches it before the legacy root callables.
The adapter runs the stylesheet's ordinary public invocation with
explicit document delivery over the source document's coherent captured
XDM, rejects ancillary results unless a callback receives the complete
result, and binds the principal result directly to the target schema
with ``parse(..., capture_xdm=True)`` -- no serialization or reparse
round trip -- and only a validating result becomes a document.

The tests are corpus-independent: a ``note`` source schema, a namespaced
``summary`` target schema, and stylesheets that map between them.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from tests.xslt.support import stylesheet

from pyxsd.document import Document
from pyxsd.document_transform import AncillaryResultError, DocumentTransform
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.relaxng import RelaxNGDocument, RelaxNGSchema
from pyxsd.schema import Schema
from pyxsd.xdm.errors import XdmInputError
from pyxsd.xslt.adapter import XsltDocumentTransform
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.processor import CompiledStylesheet, XsltProcessor
from pyxsd.xslt.results import TransformationResult
from pyxsd.xslt.serialization import Serializer

SOURCE_SCHEMA = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="note" type="Note"/>
  <xs:complexType name="Note">
    <xs:sequence>
      <xs:element name="to" type="xs:string"/>
      <xs:element name="count" type="xs:integer"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>
"""

SOURCE_XML = "<note><to>you</to><count>3</count></note>"

TARGET_SCHEMA = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:summary" xmlns:s="urn:summary" elementFormDefault="qualified">
  <xs:element name="summary">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="line" type="xs:string" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="total" type="xs:integer" use="required"/>
      <xs:attribute name="kind" type="xs:QName"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

RELAXNG_SOURCE_SCHEMA = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="note">
      <element name="to"><text/></element>
      <element name="count"><text/></element>
    </element>
  </start>
</grammar>
"""

SUMMARY_XML = (
    '<s:summary xmlns:s="urn:summary" total="3" kind="s:regular"><s:line>you</s:line></s:summary>'
)

MAPPER_BODY = (
    '<xsl:template match="/">'
    '<s:summary total="{/*/count}" kind="s:regular">'
    '<s:line><xsl:value-of select="/*/to"/></s:line>'
    "</s:summary>"
    "</xsl:template>"
)

SECONDARY_BODY = (
    '<xsl:template match="/">'
    '<xsl:result-document href="http://example.com/audit">'
    '<xsl:copy-of select="/*"/>'
    "</xsl:result-document>"
    '<s:summary total="1">'
    "<s:line>done</s:line>"
    "</s:summary>"
    "</xsl:template>"
)

MESSAGE_BODY = (
    '<xsl:template match="/">'
    "<xsl:message>mapped</xsl:message>"
    '<s:summary total="1">'
    "<s:line>done</s:line>"
    "</s:summary>"
    "</xsl:template>"
)

ANNOUNCING_BODY = (
    '<xsl:template match="/">'
    "<xsl:message>started</xsl:message>"
    '<xsl:result-document href="http://example.com/audit">'
    "<audit/>"
    "</xsl:result-document>"
    '<s:summary total="1">'
    "<s:line>done</s:line>"
    "</s:summary>"
    "</xsl:template>"
)

_MAPPER_OPTIONS: dict[str, Any] = {"namespaces": {"s": "urn:summary"}, "declare_xs": False}


def _schema(source: str) -> Schema:
    schema = Schema.compile(io.StringIO(source))
    assert not schema.report.has_errors, schema.report
    return schema


def _source_document(xml: str = SOURCE_XML) -> Document:
    document = _schema(SOURCE_SCHEMA).parse(io.StringIO(xml), capture_xdm=True)
    assert document.is_valid, document.report
    return document


def _relaxng_source_document(xml: str = SOURCE_XML) -> RelaxNGDocument:
    schema = RelaxNGSchema.compile(io.StringIO(RELAXNG_SOURCE_SCHEMA))
    assert not schema.report.has_errors, schema.report
    document = schema.parse(io.StringIO(xml), typed=False, capture_xdm=True)
    assert document.is_valid, document.report
    return document


def _target_schema() -> Schema:
    return _schema(TARGET_SCHEMA)


def _compiled(body: str, **options: Any) -> CompiledStylesheet:
    return XsltProcessor().compile(io.BytesIO(stylesheet(body, **options)))


def _mapper_sheet() -> CompiledStylesheet:
    return _compiled(MAPPER_BODY, **_MAPPER_OPTIONS)


def _expected_root(target_schema: Schema) -> Any:
    """The bound root an independent parse of the same target document produces."""
    expected = target_schema.parse(io.StringIO(SUMMARY_XML))
    assert expected.is_valid, expected.report
    return expected.root


def test_document_adapter_fresh_target():
    source_document = _source_document()
    target_schema = _target_schema()
    expected = _expected_root(target_schema)
    sheet_adapter = _mapper_sheet().as_transform(target_schema=target_schema)
    assert isinstance(sheet_adapter, DocumentTransform)

    converted = source_document.transform(sheet_adapter)

    assert converted.schema is target_schema
    assert converted.report is not source_document.report
    assert converted.root._descriptor_ is expected._descriptor_
    assert converted.to_xdm().root_element.annotation.actual_type is type(expected)
    assert converted.is_valid
    assert converted.root._attribs_["total"] == expected._attribs_["total"]
    assert converted.root._attribs_["kind"] == expected._attribs_["kind"]
    assert converted._provenance is not source_document._provenance


def test_document_adapter_accepts_relaxng_source_documents():
    source_document = _relaxng_source_document()
    target_schema = _target_schema()
    adapter = _mapper_sheet().as_transform(target_schema=target_schema)

    converted = source_document.transform(adapter)

    assert converted is not source_document
    assert converted.schema is target_schema
    assert converted.is_valid
    assert converted.to_xdm().root_element.name == "{urn:summary}summary"
    assert source_document.interpretations_current is True


def test_document_adapter_rejects_stale_or_ancillary_results():
    source_document = _source_document()
    target_schema = _target_schema()
    adapter = _mapper_sheet().as_transform(target_schema=target_schema)
    secondary_sheet = _compiled(SECONDARY_BODY, **_MAPPER_OPTIONS)
    stale_document = _source_document()
    stale_document.root._children_.pop()

    with pytest.raises(XdmInputError):
        stale_document.transform(adapter)
    with pytest.raises(AncillaryResultError):
        source_document.transform(secondary_sheet.as_transform(target_schema=target_schema))
    handled = source_document.transform(
        secondary_sheet.as_transform(target_schema=target_schema, on_result=lambda result: None)
    )
    assert handled.is_valid


def test_uncaptured_source_document_is_rejected():
    document = _schema(SOURCE_SCHEMA).parse(io.StringIO(SOURCE_XML))
    adapter = _mapper_sheet().as_transform(target_schema=_target_schema())

    with pytest.raises(XdmInputError, match="capture_xdm=True"):
        document.transform(adapter)


def test_direct_transform_document_call_matches_dispatch():
    adapter = _mapper_sheet().as_transform(target_schema=_target_schema())
    document = _source_document()

    direct = adapter.transform_document(document)
    dispatched = document.transform(adapter)

    assert direct.to_string() == dispatched.to_string()
    assert direct.root._descriptor_ is dispatched.root._descriptor_


def test_transform_document_never_serializes(monkeypatch: pytest.MonkeyPatch):
    def refuse(*args: Any, **kwargs: Any) -> str:
        del args, kwargs
        raise AssertionError("the adapter serialized the transformation result")

    monkeypatch.setattr(Serializer, "serialize", refuse)

    converted = _source_document().transform(
        _mapper_sheet().as_transform(target_schema=_target_schema())
    )

    assert converted.is_valid


def test_legacy_root_callable_path_unchanged():
    document = _source_document()

    same = document.transform(lambda root: root)
    assert isinstance(same, Document)
    assert same.report is document.report

    passed_through = document.transform(lambda root: "raw")
    assert passed_through == "raw"

    with_arguments = document.transform(lambda root, extra: extra, "arg")
    assert with_arguments == "arg"

    with_kwargs = document.transform(lambda root, flag=False: flag, flag=True)
    assert with_kwargs is True


def test_document_transform_dispatch_precedes_legacy_callables():
    class Dual:
        """Callable that also implements the protocol: only the protocol may run."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, root: Any, *args: Any, **kwargs: Any) -> Any:
            self.calls.append("root-callable")
            return root

        def transform_document(self, document: Document) -> Document:
            self.calls.append("transform-document")
            return document

    document = _source_document()
    dual = Dual()

    dispatched = document.transform(dual)

    assert dual.calls == ["transform-document"]
    assert dispatched is document


def test_document_transform_rejects_extra_arguments():
    adapter = _mapper_sheet().as_transform(target_schema=_target_schema())

    with pytest.raises(TypeError, match="DocumentTransform"):
        _source_document().transform(adapter, "extra")


def test_as_transform_validates_its_arguments():
    sheet = _mapper_sheet()

    with pytest.raises(TypeError, match="target_schema"):
        sheet.as_transform(target_schema="not-a-schema")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="on_result"):
        sheet.as_transform(target_schema=_target_schema(), on_result="nope")  # type: ignore[arg-type]


def test_adapter_constructor_validates_its_wiring():
    sheet = _mapper_sheet()
    target_schema = _target_schema()

    with pytest.raises(TypeError, match="stylesheet"):
        XsltDocumentTransform(  # type: ignore[arg-type]
            "sheet", extensions=sheet.extensions, target_schema=target_schema
        )
    with pytest.raises(TypeError, match="extensions"):
        XsltDocumentTransform(  # type: ignore[arg-type]
            sheet, extensions="snapshot", target_schema=target_schema
        )


def test_raw_non_document_result_is_rejected():
    sheet = _compiled(
        '<xsl:template match="/"><xsl:sequence select="map{\'k\':1}"/></xsl:template>'
    )
    adapter = sheet.as_transform(target_schema=_target_schema())

    with pytest.raises(XsltDynamicError) as excinfo:
        _source_document().transform(adapter)
    assert excinfo.value.code == "XTDE0450"


def test_rootless_principal_result_is_rejected():
    sheet = _compiled('<xsl:template match="/"/>')
    adapter = sheet.as_transform(target_schema=_target_schema())

    with pytest.raises(PyXSDError, match="no document element"):
        _source_document().transform(adapter)


def test_target_validation_failure_raises_with_the_fresh_report():
    sheet = _compiled('<xsl:template match="/"><s:unrelated/></xsl:template>', **_MAPPER_OPTIONS)
    adapter = sheet.as_transform(target_schema=_target_schema())
    source_document = _source_document()

    with pytest.raises(ValidationError) as excinfo:
        source_document.transform(adapter)

    assert excinfo.value.report.has_errors
    assert excinfo.value.report is not source_document.report


def test_message_producing_transform_requires_a_callback():
    sheet = _compiled(MESSAGE_BODY, **_MAPPER_OPTIONS)
    adapter = sheet.as_transform(target_schema=_target_schema())

    with pytest.raises(AncillaryResultError, match="message"):
        _source_document().transform(adapter)


def test_callback_receives_the_complete_result_and_grants_no_writes():
    observed: list[TransformationResult] = []

    def on_result(result: TransformationResult) -> str:
        observed.append(result)
        return "a callback return value must not redirect the pipeline"

    sheet = _compiled(ANNOUNCING_BODY, **_MAPPER_OPTIONS)
    adapter = sheet.as_transform(target_schema=_target_schema(), on_result=on_result)

    converted = _source_document().transform(adapter)

    assert converted.is_valid
    [result] = observed
    assert isinstance(result, TransformationResult)
    assert len(result.secondary) == 1
    assert [message.text for message in result.messages] == ["started"]


def test_callback_is_not_invoked_without_ancillary_results():
    observed: list[TransformationResult] = []
    adapter = _mapper_sheet().as_transform(
        target_schema=_target_schema(), on_result=observed.append
    )

    converted = _source_document().transform(adapter)

    assert observed == []
    assert converted.is_valid


def test_failing_callback_aborts_the_transform():
    def on_result(result: TransformationResult) -> None:
        del result
        raise RuntimeError("callback refused")

    sheet = _compiled(MESSAGE_BODY, **_MAPPER_OPTIONS)
    adapter = sheet.as_transform(target_schema=_target_schema(), on_result=on_result)

    with pytest.raises(RuntimeError, match="refused"):
        _source_document().transform(adapter)


def test_shared_schema_overlap_keeps_converted_documents_independent():
    target_schema = _target_schema()
    source_schema = _schema(SOURCE_SCHEMA)
    adapter = _mapper_sheet().as_transform(target_schema=target_schema)
    first = source_schema.parse(io.StringIO(SOURCE_XML), capture_xdm=True)
    second = source_schema.parse(
        io.StringIO("<note><to>me</to><count>9</count></note>"), capture_xdm=True
    )

    converted_first = first.transform(adapter)
    converted_second = second.transform(adapter)
    again = second.transform(_mapper_sheet().as_transform(target_schema=target_schema))

    assert converted_first.is_valid
    assert converted_second.is_valid
    assert converted_first.report is not converted_second.report
    assert converted_second.root._attribs_["total"] == again.root._attribs_["total"]
    assert converted_first.to_xdm().root_element.name == "{urn:summary}summary"
    assert converted_second.to_xdm().root_element.annotation.actual_type is type(
        converted_second.root
    )
