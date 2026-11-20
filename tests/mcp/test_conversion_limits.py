"""Adversarial import budgets and finite pagination under suppressed diagnostics."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")

import anyio
from tests.mcp.test_data_conversion import SEQUENCE_XSD, xsd_ref
from tests.mcp.test_exploration import harness, upload
from tests.mcp.test_relaxng_conversion import GENERIC, TAGGED, relaxng_ref
from tests.mcp.test_relaxng_validation import capture_of
from tests.test_import_resource_limits import RNG, XSD

from pyxsd import __version__
from pyxsd.mcp.cache import SchemaCache
from pyxsd.mcp.captures import InputPolicyError
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import ExportDataRequest, FileRef, ImportDataRequest, Page
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.services.conversion import ConversionService, _import_document, execute_import_data
from pyxsd.mcp.worker_protocol import ImportDataOperation
from pyxsd.mcp.workers import capture_to_wire
from pyxsd.relaxng.datatypes import DatatypeRegistrationError, UnsupportedDatatypeError


@pytest.mark.parametrize(
    "schema_ref,source", [(xsd_ref(), XSD), (relaxng_ref(), GENERIC)], ids=["xsd", "rng"]
)
@pytest.mark.parametrize("budget", ["elements", "depth"])
def test_import_and_export_obey_the_same_xml_structure_budgets(
    tmp_path, schema_ref, source, budget
):
    if budget == "elements":
        limits = Limits(enforced=True, xml_elements=8)
        data = ["root", *[["child", "x"] for _ in range(10)]]
        xml = "<root>" + "<child>x</child>" * 10 + "</root>"
    else:
        limits = Limits(enforced=True, xml_depth=12)
        data = ["root"]
        for _ in range(12):
            data = ["root", data]
        xml = "<root>" * 13 + "</root>" * 13
        if schema_ref.format == "relax-ng":
            source = RNG
    # The RNG grammar uses doc as its global root.
    if schema_ref.format == "relax-ng":
        data[0] = "doc"
        xml = xml.replace("<root>", "<doc>", 1)
        xml = xml.rsplit("</root>", 1)[0] + "</doc>"
    root = data[0]

    async def scenario():
        async with harness(tmp_path, limits) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {schema_ref.source.path: source, "document.xml": xml})
            service = ConversionService(inputs, pool)
            with pytest.raises(ToolError) as imported:
                await service.import_data(
                    ImportDataRequest(
                        workspace_id=workspace, schema_ref=schema_ref, data=data, format="jsonml"
                    )
                )
            assert imported.value.fault.code == "limit_exceeded"
            assert imported.value.fault.details["code"] == "xml_limit"
            with pytest.raises(ToolError) as exported:
                await service.export_data(
                    ExportDataRequest(
                        workspace_id=workspace,
                        schema_ref=schema_ref,
                        document=FileRef(path="document.xml"),
                    )
                )
            assert exported.value.fault.code == "limit_exceeded"
            assert exported.value.fault.details["code"] == "xml_limit"
            valid = await service.import_data(
                ImportDataRequest(
                    workspace_id=workspace,
                    schema_ref=schema_ref,
                    data=[root, ["child", "x"]],
                    format="jsonml",
                )
            )
            assert valid.data["valid"] is True

    anyio.run(scenario)


def test_relaxng_import_evidence_budget_is_an_operational_fault(tmp_path):
    async def scenario():
        limits = Limits(enforced=True, relaxng_evidence_states=1)
        async with harness(tmp_path, limits) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": TAGGED})
            with pytest.raises(ToolError) as raised:
                await ConversionService(inputs, pool).import_data(
                    ImportDataRequest(
                        workspace_id=workspace,
                        schema_ref=relaxng_ref(),
                        data=["doc", "12"],
                        format="jsonml",
                    )
                )
            fault = raised.value.fault
            assert fault.code == "limit_exceeded"
            assert fault.details["stage"] == "evidence"
            assert fault.details["budget"] == "max_evidence_states"
            assert fault.details["configured"] == 1
            assert fault.details["observed"] > 1

    anyio.run(scenario)


def test_suppressed_import_diagnostics_do_not_issue_unusable_cursors(tmp_path):
    async def scenario():
        limits = Limits(enforced=True, collected_issues=1, page_items=1)
        async with harness(tmp_path, limits) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.xsd": SEQUENCE_XSD})
            service = ConversionService(inputs, pool)
            request = ImportDataRequest(
                workspace_id=workspace,
                schema_ref=xsd_ref(),
                data=dict.fromkeys("abcde", "oops"),
                format="pyxsd",
            )
            first = await service.import_data(request)
            assert first.data["valid"] is False
            assert first.data["report"]["counts"]["error"] == 5
            assert "diagnostics_complete" not in first.data["report"]
            assert first.data["report"]["suppressed_count"] == 4
            assert len(first.data["report"]["issues"]) == 1
            assert first.page.returned == 1
            assert first.page.total == 5
            assert first.page.truncated is False
            assert first.page.next_cursor is None
            # Replay the offset-one cursor the buggy first page used to issue.
            result_id = service._result_id(request, await inputs.manifest(workspace))
            legacy_cursor = service.tokens.encode_page(result_id, 1)
            for _ in range(3):
                second = await service.import_data(
                    request.model_copy(update={"page": Page(cursor=legacy_cursor)})
                )
                assert second.data["report"]["issues"] == []
                assert second.data["report"]["suppressed_count"] == 4
                assert second.page.returned == 0
                assert second.page.total == 5
                assert second.page.truncated is False
                assert second.page.next_cursor is None

    anyio.run(scenario)


def test_repeated_empty_import_pages_preserve_totals_without_continuation(tmp_path):
    capture = capture_of(tmp_path / "worker", {"schema.xsd": SEQUENCE_XSD})
    cache = SchemaCache(Limits(enforced=True, collected_issues=1, page_items=1))
    operation = ImportDataOperation(
        request_id="empty-page",
        capture=capture_to_wire(capture),
        schema_ref=xsd_ref(),
        data=dict.fromkeys("abcde", "oops"),
        format="pyxsd",
        issue_offset=1,
        issue_limit=1,
    )
    try:
        for _ in range(3):
            result = execute_import_data(operation, cache, __version__)
            assert result["report"]["issues"] == []
            assert result["report"]["counts"]["error"] == 5
            assert result["report"]["suppressed_count"] == 4
            assert result["page"] == {"returned": 0, "truncated": False, "total": 5}
    finally:
        cache.close()


@pytest.mark.parametrize(
    "error,code,details",
    [
        (UnsupportedDatatypeError("unavailable datatype"), "unsupported_capability", {}),
        (DatatypeRegistrationError("unavailable registration"), "unsupported_capability", {}),
        (InputPolicyError("xml_limit", "too large"), "limit_exceeded", {"code": "xml_limit"}),
    ],
)
def test_import_operational_exceptions_are_not_conversion_faults(tmp_path, error, code, details):
    class InterruptedSchema:
        def from_data(self, *args, **kwargs):
            raise error

    operation = ImportDataOperation(
        request_id="interrupted",
        capture=capture_to_wire(capture_of(tmp_path / "worker", {})),
        schema_ref=relaxng_ref(),
        data=["doc"],
        format="jsonml",
    )
    cache = SchemaCache(Limits(enforced=True))
    try:
        with pytest.raises(ToolError) as raised:
            _import_document(InterruptedSchema(), operation, cache)
        assert raised.value.fault.code == code
        assert raised.value.fault.details == details
    finally:
        cache.close()
