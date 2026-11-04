"""Clinical context must survive, or be explicitly excluded from the scoped map."""

import xml.etree.ElementTree as ET
from io import StringIO
from unittest.mock import patch

import pytest
from tests.healthcare_support import cda as cda
from tests.healthcare_support import document_path
from tests.healthcare_support import fhir as fhir

C = {"c": "urn:hl7-org:v3"}
F = {"f": "http://hl7.org/fhir"}


def source():
    return ET.fromstring(document_path("discharge").read_bytes())


def section(root, code):
    return next(
        s for s in root.findall(".//c:section", C) if s.find("c:code", C).get("code") == code
    )


def convert(cda, fhir, root):
    from examples.healthcare.cda_to_fhir.convert import convert_document

    # Preserve the default namespace used by unprefixed xsi:type values (CD/PQ).
    with patch.dict(ET._namespace_map, {C["c"]: ""}):
        xml = ET.tostring(root, encoding="unicode")
    document = cda.parse(StringIO(xml))
    assert document.report.error_count == 0, document.report.to_dict()
    return convert_document(document, fhir=fhir)


@pytest.mark.parametrize(
    "target,attribute,value",
    [
        (".", "nullFlavor", "UNK"),
        ("c:entryRelationship/c:observation", "nullFlavor", "UNK"),
        (".", "classCode", "CONS"),
        ("c:entryRelationship", "negationInd", "true"),
        ("c:entryRelationship", "contextConductionInd", "false"),
    ],
)
def test_problem_context_is_not_a_positive_patient_assertion(cda, fhir, target, attribute, value):
    root = source()
    for act in section(root, "11450-4").findall("c:entry/c:act", C):
        act.find(target, C).set(attribute, value)
    result = convert(cda, fhir, root)
    assert result.report.resources.get("Condition", 0) == 0
    assert len(result.report.unsupported_entries) == 2


@pytest.mark.parametrize(
    "code,target,expected_conditions,expected_observations",
    [
        ("11450-4", "c:entry/c:act", 1, 5),
        ("11450-4", "c:entry/c:act/c:entryRelationship/c:observation", 1, 5),
        ("8716-3", "c:entry/c:organizer", 2, 0),
        ("8716-3", "c:entry/c:organizer/c:component/c:observation", 2, 4),
    ],
)
def test_explicit_subject_is_not_replaced_by_record_target(
    cda, fhir, code, target, expected_conditions, expected_observations
):
    root = source()
    node = section(root, code).find(target, C)
    # The observation's value/interpretation precede subject; participation and
    # relationships follow it. Insert at that declared boundary for each type.
    following = {
        "specimen",
        "performer",
        "author",
        "informant",
        "participant",
        "entryRelationship",
        "reference",
        "precondition",
        "referenceRange",
        "component",
    }
    position = next(
        (i for i, n in enumerate(node) if n.tag.rsplit("}", 1)[-1] in following), len(node)
    )
    subject = ET.fromstring('<subject xmlns="urn:hl7-org:v3"><relatedSubject/></subject>')
    node.insert(position, subject)
    result = convert(cda, fhir, root)
    assert result.report.resources.get("Condition", 0) == expected_conditions
    assert result.report.resources.get("Observation", 0) == expected_observations
    assert result.report.unsupported_entries


@pytest.mark.parametrize(
    "target", ["c:entry/c:organizer", "c:entry/c:organizer/c:component/c:observation"]
)
def test_null_vital_context_does_not_emit_a_measurement(cda, fhir, target):
    root = source()
    section(root, "8716-3").find(target, C).set("nullFlavor", "UNK")
    result = convert(cda, fhir, root)
    expected = 0 if target.endswith("organizer") else 4
    assert result.report.resources.get("Observation", 0) == expected
    assert result.report.unsupported_entries


def test_unknown_gender_does_not_emit_the_retained_code(cda, fhir):
    root = source()
    root.find("c:recordTarget/c:patientRole/c:patient/c:administrativeGenderCode", C).set(
        "nullFlavor", "UNK"
    )
    result = convert(cda, fhir, root)
    assert ET.fromstring(result.bundle.to_string()).find(".//f:Patient/f:gender", F) is None
    assert any("UNK" in note["reason"] for note in result.report.notes)


def test_proposed_vital_panel_is_not_final(cda, fhir):
    root = source()
    section(root, "8716-3").find("c:entry/c:organizer", C).set("moodCode", "PRP")
    result = convert(cda, fhir, root)
    assert result.report.resources.get("Observation", 0) == 0
    assert result.report.unsupported_entries


@pytest.mark.parametrize("code", ["11450-4", "8716-3"])
def test_multiple_observation_values_are_not_silently_truncated(cda, fhir, code):
    root = source()
    observation = section(root, code).find(".//c:observation", C)
    first = observation.find("c:value", C)
    second = ET.fromstring(ET.tostring(first))
    second.set("value" if code == "8716-3" else "code", "178" if code == "8716-3" else "74400008")
    observation.insert(list(observation).index(first) + 1, second)
    result = convert(cda, fhir, root)
    assert result.report.resources.get("Condition", 0) == (1 if code == "11450-4" else 2)
    assert result.report.resources.get("Observation", 0) == (4 if code == "8716-3" else 5)
    assert any("value" in item["reason"] for item in result.report.unsupported_entries)


def test_local_document_root_is_rejected_instead_of_an_empty_system(cda, fhir):
    from examples.healthcare.cda_to_fhir.convert import ConversionError

    root = source()
    root.find("c:id", C).set("root", "local-document-id")
    with pytest.raises(ConversionError, match="document identifier"):
        convert(cda, fhir, root)
