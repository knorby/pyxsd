"""Schema diagnostics report measured namespace-mode evidence, not guesses."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, schema_ref, upload

from pyxsd.exceptions import PyXSDError
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import (
    CompileOptions,
    ExplainIssueRequest,
    FileRef,
    InspectSchemaRequest,
    SchemaRef,
)
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.worker_protocol import CaptureFileMessage, CaptureMessage, DescribeOperation
from pyxsd.validation import ValidationReport

LEGACY_HEAD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
  xmlns:t="urn:test" targetNamespace="urn:test">
  <xs:element name="child" type="xs:string" substitutionGroup="t:missing"/>
</xs:schema>"""


def _operation(schema: SchemaRef) -> DescribeOperation:
    return DescribeOperation(
        request_id="diagnostics",
        capture=CaptureMessage(
            root="/unused",
            files=(CaptureFileMessage(path="schema.xsd", digest="0" * 64, size=1),),
        ),
        schema_ref=schema,
        view="diagnostics",
    )


def _head_report(codes: tuple[str, ...], *, prefixed: bool = True) -> ValidationReport:
    report = ValidationReport()
    for code in codes:
        report.add_error(
            "diagnostic issue",
            code=code,
            phase="schema",
            details=(
                {"substitutionGroup": "t:missing", "prefixed": prefixed}
                if code == "unknown-substitution-head"
                else None
            ),
        )
    return report


class _FakeCache:
    def __init__(
        self,
        current: ValidationReport,
        alternative: ValidationReport,
        failure: BaseException | None = None,
    ) -> None:
        self.current = SimpleNamespace(report=current)
        self.alternative = SimpleNamespace(report=alternative)
        self.failure = failure
        self.limits = Limits(message_bytes=32)
        self.requests = []
        self.active = 0
        self.max_active = 0

    @contextmanager
    def acquire(self, request) -> Iterator[SimpleNamespace]:
        self.requests.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if len(self.requests) == 1:
                yield self.current
            else:
                if self.failure is not None:
                    raise self.failure
                yield self.alternative
        finally:
            self.active -= 1


def _legacy_ref() -> SchemaRef:
    return SchemaRef(
        source=FileRef(path="schema.xsd"),
        options=CompileOptions(
            binding="strict",
            namespaces="legacy",
            xsd_version="1.0",
            namespace_schemas={"urn:extra": FileRef(path="extra.xsd")},
        ),
    )


def test_diagnostics_view_returns_original_schema_report(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": LEGACY_HEAD})
            envelope = await service.inspect_schema(
                InspectSchemaRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(
                        source=FileRef(path="schema.xsd"),
                        options=CompileOptions(namespaces="strict"),
                    ),
                    view="diagnostics",
                )
            )
            assert envelope.data["view"] == "diagnostics"
            assert envelope.data["report"]["counts"]["error"] == 1
            assert envelope.data["report"]["issues"][0]["code"] == "unknown-substitution-head"
            assert envelope.data["namespace_probe"] is None
            assert envelope.data["recommendations"] == []

    anyio.run(scenario)


def test_diagnostics_view_rejects_unknown_view(tmp_path):
    from pydantic import ValidationError

    async def scenario():
        async with harness(tmp_path) as (service, _inputs, _pool, workspace):
            with pytest.raises(ValidationError, match="literal_error"):
                await service.inspect_schema(
                    InspectSchemaRequest(
                        workspace_id=workspace,
                        schema_ref=schema_ref(),
                        view="diagnostic",
                    )
                )

    anyio.run(scenario)


def test_namespace_probe_no_recommendation_when_modes_agree(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": LEGACY_HEAD})
            envelope = await service.inspect_schema(
                InspectSchemaRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(
                        source=FileRef(path="schema.xsd"),
                        options=CompileOptions(namespaces="legacy"),
                    ),
                    view="diagnostics",
                )
            )
            probe = envelope.data["namespace_probe"]
            assert probe["status"] == "ok"
            assert probe["current"]["codes"] == {"unknown-substitution-head": 1}
            assert probe["alternative"]["codes"] == {"unknown-substitution-head": 1}
            assert probe["introduced_codes"] == []
            assert envelope.data["recommendations"] == []

    anyio.run(scenario)


def test_namespace_probe_recommends_only_measured_improvement():
    from pyxsd.mcp.worker_main import _describe

    cache = _FakeCache(_head_report(("unknown-substitution-head",)), ValidationReport())
    data = _describe(_operation(_legacy_ref()), cache, "test-processor")

    recommendation = data["recommendations"][0]
    assert recommendation["schema_ref"]["options"]["namespaces"] == "strict"
    assert recommendation["schema_ref"]["options"]["binding"] == "strict"
    assert recommendation["schema_ref"]["options"]["xsd_version"] == "1.0"
    assert recommendation["schema_ref"]["options"]["namespace_schemas"] == {
        "urn:extra": {"path": "extra.xsd"}
    }
    probe = data["namespace_probe"]
    assert probe["status"] == "ok"
    assert probe["current"]["codes"] == {"unknown-substitution-head": 1}
    assert probe["alternative"]["codes"] == {}
    assert probe["introduced_codes"] == []


def test_namespace_probe_releases_original_lease_and_reports_new_codes():
    from pyxsd.mcp.worker_main import _describe

    current = _head_report(("unknown-substitution-head", "unknown-substitution-head"))
    alternative = _head_report(("alternative-invalid",), prefixed=False)
    cache = _FakeCache(current, alternative)
    data = _describe(_operation(_legacy_ref()), cache, "test-processor")

    assert [request.schema_ref.options.namespaces for request in cache.requests] == [
        "legacy",
        "strict",
    ]
    assert cache.max_active == 1
    probe = data["namespace_probe"]
    assert probe["introduced_codes"] == ["alternative-invalid"]
    assert data["recommendations"] == []


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ToolError("invalid_schema", "alternate failed"), "invalid_schema"),
        (PyXSDError("alternate failed"), "invalid_schema"),
    ],
)
def test_namespace_probe_failure_preserves_original_report(failure, code):
    from pyxsd.mcp.worker_main import _describe

    cache = _FakeCache(_head_report(("unknown-substitution-head",)), ValidationReport(), failure)
    data = _describe(_operation(_legacy_ref()), cache, "test-processor")

    assert data["report"]["counts"]["error"] == 1
    probe = data["namespace_probe"]
    assert probe["status"] == "error"
    assert probe["fault"]["code"] == code
    assert probe["fault"]["message"] == "alternate failed"
    assert data["recommendations"] == []


def test_explain_unknown_substitution_head_points_to_diagnostics(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, _inputs, _pool, _workspace):
            envelope = await service.explain_issue(
                ExplainIssueRequest(code="unknown-substitution-head")
            )
            assert envelope.data["known"] is True
            assert any(
                "pyxsd_inspect_schema" in action and "diagnostics" in action
                for action in envelope.data["actions"]
            )
            assert "strict" in " ".join(envelope.data["actions"]).lower()

    anyio.run(scenario)
