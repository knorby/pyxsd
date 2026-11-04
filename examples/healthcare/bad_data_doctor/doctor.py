"""Run the MCP diagnosis loop, without AI credentials or code execution."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.healthcare.common.sources import cda_schema_path, resolve_document


class ToolSession(Protocol):
    async def call_tool(self, name: str, arguments: dict): ...


class DoctorError(ValueError):
    """A tool fault, not a schema-invalid document verdict."""


@dataclass(frozen=True)
class DoctorIssue:
    code: str
    severity: str
    message: str
    path: str | None


@dataclass(frozen=True)
class DoctorReport:
    error_count: int
    warning_count: int
    issues: tuple[DoctorIssue, ...]
    selected_issue: DoctorIssue | None
    occurrence_xml: str | None
    explanation: str | None
    occurrence_base64: str | None = None
    occurrence_note: str | None = None


def occurrence_query(location: dict) -> tuple[str, dict[str, str]]:
    namespaces, segments = {}, []
    for step in location["path"]:
        name = step["name"]
        if name.startswith("{"):
            uri, local = name[1:].split("}", 1)
            prefix = next((p for p, ns in namespaces.items() if ns == uri), None)
            if prefix is None:
                prefix = f"n{len(namespaces)}"
                namespaces[prefix] = uri
            name = f"{prefix}:{local}"
        segments.append(f"{name}[{step['index']}]")
    # Attribute findings are inspected at the containing element occurrence.
    return "/" + "/".join(segments), namespaces


async def call(session, name, arguments) -> dict:
    result = await session.call_tool(name, arguments)
    envelope = result.structured_content
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict):
        raise DoctorError("Missing structured tool envelope")
    if any(artifact["kind"] == "result" for artifact in envelope["artifacts"]):
        raise DoctorError("Response spilled to an artifact; inspect it with pyxsd_read_artifact")
    if envelope["status"] == "error":
        fault = envelope["data"]["fault"]
        raise DoctorError(f"{fault['code']}: {fault['message']}")
    return envelope


async def collect_diagnostics(
    session: ToolSession, *, schema_path: str, document_path: str, issue_index: int | None = None
) -> DoctorReport:
    capabilities = await call(session, "pyxsd_capabilities", {})
    if not capabilities["data"].get("source_root"):
        raise DoctorError(
            "This automated client requires --source-root; use the uploaded-workspace walkthrough for other servers"
        )
    created = await call(session, "pyxsd_manage_workspace", {"action": "create"})
    workspace = created["data"]["id"]
    arguments = {
        "workspace_id": workspace,
        "schema_ref": {
            "source": {"path": schema_path},
            "options": {"binding": "lax", "namespaces": "strict", "xsd_version": "1.0"},
        },
        "document": {"path": document_path},
    }
    try:
        issues, evidence = [], []
        cursor, counts = None, {"error": 0, "warning": 0}
        while True:
            response = await call(
                session,
                "pyxsd_validate_document",
                {**arguments, "page": {"cursor": cursor, "limit": 2}},
            )
            report = response["data"].get("report")
            if report is not None:
                counts = report["counts"]
                if report.get("suppressed_count"):
                    raise DoctorError(
                        "Issue details were suppressed; use a smaller input or larger server budget"
                    )
                reference = next((a for a in response["artifacts"] if a["kind"] == "report"), None)
                for local_index, item in enumerate(report["issues"]):
                    path = occurrence_query(item["location"])[0] if item.get("location") else None
                    issues.append(
                        DoctorIssue(item["code"], item["severity"], item["message"], path)
                    )
                    evidence.append((item, reference, local_index))
            cursor = (response.get("page") or {}).get("next_cursor")
            if cursor is None:
                break
        if not issues:
            return DoctorReport(counts["error"], counts["warning"], (), None, None, None)
        if issue_index is None:
            issue_index = next(
                (i for i, (item, _, _) in enumerate(evidence) if item.get("location")), 0
            )
        if not 0 <= issue_index < len(issues):
            raise DoctorError("Issue index is out of range")
        item, reference, local_index = evidence[issue_index]
        occurrence = occurrence_base64 = occurrence_note = None
        if item.get("location"):
            xpath, namespaces = occurrence_query(item["location"])
            response = await call(
                session,
                "pyxsd_query_document",
                {**arguments, "xpath": xpath, "namespaces": namespaces, "result": "subtree"},
            )
            matches = response["data"]["results"]
            if matches and matches[0].get("source_faithful"):
                occurrence_base64 = matches[0]["data"]
                raw = base64.b64decode(occurrence_base64, validate=True)
                try:
                    occurrence = raw.decode("utf-8")
                except UnicodeDecodeError:
                    occurrence = None
                # UTF-16 slices often lack their document's BOM. Do not mistake
                # their NUL-interleaved ASCII bytes for a usable UTF-8 text view.
                if occurrence is None or "\x00" in occurrence:
                    occurrence = None
                    occurrence_note = (
                        "Original bytes retained in occurrence_base64; decode using the "
                        "document's original encoding, not an inferred UTF-8 view."
                    )
        explain_args = {"code": item["code"]}
        if reference is not None:
            explain_args.update(report=reference, issue_index=local_index)
        response = await call(session, "pyxsd_explain_issue", explain_args)
        explanation = json.dumps(response["data"], sort_keys=True, indent=2)
        return DoctorReport(
            counts["error"],
            counts["warning"],
            tuple(issues),
            issues[issue_index],
            occurrence,
            explanation,
            occurrence_base64,
            occurrence_note,
        )
    finally:
        await call(
            session, "pyxsd_manage_workspace", {"action": "release", "workspace_id": workspace}
        )


async def run(document_path: str, issue_index: int | None) -> DoctorReport:
    # MCP SDK 2.x Client manages initialization and subprocess shutdown.
    from mcp import Client, StdioServerParameters

    document = resolve_document(document_path)
    schema = cda_schema_path().resolve()
    source_root = Path(os.path.commonpath((document.parent, schema.parent)))
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pyxsd.mcp", "--source-root", str(source_root), "--no-persist"],
    )
    async with Client(server) as session:
        return await collect_diagnostics(
            session,
            schema_path=schema.relative_to(source_root).as_posix(),
            document_path=document.relative_to(source_root).as_posix(),
            issue_index=issue_index,
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--document",
        default="ccd-with-issues",
        help="local XML path, named example, or repo:relative/path",
    )
    parser.add_argument(
        "--issue", type=int, default=None, help="zero-based index; default is first located finding"
    )
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run(args.document, args.issue))
    except ImportError:
        print(
            "Run with uv run --extra mcp python examples/healthcare/bad_data_doctor/doctor.py",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
