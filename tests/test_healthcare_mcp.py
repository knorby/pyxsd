"""Exercise the diagnostic client offline with realistic tool envelopes."""

import asyncio
import base64
from types import SimpleNamespace

import pytest
from tests.healthcare_support import cda as cda


def envelope(data, *, page=None, artifacts=None, status="ok"):
    return SimpleNamespace(
        structured_content={
            "status": status,
            "data": data,
            "warnings": [],
            "provenance": {},
            "page": page,
            "artifacts": artifacts or [],
        }
    )


def issue(code="order", index=2):
    return {
        "code": code,
        "severity": "error",
        "message": "Wrong order",
        "phase": "instance",
        "element": "item",
        "location": {
            "source": "doc.xml",
            "path": [
                {"name": "{urn:test}root", "index": 1},
                {"name": "{urn:test}item", "index": index},
            ],
            "attribute": None,
        },
        "constraint": None,
        "details": None,
        "location_precision": "exact",
    }


class FakeSession:
    def __init__(self, *, fault=None, valid=False):
        self.calls = []
        self.fault, self.valid = fault, valid

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "pyxsd_capabilities":
            return envelope({"source_root": "/example", "execution_backend": "disabled"})
        if name == "pyxsd_manage_workspace":
            return envelope(
                {"id": "workspace"}
                if arguments["action"] == "create"
                else {"released": "workspace"}
            )
        if name == "pyxsd_validate_document":
            if self.fault:
                return envelope(
                    {
                        "fault": {
                            "code": self.fault,
                            "message": "fault",
                            "retryable": False,
                            "details": {},
                        }
                    },
                    status="error",
                )
            if self.valid:
                return envelope({"valid": True, "binding_available": True, "report": None})
            second = bool(arguments["page"].get("cursor"))
            return envelope(
                {
                    "valid": False,
                    "binding_available": True,
                    "report": {
                        "counts": {"error": 2, "warning": 0},
                        "issues": [
                            issue("unexpected-element" if second else "order", 2 if second else 1)
                        ],
                        "suppressed_count": 0,
                    },
                },
                page={
                    "returned": 1,
                    "next_cursor": None if second else "next",
                    "truncated": not second,
                    "total": 2,
                },
                artifacts=[{"id": "report2" if second else "report1", "kind": "report"}],
            )
        if name == "pyxsd_query_document":
            assert arguments["xpath"] == "/n0:root[1]/n0:item[2]"
            assert arguments["namespaces"] == {"n0": "urn:test"}
            return envelope(
                {
                    "results": [
                        {
                            "name": "{urn:test}item",
                            "encoding": "base64",
                            "source_faithful": True,
                            "data": base64.b64encode(b'<item id="2"/>').decode(),
                            "fault": None,
                        }
                    ]
                }
            )
        if name == "pyxsd_explain_issue":
            assert arguments["report"]["id"] == "report2" and arguments["issue_index"] == 0
            return envelope(
                {
                    "code": "unexpected-element",
                    "summary": "Preserve unfamiliar content",
                    "actions": [],
                    "known": True,
                }
            )
        raise AssertionError(name)


def test_pages_exact_occurrence_and_page_local_report_reference():
    from examples.healthcare.bad_data_doctor.doctor import collect_diagnostics

    session = FakeSession()
    report = asyncio.run(
        collect_diagnostics(session, schema_path="main.xsd", document_path="doc.xml", issue_index=1)
    )
    assert report.error_count == 2
    assert [i.code for i in report.issues] == ["order", "unexpected-element"]
    assert report.occurrence_xml == '<item id="2"/>'
    assert "Preserve unfamiliar content" in report.explanation
    assert session.calls[-1] == (
        "pyxsd_manage_workspace",
        {"action": "release", "workspace_id": "workspace"},
    )


@pytest.mark.parametrize("fault", ["invalid_xml", "schema_incomplete"])
def test_faults_are_not_invalid_document_verdicts(fault):
    from examples.healthcare.bad_data_doctor.doctor import DoctorError, collect_diagnostics

    session = FakeSession(fault=fault)
    with pytest.raises(DoctorError, match=fault):
        asyncio.run(collect_diagnostics(session, schema_path="main.xsd", document_path="doc.xml"))
    assert session.calls[-1][1]["action"] == "release"


def test_valid_document_needs_no_report_or_query():
    from examples.healthcare.bad_data_doctor.doctor import collect_diagnostics

    session = FakeSession(valid=True)
    report = asyncio.run(
        collect_diagnostics(session, schema_path="main.xsd", document_path="doc.xml")
    )
    assert report.error_count == 0 and report.issues == ()
    assert report.selected_issue is None and report.occurrence_xml is None
    assert not any("query" in name or "explain" in name for name, _ in session.calls)


def test_real_stdio_doctor_preserves_source_and_inspects_occurrence(cda):
    pytest.importorskip("mcp")
    from examples.healthcare.bad_data_doctor.doctor import run
    from examples.healthcare.common.sources import document_path

    source = document_path("ccd-with-issues")
    before = source.read_bytes()
    report = asyncio.run(run("documents/hl7-ccd-with-issues.xml", None))
    assert report.error_count > 0
    assert {"order", "unexpected-element"} <= {i.code for i in report.issues}
    assert report.occurrence_xml and "raceCode" in report.occurrence_xml
    assert report.explanation
    assert source.read_bytes() == before
