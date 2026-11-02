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


def test_oversized_envelope_is_not_mistaken_for_a_valid_document():
    from examples.healthcare.bad_data_doctor.doctor import DoctorError, call

    class Spilled:
        async def call_tool(self, *args):
            return envelope(
                {
                    "summary": "Full result is stored in the attached JSON artifact.",
                    "bytes": 999999,
                },
                artifacts=[{"kind": "result"}],
            )

    with pytest.raises(DoctorError, match="spilled"):
        asyncio.run(call(Spilled(), "pyxsd_validate_document", {}))


def test_absent_structured_data_and_source_root_are_explicit_faults():
    from examples.healthcare.bad_data_doctor.doctor import DoctorError, call, collect_diagnostics

    class Empty:
        async def call_tool(self, *args):
            return SimpleNamespace(structured_content=None)

    with pytest.raises(DoctorError, match="structured"):
        asyncio.run(call(Empty(), "anything", {}))

    class NoRoot:
        async def call_tool(self, *args):
            return envelope({"source_root": None})

    with pytest.raises(DoctorError, match="source-root"):
        asyncio.run(collect_diagnostics(NoRoot(), schema_path="s.xsd", document_path="d.xml"))


@pytest.mark.parametrize(
    "kind",
    ["suppressed", "no-location", "no-reference", "unavailable", "empty-matches", "out-of-range"],
)
def test_partial_diagnostics_do_not_invent_occurrences(kind):
    from examples.healthcare.bad_data_doctor.doctor import DoctorError, collect_diagnostics

    class Altered(FakeSession):
        async def call_tool(self, name, arguments):
            if name == "pyxsd_query_document":
                return envelope(
                    {
                        "results": []
                        if kind == "empty-matches"
                        else [{"source_faithful": False, "fault": {"code": "source_unavailable"}}]
                    }
                )
            if name == "pyxsd_explain_issue":
                return envelope({"summary": "Review it"})
            result = await super().call_tool(name, arguments)
            if name == "pyxsd_validate_document":
                body = result.structured_content
                body["page"]["next_cursor"] = None
                if kind == "suppressed":
                    body["data"]["report"]["suppressed_count"] = 1
                if kind == "no-location":
                    body["data"]["report"]["issues"][0]["location"] = None
                if kind == "no-reference":
                    body["artifacts"] = []
            return result

    session = Altered()
    if kind in ("suppressed", "out-of-range"):
        with pytest.raises(
            DoctorError, match="suppressed" if kind == "suppressed" else "out of range"
        ):
            asyncio.run(
                collect_diagnostics(
                    session,
                    schema_path="s.xsd",
                    document_path="d.xml",
                    issue_index=99 if kind == "out-of-range" else None,
                )
            )
    else:
        result = asyncio.run(
            collect_diagnostics(session, schema_path="s.xsd", document_path="d.xml")
        )
        assert result.selected_issue.code == "order"
        assert result.occurrence_xml is None
        assert "Review it" in result.explanation
    assert session.calls[-1][1]["action"] == "release"


def test_unqualified_and_multiple_namespace_paths():
    from examples.healthcare.bad_data_doctor.doctor import occurrence_query

    path, namespaces = occurrence_query(
        {
            "path": [
                {"name": "root", "index": 1},
                {"name": "{urn:a}one", "index": 2},
                {"name": "{urn:b}two", "index": 3},
            ]
        }
    )
    assert path == "/root[1]/n0:one[2]/n1:two[3]"
    assert namespaces == {"n0": "urn:a", "n1": "urn:b"}
