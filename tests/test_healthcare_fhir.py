"""Real R4 output and explicit failure/unsupported-content boundaries."""

import json
from io import StringIO
from xml.etree import ElementTree as ET

import pytest
from examples.healthcare.common.sources import document_path
from tests.healthcare_support import cda as cda
from tests.healthcare_support import fhir as fhir

from pyxsd import XmlParserPolicy

NS = {"f": "http://hl7.org/fhir"}


def parse(cda, xml=None):
    return cda.parse(
        StringIO(xml) if xml is not None else document_path("discharge"),
        xml_policy=XmlParserPolicy.protected(),
    )


def test_discharge_resources_and_values(cda, fhir):
    from examples.healthcare.cda_to_fhir.convert import convert_document

    result = convert_document(parse(cda), fhir=fhir)
    assert result.bundle.report.error_count == 0
    assert result.report.resources == {
        "Composition": 1,
        "Patient": 1,
        "Condition": 2,
        "Observation": 5,
    }
    assert result.report.unsupported_section_count == 19
    root = ET.fromstring(result.bundle.to_string())
    assert root.find("f:entry/f:resource/*", NS).tag == "{http://hl7.org/fhir}Composition"

    def values(path):
        return [n.get("value") for n in root.findall(path, NS)]

    assert values(".//f:Patient/f:birthDate") == ["2005-05-01"]
    assert values(".//f:Patient/f:gender") == ["female"]
    assert values(".//f:Condition/f:code/f:coding/f:display") == ["Dark stools", "Appendicitis"]
    assert values(".//f:Observation/f:valueQuantity/f:value") == ["177", "86", "80", "55"]
    assert values(".//f:Observation/f:status") == ["final"] * 5
    assert (
        values(".//f:Observation/f:hasMember/f:reference")
        and len(values(".//f:Observation/f:hasMember/f:reference")) == 4
    )
    assert values("f:timestamp") == ["2014-09-17T19:04:00-05:00"]
    assert values(".//f:Composition/f:type/f:coding/f:code") == ["18842-5"]
    assert values(".//f:Composition/f:title") == [
        "Community Health and Hospitals: Discharge Summary"
    ]


def test_duplicate_source_ids_do_not_collide_and_output_is_deterministic(cda, fhir):
    from examples.healthcare.cda_to_fhir.convert import convert_document

    first = convert_document(parse(cda), fhir=fhir)
    second = convert_document(parse(cda), fhir=fhir)
    assert first.bundle.to_string() == second.bundle.to_string()
    assert first.report.to_dict() == second.report.to_dict()
    root = ET.fromstring(first.bundle.to_string())
    urls = [n.get("value") for n in root.findall("f:entry/f:fullUrl", NS)]
    assert len(urls) == len(set(urls)) == 9
    assert all(n.get("value") in urls for n in root.findall(".//f:reference", NS))


def test_null_and_unknown_code_system_are_reported(cda, fhir):
    from examples.healthcare.cda_to_fhir.convert import convert_document

    xml = (
        document_path("discharge")
        .read_text()
        .replace('value="177" unit="cm"', 'nullFlavor="UNK"')
        .replace(
            'code="35064005" codeSystem="2.16.840.1.113883.6.96"',
            'code="35064005" codeSystem="1.2.3"',
        )
    )
    result = convert_document(parse(cda, xml), fhir=fhir)
    report = json.dumps(result.report.to_dict())
    assert "UNK" in report and "urn:oid:1.2.3" in report
    assert result.report.resources["Condition"] == 1
    assert result.report.resources["Observation"] == 4
    assert 'value="177"' not in result.bundle.to_string()


def test_source_findings_require_classification(cda, fhir):
    from examples.healthcare.cda_to_fhir.convert import (
        ConversionError,
        convert_document,
        source_issue_key,
    )

    xml = (
        document_path("discharge")
        .read_text()
        .replace("</ClinicalDocument>", '<extra xmlns="urn:example:extension"/></ClinicalDocument>')
    )
    document = parse(cda, xml)
    assert document.report.error_count > 0
    with pytest.raises(ConversionError, match="Unclassified"):
        convert_document(document, fhir=fhir)
    result = convert_document(
        document,
        fhir=fhir,
        allowed_source_issues=[source_issue_key(i) for i in document.report.errors],
    )
    assert result.report.source_findings
    assert result.bundle.report.error_count == 0


@pytest.mark.parametrize(
    "xml",
    ["<broken>", '<unknown xmlns="urn:hl7-org:v3"/>', '<ClinicalDocument xmlns="urn:hl7-org:v3"/>'],
)
def test_unusable_input_writes_no_bundle(cda, fhir, tmp_path, monkeypatch, xml):
    from examples.healthcare.cda_to_fhir import convert

    source = tmp_path / "source.xml"
    source.write_text(xml)
    output = tmp_path / "output"
    monkeypatch.setattr(convert, "compile_cda", lambda: cda)
    monkeypatch.setattr(convert, "compile_fhir", lambda: fhir)
    assert convert.main([str(source), "--output-dir", str(output)]) == 1
    assert not output.exists()


def test_multiple_inputs_reuse_compiled_schemas(cda, fhir, tmp_path, monkeypatch):
    from examples.healthcare.cda_to_fhir import convert

    calls = []

    def compile_cda():
        calls.append("cda")
        return cda

    def compile_fhir():
        calls.append("fhir")
        return fhir

    monkeypatch.setattr(convert, "compile_cda", compile_cda)
    monkeypatch.setattr(convert, "compile_fhir", compile_fhir)
    second = tmp_path / "second.xml"
    second.write_bytes(document_path("discharge").read_bytes())
    output = tmp_path / "out"
    assert (
        convert.main([str(document_path("discharge")), str(second), "--output-dir", str(output)])
        == 0
    )
    assert calls == ["cda", "fhir"]
    assert len(list(output.glob("*.fhir.xml"))) == 2


def test_stable_identity_includes_full_occurrence():
    from examples.healthcare.cda_to_fhir.fhir_build import resource_id

    assert resource_id("a/b", "c", "entry[1]") != resource_id("a", "b/c", "entry[1]")
    assert resource_id("root", "ext", "entry[1]") != resource_id("root", "ext", "entry[2]")


def test_quantity_lexical_precision_survives_real_cda_binding(cda, fhir):
    from examples.healthcare.cda_to_fhir.convert import convert_document

    xml = (
        document_path("discharge")
        .read_text()
        .replace('value="177" unit="cm"', 'value="0.100000000000000001" unit="cm"')
    )
    result = convert_document(parse(cda, xml), fhir=fhir)
    root = ET.fromstring(result.bundle.to_string())
    assert (
        root.find(".//f:Observation/f:valueQuantity/f:value", NS).get("value")
        == "0.100000000000000001"
    )


def test_unfinished_panel_is_not_reported_final(cda, fhir):
    from examples.healthcare.cda_to_fhir.convert import convert_document

    xml = document_path("discharge").read_text()
    start = xml.index('<organizer classCode="CLUSTER"')
    xml = xml[:start] + xml[start:].replace(
        '<statusCode code="completed"/>', '<statusCode code="active"/>', 1
    )
    result = convert_document(parse(cda, xml), fhir=fhir)
    assert result.report.resources.get("Observation", 0) == 0
    assert any("status" in e["reason"] for e in result.report.unsupported_entries)


def test_failed_fresh_output_validation_is_fatal(cda, fhir, monkeypatch):
    from examples.healthcare.cda_to_fhir.convert import ConversionError, convert_document
    from examples.healthcare.cda_to_fhir.fhir_build import Builder

    invalid = fhir.parse(StringIO('<Bundle xmlns="http://hl7.org/fhir"/>'))
    monkeypatch.setattr(Builder, "validate", lambda self, bundle: invalid)
    with pytest.raises(ConversionError, match="fresh validation"):
        convert_document(parse(cda), fhir=fhir)
