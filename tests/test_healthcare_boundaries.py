"""Always-on mapping guards and script entrypoints; no external schema downloads."""

import json
import runpy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from examples.healthcare.cda_to_fhir import mapping
from examples.healthcare.common.cda_datatypes import (
    CodingValue,
    IdentifierValue,
    datetime_value,
)
from examples.healthcare.common.conversion_report import ConversionReport


def node(name, *kids, **attributes):
    return SimpleNamespace(_name_="{urn:hl7-org:v3}" + name, _children_=list(kids), **attributes)


def report():
    return ConversionReport("test", {"error": 0, "warning": 0})


def coded(name="code", **changes):
    value = replace(CodingValue("http://loinc.org", "123", None, None, (), True, None), **changes)
    return node(name, as_coding=lambda: value)


def test_mapping_requires_actual_elements_and_time_precision():
    with pytest.raises(mapping.ConversionError, match="Missing required CDA"):
        mapping.required(node("root"), "id")
    with pytest.raises(mapping.ConversionError, match="Missing required timestamp"):
        mapping.timestamp(None, report(), "time")
    for raw in [None, "2020", "2020010112", "2020010112+0000"]:
        with pytest.raises(mapping.ConversionError, match="Insufficient"):
            mapping.timestamp(
                node("time", as_datetime=lambda raw=raw: datetime_value(raw)),
                report(),
                "time",
                instant=True,
            )
    assert (
        mapping.timestamp(
            node("time", as_datetime=lambda: datetime_value("2020")), report(), "time"
        )
        == "2020"
    )
    assert (
        mapping.timestamp(
            node("time", as_datetime=lambda: datetime_value("20200101120000+0000")),
            report(),
            "time",
            instant=True,
        )
        == "2020-01-01T12:00:00+00:00"
    )
    for raw in ["2020.1", "20200101+0000"]:
        with pytest.raises(ValueError, match="Unsupported"):
            datetime_value(raw)


def test_missing_or_translated_coding_is_explicit():
    for value in [None, node("value"), coded(null_flavor="UNK"), coded(code=None)]:
        result = report()
        assert mapping.supported_coding(value, result, "entry[2]") is None
        assert result.unsupported_entries[0]["path"] == "entry[2]"
    result = report()
    translation = coded().as_coding()
    assert (
        mapping.supported_coding(coded(translations=(translation,)), result, "entry[1]") is not None
    )
    assert result.notes == [{"path": "entry[1]", "reason": "coding translations not mapped"}]


def test_missing_or_ambiguous_problem_shapes_are_not_conditions():
    for entry in [
        node("entry"),
        node("entry", node("act", node("entryRelationship"), node("entryRelationship"))),
    ]:
        result = report()
        assert mapping.map_problem(None, entry, "entry[1]", result) == []
        assert result.unsupported_entries


def test_unusable_vitals_are_not_measurements():
    status = node("statusCode", code="completed")
    for code, components, reason in [
        (None, [], "coding datatype"),
        (coded(), [], None),
        (coded(), [node("component", node("observation"))], "coding datatype"),
        (coded(), [node("component", node("observation", coded()))], "quantity datatype"),
        (
            coded(),
            [node("component", node("observation", coded(), node("value")))],
            "quantity datatype",
        ),
    ]:
        organizer = node(
            "organizer", status, *([code] if code else []), *components, moodCode="EVN"
        )
        result = report()
        assert mapping.map_vitals(None, node("entry", organizer), "entry[1]", result) == []
        if reason:
            assert any(reason in entry["reason"] for entry in result.unsupported_entries)
        else:
            assert result.unsupported_entries == []


def test_patient_without_usable_identity_is_rejected():
    for ids in [
        [],
        [node("id", as_identifier=lambda: IdentifierValue(None, None, None, None, None, "UNK"))],
    ]:
        result = report()
        with pytest.raises(mapping.ConversionError, match="usable identifier"):
            mapping.patient_resource(None, node("patientRole", *ids, node("patient")), result)


def test_empty_mapping_does_not_create_section_references():
    root = node(
        "ClinicalDocument",
        node(
            "component",
            node(
                "structuredBody",
                node(
                    "component",
                    node(
                        "section",
                        node("code", code="11450-4", codeSystem="2.16.840.1.113883.6.1"),
                        node("entry"),
                    ),
                ),
            ),
        ),
    )
    result = report()
    assert mapping.map_sections(None, SimpleNamespace(root=root), result) == ([], [])
    assert result.unsupported_entries


def test_downloader_script_entrypoint_uses_requested_flags(monkeypatch, capsys, tmp_path):
    from examples.healthcare.common import sources

    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return [tmp_path]

    monkeypatch.setattr(sources, "fetch_sources", fetch)
    monkeypatch.setattr(
        "sys.argv", ["download_sources.py", "--include-onc", "--include-vendor", "--force"]
    )
    with pytest.raises(SystemExit) as exc:
        runpy.run_path("examples/healthcare/download_sources.py", run_name="__main__")
    assert exc.value.code == 0
    assert calls == [{"include_onc": True, "include_vendor": True, "force": True}]
    assert str(tmp_path) in capsys.readouterr().out


def test_doctor_main_reports_success_and_dependency_or_tool_fault(monkeypatch, capsys):
    from examples.healthcare.bad_data_doctor import doctor

    async def success(*args):
        return doctor.DoctorReport(0, 0, (), None, None, None)

    monkeypatch.setattr(doctor, "run", success)
    assert doctor.main([]) == 0
    assert json.loads(capsys.readouterr().out)["issues"] == []

    async def missing(*args):
        raise ImportError("mcp")

    monkeypatch.setattr(doctor, "run", missing)
    assert doctor.main([]) == 2
    assert "--extra mcp" in capsys.readouterr().err

    async def fault(*args):
        raise doctor.DoctorError("invalid_xml")

    monkeypatch.setattr(doctor, "run", fault)
    assert doctor.main([]) == 1
    assert "invalid_xml" in capsys.readouterr().err


@pytest.mark.parametrize(
    "script",
    ["datatypes/inspect_document.py", "cda_to_fhir/convert.py", "bad_data_doctor/doctor.py"],
)
def test_direct_script_help_does_not_need_downloads(script, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [script, "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path("examples/healthcare/" + script, run_name="__main__")
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_downloader_imported_entrypoint_and_default_flags(monkeypatch, capsys):
    from examples.healthcare import download_sources

    monkeypatch.setattr(download_sources, "fetch_sources", lambda **kwargs: [])
    assert download_sources.main([]) == 0
    assert capsys.readouterr().out == ""


def test_inspection_leaves_missing_and_unusable_values_absent():
    from examples.healthcare.common.cda_datatypes import QuantityValue
    from examples.healthcare.datatypes.inspect_document import inspect

    sections = [
        node(
            "section",
            node("code", code="11450-4"),
            node(
                "entry", node("act", node("entryRelationship", node("observation", node("value"))))
            ),
        ),
        node(
            "section",
            node("code", code="8716-3"),
            node(
                "entry",
                node(
                    "organizer",
                    node("component", node("observation", node("value"))),
                    node(
                        "component",
                        node(
                            "observation",
                            node("value", as_quantity=lambda: QuantityValue(None, None, "UNK")),
                        ),
                    ),
                ),
            ),
        ),
    ]
    view = inspect(SimpleNamespace(root=node("ClinicalDocument"), findall=lambda path: sections))
    assert view["patient"] == {"identifiers": [], "birth_date": None, "gender": None}
    assert view["conditions"] == []
    assert view["vital_signs"] == [
        {"code": None, "value": None, "unit": None, "null_flavor": "UNK"}
    ]
