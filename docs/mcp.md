# MCP integration

pyxsd can run as a local [Model Context Protocol](https://modelcontextprotocol.io/)
server. MCP clients can create private workspaces, explore compiled schemas,
validate XML, follow diagnostics to exact source occurrences, run bounded XPath
queries, and download immutable artifacts through one consistent tool surface.

The current transport is **stdio**. Execution of user-supplied Python and remote
HTTP transport are separate milestones; execution is disabled in this build.
See {doc}`mcp-deployment` for the worker runtime and OCI containment status.

## Install and launch

Install the optional dependencies with Python 3.11 or newer:

```bash
pip install 'pyxsd[mcp]'
```

Choose a private, persistent state directory and launch:

```bash
pyxsd-mcp --state-dir "$HOME/.local/state/pyxsd/mcp"
```

`python -m pyxsd.mcp` is equivalent. A generic stdio client configuration looks
like this (use an absolute, owner-only state path):

```json
{
  "mcpServers": {
    "pyxsd": {
      "command": "pyxsd-mcp",
      "args": ["--state-dir", "/absolute/path/to/private/state"]
    }
  }
}
```

The state directory contains workspaces, immutable revisions, artifacts, and
restart metadata. It is never selected by a tool argument. Back up or delete it
using the same ownership policy you would use for other application state.

For an operator-granted read-only source tree, add `--source-root /absolute/root`.
Capture resolves only that configured root for the local `stdio` principal; a
client cannot mount an arbitrary directory. Normal workspace uploads use
`pyxsd_put_files` and need no source mount.

## End-to-end client example

The official Python SDK works directly with the stdio transport:

```python
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    parameters = StdioServerParameters(
        command="pyxsd-mcp",
        args=["--state-dir", "/absolute/path/to/private/state"],
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            created = await client.call_tool(
                "pyxsd_manage_workspace",
                {"action": "create", "idempotency_key": "example-1"},
            )
            workspace = created.structured_content["data"]["id"]

            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {
                            "path": "order.xsd",
                            "text": '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                            '<xs:element name="order" type="xs:int"/></xs:schema>',
                        },
                        {"path": "order.xml", "text": "<order>42</order>"},
                    ],
                    "expected_revision": None,
                },
            )
            result = await client.call_tool(
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "order.xml"},
                    "schema_ref": {"source": {"path": "order.xsd"}},
                },
            )
            assert result.structured_content["data"]["valid"] is True


anyio.run(main)
```

Every ordinary tool returns the same `ToolEnvelope` in `structuredContent` and
as JSON text. `result.is_error` distinguishes an operational failure from a
successful call. XML validation findings are different: an invalid document has
`status="ok"` and `data.valid=false`; malformed or unreadable XML has
`is_error=true` and a typed fault.

## Tool catalog

| Tool | What it does |
| --- | --- |
| `pyxsd_capabilities` | Reports version, enabled groups, execution policy, limits and adapter status without host paths or secrets. |
| `pyxsd_manage_workspace` | Creates, lists, or explicitly releases a private workspace. Creation accepts an idempotency key. |
| `pyxsd_put_files` | Publishes a bounded UTF-8 bundle as a new immutable revision, guarded by the expected previous revision. |
| `pyxsd_inspect_schema` | Returns an overview, compact/full stubs, or constraint-rich component descriptions with bounded depth. |
| `pyxsd_find_schema_components` | Searches literal text, kind, name and namespace filters, or traverses `uses` / `used_by` relationships. |
| `pyxsd_validate_document` | Returns verdicts, exact issue locations, aggregate counts, paged issues and source/schema fingerprints. |
| `pyxsd_query_document` | Runs XPath over the bound projection and returns scalars, node summaries, exact locations or bounded subtrees. |
| `pyxsd_explain_issue` | Gives curated issue-code guidance, explicit unknowns, and optional exact issue lookup from a report artifact. |
| `pyxsd_read_artifact` | Returns a bounded byte range as base64 with digest and media type. |

Schema and document references use workspace-relative POSIX paths:

```json
{
  "document": {"path": "order.xml", "revision": null},
  "schema_ref": {
    "source": {"path": "order.xsd", "revision": null},
    "options": {
      "binding": "lax",
      "namespaces": "strict",
      "xsd_version": "1.1",
      "namespace_schemas": {}
    }
  }
}
```

Pin `revision` for repeatable reads. New revisions never overwrite old bytes in
place, and an active workspace lease prevents idle expiry during an operation.

## Diagnostics and repeated occurrences

`ValidationIssue.location` is a structured path with expanded names and one-based
same-name sibling indexes. It identifies the exact occurrence, not merely the
first element with a matching local name. Use `pyxsd_query_document` with
`result="subtree"` to retrieve that occurrence's XML and location together.

Reports distinguish:

- **aggregate counts** (`error_count` / `warning_count` in the report's `counts`);
- **collection suppression**, when an operator's issue budget omits details;
- **pagination**, which pages only the retained details without changing counts.

Page cursors authenticate the owner, revision, query and position. Reusing one
for another query or revision fails. Large scalar and subtree results are kept
intact; the coordinator stores oversized envelopes once as JSON artifacts and
returns a summary plus the artifact reference instead of truncated JSON.
Artifact ranges default to 8 KiB so base64 data and the JSON text/structured
representations fit together in the response budget.

## Resources and prompts

Resources include `pyxsd://issue-codes`, `pyxsd://capabilities`, and
`pyxsd://artifacts/{artifact_id}`. Artifact reads recheck ownership and return
the immutable bytes. The three prompts are `pyxsd_diagnose_xml`,
`pyxsd_author_pipeline`, and `pyxsd_evaluate_migration`. They provide optional
workflow guidance; no prompt or skill is required for tool correctness.

## Worker and resource model

The coordinator authorizes workspaces and budgets before a clean worker parses
or evaluates XML. Related operations run serially on one cached `Schema`;
separate workers provide isolation and parallelism. IPC uses bounded,
role-validated JSON—never pickle or live `Document` objects. Workers have
absolute deadlines, queue bounds, operation recycling, death recovery and
coordinated shutdown.

Important starting limits (all operator-configurable in `Settings.limits`):

| Limit | Default |
| --- | --- |
| XML / individual XSD file | 64 MiB / 16 MiB |
| Inline upload / MCP request | 1 MiB / 2 MiB |
| Result payload / page items | 24 KiB / 50 |
| Collected issues / message | 10,000 / 4 KiB |
| Output artifact | 128 MiB |
| Clean cache entries / workers | 8 / 4 |
| Worker operations before recycling | 100 |
| Compile or query deadline | 30 seconds |
| Idle workspace TTL | 60 minutes |

The local subprocess runtime is not a sandbox for untrusted Python. See
{doc}`mcp-deployment` for pinned OCI images, read-only mounts, no-network
configuration, forced cleanup, and the separately required Linux containment
tests.

## Restart, errors, and current boundaries

Successful revisions and artifacts survive coordinator restart. Interrupted
staging is discarded rather than treated as published data. Active operations
have deadlines and cancellation cleanup; durable background-job resumption is a
separate execution milestone, not something the current stdio server claims.

- **Missing optional dependency:** reinstall with `pip install 'pyxsd[mcp]'`;
  the CLI reports this without a traceback.
- **`not_found` versus `stale_reference`:** an unknown resource is absent;
  a pinned revision or cursor no longer matches current state.
- **Invalid versus malformed XML:** inspect the report for validation findings;
  malformed input is an operational `invalid_xml` fault.
- **Worker timeout or queue overflow:** retry only after reviewing document size,
  complexity and operator budgets. Neither produces a partial validation verdict.
- **Execution:** disabled. Do not send transforms for execution to this build.
- **HTTP/OAuth:** not registered by the stdio entry point. See the deployment
  guide for the remote security requirements rather than exposing stdio blindly.
