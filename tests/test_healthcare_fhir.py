"""Real R4 output and explicit failure/unsupported-content boundaries."""

import json
from io import StringIO
from xml.etree import ElementTree as ET

import pytest
from tests.healthcare_support import cda as cda
from tests.healthcare_support import document_path
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
        .read_text(encoding="utf-8")
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
        .read_text(encoding="utf-8")
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
    source.write_text(xml, encoding="utf-8")
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
        .read_text(encoding="utf-8")
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

    xml = document_path("discharge").read_text(encoding="utf-8")
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


@pytest.mark.parametrize("missing", ["id-value", "target", "title", "author-id"])
def test_required_header_fields_cannot_be_invented(cda, fhir, missing):
    from examples.healthcare.cda_to_fhir.convert import ConversionError, convert_document
    from examples.healthcare.common.cda_datatypes import child, children

    document = parse(cda)
    root = document.root
    if missing == "id-value":
        child(root, "id").nullFlavor = "NI"
    elif missing == "target":
        root._children_ = [n for n in root._children_ if n not in children(root, "recordTarget")]
    elif missing == "title":
        child(root, "title")._value_ = []
    else:
        child(child(child(root, "author"), "assignedAuthor"), "id").nullFlavor = "NI"
    with pytest.raises(ConversionError, match=r"identifier|recordTarget|title/code|author"):
        convert_document(document, fhir=fhir)


def test_optional_patient_values_are_not_fabricated(cda, fhir):
    from examples.healthcare.cda_to_fhir.fhir_build import Builder
    from examples.healthcare.cda_to_fhir.mapping import patient_resource
    from examples.healthcare.common.cda_datatypes import child, children
    from examples.healthcare.common.conversion_report import ConversionReport

    document = parse(cda)
    role = child(child(document.root, "recordTarget"), "patientRole")
    patient = child(role, "patient")
    children(role, "id")[1].nullFlavor = "UNK"
    child(patient, "administrativeGenderCode").code = "X"
    child(patient, "birthTime").nullFlavor = "UNK"
    name = child(patient, "name")
    name._children_ = [n for n in name._children_ if n not in children(name, "family")]
    for given in children(name, "given"):
        given._value_ = []
    result = ConversionReport("test", {})
    resource = patient_resource(Builder(fhir, "root", "ext"), role, result)
    assert len(result.notes) == 3
    assert all(
        n._name_.split("}")[-1] not in ("name", "gender", "birthDate") for n in resource._children_
    )
    patient._children_ = []
    result = ConversionReport("test", {})
    resource = patient_resource(Builder(fhir, "root", "ext"), role, result)
    assert len(result.notes) == 1


def test_duplicate_output_stems_are_rejected_before_work(tmp_path):
    from examples.healthcare.cda_to_fhir.convert import main

    with pytest.raises(SystemExit) as exc:
        main(["a/same.xml", "b/same.xml", "--output-dir", str(tmp_path)])
    assert exc.value.code == 2


@pytest.mark.parametrize("lexical", ["true", "false"])
def test_negated_findings_are_never_positive_conditions_or_measurements(cda, fhir, lexical):
    from examples.healthcare.cda_to_fhir.convert import convert_document

    xml = (
        document_path("discharge")
        .read_text(encoding="utf-8")
        .replace(
            'classCode="OBS" moodCode="EVN"',
            f'classCode="OBS" moodCode="EVN" negationInd="{lexical}"',
        )
    )
    document = parse(cda, xml)
    assert document.report.error_count == 0
    result = convert_document(document, fhir=fhir)
    assert result.report.resources.get("Condition", 0) == (0 if lexical == "true" else 2)
    assert result.report.resources.get("Observation", 0) == (0 if lexical == "true" else 5)
    assert len(result.report.unsupported_entries) == (6 if lexical == "true" else 0)


@pytest.mark.parametrize(
    "context", ["negated-act", "intent-act", "reversed-subject", "different-relationship"]
)
def test_problem_parent_context_must_not_change_the_finding(cda, fhir, context):
    from examples.healthcare.cda_to_fhir.convert import convert_document

    xml = document_path("discharge").read_text(encoding="utf-8")
    if context == "negated-act":
        xml = xml.replace(
            '<act classCode="ACT" moodCode="EVN">',
            '<act classCode="ACT" moodCode="EVN" negationInd="true">',
        )
    elif context == "intent-act":
        xml = xml.replace(
            '<act classCode="ACT" moodCode="EVN">', '<act classCode="ACT" moodCode="INT">'
        )
    elif context == "reversed-subject":
        xml = xml.replace(
            '<entryRelationship typeCode="SUBJ">',
            '<entryRelationship typeCode="SUBJ" inversionInd="true">',
        )
    else:
        xml = xml.replace(
            '<entryRelationship typeCode="SUBJ">', '<entryRelationship typeCode="REFR">'
        )
    document = parse(cda, xml)
    assert document.report.error_count == 0
    result = convert_document(document, fhir=fhir)
    assert result.report.resources.get("Condition", 0) == 0
    assert result.report.resources["Observation"] == 5
    assert len(result.report.unsupported_entries) == 2
