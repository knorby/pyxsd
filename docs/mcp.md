# MCP integration

pyxsd can run as a local [Model Context Protocol](https://modelcontextprotocol.io/)
server. MCP clients can create private workspaces, explore compiled schemas,
validate XML, follow diagnostics to exact source occurrences, run bounded XPath
queries, and download immutable artifacts through one consistent tool surface.

Both **stdio** and authenticated **streamable HTTP** are available. Execution is
disabled by default. Local transformation previews require an explicit stdio
opt-in and run with the launching user's privileges, not in a sandbox. See
{doc}`mcp-deployment` for the worker runtime and OCI containment status.

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

## Authenticated streamable HTTP

Start the HTTP transport with an explicit issuer, audience, and JWKS endpoint:

```bash
pyxsd-mcp \
  --transport streamable-http \
  --http-host 127.0.0.1 --http-port 8000 \
  --issuer-url https://issuer.example \
  --audience https://mcp.example \
  --jwks-url https://issuer.example/jwks.json \
  --allowed-host '127.0.0.1:*' \
  --allowed-origin https://client.example
```

The server verifies JWT signatures with the fixed issuer's keys, pinned accepted
algorithms, audience, expiry and a non-empty subject. There is no login provider,
request-selected issuer, or caller-selected tenant field. Workspace ownership
comes from the verified subject, and scopes come from the verified token:

- `pyxsd:read` — exploration, resources and artifact downloads
- `pyxsd:upload` — workspace creation and file publication
- `pyxsd:execute`, `pyxsd:write`, `pyxsd:replace`, `pyxsd:write-invalid` —
  required by the corresponding preview/publication operations

`/.well-known/oauth-protected-resource` advertises the resource metadata. Missing
or invalid credentials receive a Bearer challenge; missing scopes are distinct
from an unknown resource. Host and Origin allowlists apply to metadata, MCP and
binary routes. Forwarded host/identity headers are not trusted. Terminate TLS at
a reviewed reverse proxy and keep the issuer/JWKS origin fixed in deployment
configuration.

For local development only, `--loopback-dev` permits an unauthenticated server
bound to loopback. It is an explicit development switch, not a remote mode.

### Binary transfer

Authenticated routes preserve original bytes without JSON/base64 expansion:

```text
PUT /files/{workspace}/{path}
GET /artifacts/{artifact_id}?offset=0&length=8192
```

`PUT` accepts `application/octet-stream` and publishes an immutable revision.
Set `X-PyxSD-Expected-Revision` when updating an existing revision. Request bodies
are limited to 2 MiB. `GET` verifies the same owner and returns the exact binary
range, media type and `X-Content-SHA256` digest. No route mounts a host directory
or accepts an arbitrary output path.

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
| `pyxsd_preview_records` | Projects document rows through Arrow with a 20-row preview cap, exact/incomplete counts, occurrence selectors and contextual columns. Read-only; requires the optional Arrow dependency. |
| `pyxsd_export_records` | Durable job writing Parquet (single document) or a manifested dataset (multiple documents) as immutable digest-verified artifacts with provenance. Requires the execution tool group and Arrow. |
| `pyxsd_compare_schemas` | Durable job diffing two schema revisions (added/removed/changed components including anonymous declarations) and optionally validating a corpus under both, classifying each file as valid/regressed/resolved/invalid/malformed/unbindable/skipped. |
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

### Projected Pydantic contracts

`pyxsd_inspect_model` exposes projected JSON Schema for a selected declaration;
`pyxsd_validate_data` checks JSON data against it. They are available with the MCP
extra and can be disabled with `Settings.pydantic=False`. They do not establish
full XML validity or mutate XML. See {doc}`integrations` for aliases, exact values,
declaration paths and unsupported shapes.

## Projected records (optional Arrow)

With the optional `pyarrow` dependency installed, `pyxsd_preview_records` projects
bound document rows in a clean worker and returns at most 20 rows plus a total row
count. Counts are exact by default; a fast mode (`exact_count: false`) may report
`count_complete: false` when rows exceed the preview cap. Rows are projected, not
silently flattened: XML is fully materialized before batching, and the result
carries a warning saying so. Selection uses either the declaration path
(`element` plus `path`) or an ElementTree occurrence `selector` (for example
`./item[@id='24']`), and contextual columns map record, ancestor and root
`FieldSource` values.

`pyxsd_export_records` submits a durable job that writes a single-document Parquet
file or a multi-document manifested dataset inside a disposable worker output
directory. The coordinator — never the worker — captures the bounded regular
files, hashes them, and retains them as immutable artifacts with digests, sizes,
media types and a manifest of relative paths. Export refutes invalid documents
(`invalid_document`), enforces the document-count corpus quota and a total output
byte budget (`output_limit`), and rejects non-regular or linked output
(`unsafe_candidate`). Datasets never append to existing output. Download exports
with `pyxsd_read_artifact` and verify the digest locally; the result records the
source documents and the schema fingerprint for provenance. Both record tools are
registered only when Arrow is available, and `pyxsd:execute` is required to
export.

## Packaged skill discovery

The server declares the vendor extension `com.knorby.pyxsd/skills` through the
SDK's extension interface. Modern protocol discovery advertises its settings;
legacy `initialize` responses do not expose modern extension capabilities.
`skills/list` returns a `skills` array of names and descriptions. `skills/get`
accepts a skill `name` and returns its UTF-8 file content and a complete manifest
with `path`, resource `uri`, byte `size`, and SHA-256 `digest` for each file.
SDK clients sending generic requests must use a parameter model retaining
`name`, rather than the base protocol parameter model, which drops that field.

Clients without extension support can list and read the same files as ordinary
`pyxsd://skills/<name>/<path>` resources. Content is byte-identical to
`python -m pyxsd.skills export NAME DEST`, including uppercase `SKILL.md` and
shared references. Reading content does not activate a skill; the client decides
whether to apply it. Existing documentation resources remain available.

## Schema comparison and migration evidence

`pyxsd_compare_schemas` submits a durable job comparing an old and a new schema
in clean workers. The structural diff reports added, removed and changed
components — including anonymous declarations, which are identified by content
address, so editing an inline declaration re-identifies it as an add/remove pair
rather than a change — plus changed-field details, a namespace-change flag, and
exact totals. A target namespace change re-identifies every component. The
summary is deterministic for unchanged inputs.

An optional corpus validates every supplied document under **both** schemas.
Each file lands in exactly one category — `valid`, `regressed` (was valid, now
invalid), `resolved` (was invalid, now valid), `invalid`, `malformed`,
`unbindable`, or `skipped` — with per-file old/new verdicts and totals covering
every category. Corpus size is capped (default 100, operator maximum 1,000), and
a per-corpus time budget marks remaining files `skipped` instead of failing the
job. Files are validated sequentially per schema.

Potential impact (the structural diff) is reported separately from observed
corpus regressions, and the result states that **no universal compatibility
conclusion follows from a corpus sample** — no-regression evidence applies only
to the tested files and options.

### Source-to-target migration workflow

To migrate documents from an old schema to a new one:

1. `pyxsd_compare_schemas` with the corpus you intend to migrate. Observed
   `regressed` files need transforms; `resolved` files already validate.
2. For each regressed file, `pyxsd_preview_transform` with a transform whose
   source schema is the old revision and whose target schema is the new one.
   The preview independently revalidates the retained candidate bytes in a
   separate clean worker; only validated candidates proceed.
3. `pyxsd_commit_transform` publishes the exact retained candidate bytes. No
   Python is rerun at commit time.
4. Re-run `pyxsd_compare_schemas` (or `pyxsd_validate_document`) on the migrated
   files against the target schema. A no-regressions result on the migrated
   corpus applies only to those files and the options tested — it is not a
   universal compatibility claim.

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

## Local transformation previews

Enable local previews only for code you trust:

```bash
pyxsd-mcp --state-dir /absolute/private/state --execution-backend trusted-local
```

This enables `pyxsd_preview_transform`, `pyxsd_job_status`,
`pyxsd_cancel_job` and `pyxsd_commit_transform`. HTTP refuses trusted-local execution; an unavailable isolated
backend never falls back to local execution.

Call `pyxsd_preview_transform` with `workspace_id`, `document`, `source_schema`,
`transform` and `idempotency_key`. An optional `target_schema` supports migration
previews. Python transforms use this form:

```json
{
  "kind": "python",
  "source": "def transform(document, context):\n    return document\n"
}
```

The function receives a `Document` and context, and may return a `Document`, a
bound root, or `None` for in-place changes. Pipeline recipes use explicit
registered source or supplied definitions, never implicit imports from the
current directory. Source is capped at 64 KiB and recipes at 32 steps, or lower
operator limits.

Poll the returned job ID with `pyxsd_job_status`. A successful job returns a
preview record with original/candidate hashes, validation reports, byte and
positional structural diffs, checkpoint artifacts, warnings and expiry.
Execution terminates before candidate capture. Separate clean workers validate
the retained bytes and any requested checkpoints; executed code's validity
claims do not determine the verdict. Invalid and malformed candidates remain
inspectable. Serialization may change prefixes, layout, mixed-content
presentation and comments; review the byte diff, not just structural changes.

A preview does not publish files. Isolated OCI previews remain unfinished;
do not use the local backend for untrusted code.

### Review and publish the retained candidate

After inspecting the candidate, validation report, diffs and warnings, call
`pyxsd_commit_transform` with the preview ID and candidate digest from the job
result. Choose a workspace-relative destination and a stable idempotency key:

```json
{
  "preview_id": "<preview job ID>",
  "expected_candidate_digest": "<candidate SHA-256>",
  "destination": "reviewed.xml",
  "mode": "new",
  "idempotency_key": "publish-reviewed-1"
}
```

Publication copies the exact retained bytes into a new immutable workspace
revision. It never reruns the transform or serializes its output again. The
source/schema revision must still match the preview, the candidate must not be
expired, and `new` refuses an existing destination. `replace` additionally
requires `pyxsd:replace` and `expected_destination_digest` for that destination.
All publication calls require `pyxsd:write`, read and upload permissions.

Retry the same request and key after a disconnect or coordinator restart to
retrieve the same receipt. A different request conflicts with that key, and a
different key cannot consume the same preview twice. Receipts live as long as
their workspace. The revision head and receipt commit in one SQLite transaction;
interrupted staging is discarded and can be retried without executing Python.

Malformed XML is never publishable. Invalid-but-well-formed output requires an
embedding to enable the write-invalid policy, a `pyxsd:write-invalid` scope and
explicit `allow_invalid=true`; the standard server leaves that policy disabled.
Destinations are coordinator-managed workspace files only, not mounted or
arbitrary host paths. This does not claim atomic replacement against external
writers on shared host directories.

## Restart, errors, and current boundaries

Successful revisions and artifacts survive coordinator restart. Interrupted
staging is discarded rather than treated as published data. Active operations
have deadlines and cancellation cleanup. Job outcomes are durable; interrupted
jobs fail without automatically rerunning code.

- **Missing optional dependency:** reinstall with `pip install 'pyxsd[mcp]'`;
  the CLI reports this without a traceback.
- **`not_found` versus `stale_reference`:** an unknown resource is absent;
  a pinned revision or cursor no longer matches current state.
- **Invalid versus malformed XML:** inspect the report for validation findings;
  malformed input is an operational `invalid_xml` fault.
- **Worker timeout or queue overflow:** retry only after reviewing document size,
  complexity and operator budgets. Neither produces a partial validation verdict.
- **Execution:** disabled by default; trusted-local stdio previews require explicit
  opt-in. Managed exact-byte publication is available; isolated execution remains unfinished.
- **HTTP/OAuth:** available with explicit fixed-issuer configuration and
  loopback-only development fallback. User-code execution over HTTP remains disabled;
  remote deployment still requires the OCI and containment gates.
