# MCP integration

```{eval-rst}
.. meta::
   :description: Run pyxsd as a local MCP server to inspect XSD schemas, validate XML, follow diagnostics, and retain bounded output artifacts.
```

pyxsd can run as a local [Model Context Protocol](https://modelcontextprotocol.io/)
server. MCP clients can load schema and XML inputs for a session, explore compiled
schemas, validate XML, follow diagnostics to exact source occurrences, run bounded
XPath queries, transform XML, compare schemas, export records, and read retained
output files.

The server is **stdio-only** and runs beside the MCP client. Execution is disabled
by default. Local transformation previews require an explicit opt-in and run with
the launching user's privileges, not in a sandbox. See {doc}`mcp-deployment` for
the worker boundary.

## Install and launch

The MCP Registry launcher uses `pyxsd mcp`, an alias for `pyxsd-mcp`.
It installs the version-matched `pyxsd[mcp]` extra; the base package alone
does not provide the server's dependencies. Existing configurations below
continue to work unchanged.

The primary launch method is [`uvx`](https://docs.astral.sh/uv/), which creates an
isolated environment with the optional MCP dependencies (Python 3.11 or newer):

```bash
uvx --from 'pyxsd[mcp]' pyxsd-mcp
```

Alternatively, install the optional dependencies and launch the entry point:

```bash
pip install 'pyxsd[mcp]'
pyxsd-mcp
```

`pyxsd[mcp]` is the minimal server install. For the projected-record tools,
install the complete integration set instead:

```bash
pip install 'pyxsd[mcp-all]'
pyxsd-mcp
```

The equivalent isolated launch is `uvx --from 'pyxsd[mcp-all]' pyxsd-mcp`.
Arrow preview and export still run in clean workers; installing `mcp-all`
does not enable trusted-local transforms.

`python -m pyxsd.mcp` is equivalent after installation. The default store is
`<project-root>/.pyxsd/mcp`, created only when an artifact is retained. The project
root is the launch working directory; clients that launch elsewhere should pass
`--project-root /absolute/project`. A generic stdio client configuration looks like this:

```json
{
  "mcpServers": {
    "pyxsd": {
      "command": "uvx",
      "args": ["--from", "pyxsd[mcp]", "pyxsd-mcp"]
    }
  }
}
```

Select another persistent store with `--state-dir /absolute/private/path`. Use
`--no-persist` for a temporary store that is removed on orderly shutdown; it
cannot be combined with `--state-dir`.

Execution is configured at launch. An explicit `--execution-backend
disabled|trusted-local` wins over `PYXSD_MCP_EXECUTION_BACKEND`, which wins
over the default `disabled`. An invalid environment value aborts startup.
This setting applies to `pyxsd_preview_transform` only:

```bash
PYXSD_MCP_EXECUTION_BACKEND=disabled pyxsd-mcp
```

Use that explicit `disabled` form when a wrapper or shell profile has already
exported `trusted-local` but a particular server must not run supplied Python.
See {doc}`mcp-deployment` for the trust boundary.

Resource limits are separate from execution and are **disabled by default**:
file sizes, XML depth and element counts, corpus sizes, IPC/result sizes,
paging, cache, worker count/queue, and operation deadlines are unlimited unless
enabled. Enable the current numeric defaults or tune individual values at launch:

```bash
pyxsd-mcp --resource-limits enabled
pyxsd-mcp --resource-limits enabled --limit xml_file_bytes=134217728 \
  --limit corpus_files=500
PYXSD_MCP_RESOURCE_LIMITS=enabled PYXSD_MCP_CORPUS_FILES=500 pyxsd-mcp
```

`--resource-limits` wins over `PYXSD_MCP_RESOURCE_LIMITS`; otherwise the default
is `disabled`. Per-field `--limit FIELD=VALUE` wins over
`PYXSD_MCP_<UPPERCASE_FIELD>`, which wins over the current default. Values are
positive decimal integers. Unknown or malformed values, and any override while
limits resolve disabled, abort startup. `workers` and `worker_queue` are
ordinary tunable fields under the same toggle. Disabled limits remove guardrails:
a local server may consume memory, spawn many workers, accept huge IPC frames,
or spend unbounded time on a pathological input. See {doc}`mcp-deployment`.

See {doc}`mcp-deployment` for source roots, persistent-store layout, shared
processes, cleanup, and operator limits. Normal session uploads need no source root.

## Validate XML from a Python client

Install `pyxsd[mcp]` in the environment that runs this script; the `uvx`
server environment alone does not install client imports into your project.
Save the following as `validate_client.py`, then run
`python validate_client.py`. It starts and closes its own stdio server, uploads
both inputs, and prints `valid: True`.

The server uses `--no-persist`: uploads end with this session, and any retained
outputs are removed on orderly shutdown. Transformation execution stays
disabled. For persistent or sensitive document data, choose storage using
{doc}`mcp-deployment` before changing these launch options.

```python
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "pyxsd.mcp", "--no-persist"]
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as client,
    ):
        with anyio.fail_after(30):
            await client.initialize()
            created = await client.call_tool("pyxsd_manage_workspace", {"action": "create"})
            workspace = created.structured_content["data"]["id"]
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {
                            "path": "order.xsd",
                            "text": (
                                '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                                '<xs:element name="order" type="xs:int"/></xs:schema>'
                            ),
                        },
                        {"path": "order.xml", "text": "<order>42</order>"},
                    ],
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
            print("valid:", result.structured_content["data"]["valid"])


if __name__ == "__main__":
    anyio.run(main)
```

## Persistent store and session inputs

Uploads, workspaces, compiled schemas, cursors, and execution context last only
for the server session. Retained output artifacts survive restart unless the
server uses `--no-persist` or they are explicitly deleted. Releasing a workspace
removes its inputs, not its outputs. Treat retained files as document data and
choose their storage location accordingly; see {doc}`mcp-deployment`.

## Basic workflow

Call `pyxsd_manage_workspace` with `action: "create"`, upload files with
`pyxsd_put_files`, and refer to them by workspace-relative paths. Each upload
atomically adds or replaces the supplied paths; omitted files remain in the
session input set. An invalid batch leaves the previous set intact.
An empty upload is a no-op and returns the unchanged generation and manifest.
Uploads encode `text` as UTF-8 without rewriting declarations. UTF-8 declarations
and ASCII/US-ASCII declarations with ASCII-only bytes are accepted unchanged.
Conflicting declarations are rejected; use `--source-root` for original bytes in
other encodings. The text upload API has no arbitrary-byte upload field.
`action: "release"` removes only session input scratch. It never removes output
artifacts.

```json
{
  "document": {"path": "order.xml"},
  "schema_ref": {
    "source": {"path": "order.xsd"},
    "options": {
      "binding": "lax",
      "namespaces": "strict",
      "xsd_version": "1.1",
      "namespace_schemas": {}
    }
  }
}
```

References contain only a path. There are no input revisions, leases, owners, or
revision pins. Each operation copies the current uploaded set into a private
temporary capture, verifies its manifest, and deletes it when the operation ends.
Page cursors are bound to the request result identity and to the input generation
and file digests; changing the inputs invalidates old pages.
The provenance field `schema_fingerprint` is a conservative fingerprint of the
complete captured input manifest plus the schema reference/options, not a
standalone hash of the schema closure. It can change when an instance or unrelated
uploaded file changes. Use the per-source digest for document identity; do not
infer a schema revision from a changed capture fingerprint alone.

## Tool catalog

| Tool | What it does |
| --- | --- |
| `pyxsd_capabilities` | Reports version, enabled groups, execution policy, limits, source root, and adapter status. |
| `pyxsd_manage_workspace` | Creates, lists, or releases a session-only input set. |
| `pyxsd_put_files` | Atomically adds or replaces supplied paths in the session input set, retaining omitted files, and returns its generation and file manifest. |
| `pyxsd_inspect_schema` | Returns an overview, compact/full stubs, constraint-rich descriptions, or a diagnostics report with measured namespace evidence. |
| `pyxsd_find_schema_components` | Searches literal text, kind, name and namespace filters, or traverses `uses` / `used_by` relationships. |
| `pyxsd_validate_document` | Returns verdicts, exact issue locations, aggregate counts, paged issues, and fingerprints; retains a report artifact only for invalid documents. |
| `pyxsd_validate_documents` | Validates a batch with per-document schemas and outcomes in one capture and worker call; retains reports only for invalid documents. |
| `pyxsd_query_document` | Runs XPath over the bound projection and returns scalars, node summaries, exact locations, or exact source subtrees. |
| `pyxsd_explain_issue` | Gives curated issue-code guidance and can read an exact issue from a retained report. |
| `pyxsd_inspect_model` | Projects JSON Schema for a selected declaration (optional Pydantic integration). |
| `pyxsd_validate_data` | Checks JSON data against a projected contract (optional Pydantic integration). |
| `pyxsd_preview_records` | Projects document rows through Arrow with a 20-row preview cap and optional contextual columns. |
| `pyxsd_export_records` | Writes Parquet or a manifested dataset as retained, digest-verified outputs. |
| `pyxsd_compare_schemas` | Diffs two schemas and validates an explicit corpus or a bounded `corpus_glob` selection under both. |
| `pyxsd_preview_transform` | Runs a trusted-local transform, independently validates the candidate, and retains candidate/report/diff outputs. |
| `pyxsd_list_artifacts` | Lists retained outputs in stable creation order with an opaque cursor. |
| `pyxsd_read_artifact` | Returns bytes from an offset as base64 with digest, media type, and local path; omitted `length` reads through EOF. |
| `pyxsd_delete_artifact` | Explicitly deletes one retained output; repeating the call is harmless. |

## Artifacts

Outputs are never removed automatically by time, workspace release, or preview
completion. They remain until `pyxsd_delete_artifact` is called. Validation reports,
response spillover bodies, record exports, and preview outputs are all indexed the
same way. Listing and reading return the local absolute path, size, SHA-256 digest,
media type, creation time, and kind.

`pyxsd_read_artifact` reads from an offset and either a requested `length` or,
when `length` is omitted, through end of file:

```json
{"artifact_id": "<32-hex ID>", "offset": 0, "length": 8192}
```

The MCP envelope carries base64 expansion. With resource limits enabled, request
bounded ranges. The coordinator verifies the full file against its indexed digest
before returning the range. The response's `length` is the actual returned byte count, which can be
smaller than requested at end of file; `size` remains the complete artifact size.
A missing or modified file is a typed error, never substituted content.
If resource limits are enabled and the base64 reply cannot fit the response
budget, `response_limit` asks for a smaller range. Artifact reads never spill
recursively into another artifact.
Datasets and previews are a set of independent artifacts; deleting one does not
cascade to another.

When resource limits are enabled, structured results that exceed the response
budget are written once as a `result` artifact. The tool returns a summary and
artifact reference instead of truncated JSON. Read the complete body by range
and concatenate it. With limits disabled, results return inline.
The summary preserves pagination, warnings, provenance, and existing artifact
references. Its `bytes` field measures the serialized envelope, not a source file.
`pyxsd_list_artifacts` puts `returned`, `next_cursor`, and `truncated` in the
envelope's `page`, so a spilled listing remains pageable without first reading
its full-result artifact.
Listing entries are in `data.artifacts`; the envelope's top-level `artifacts`
references outputs produced by that call (including a spilled listing). These
fields have different roles and need not contain the same references.

Resources include `pyxsd://issue-codes`, `pyxsd://capabilities`, and
`pyxsd://artifacts/{artifact_id}`. The three prompts are `pyxsd_diagnose_xml`,
`pyxsd_author_pipeline`, and `pyxsd_evaluate_migration`. They provide optional
workflow guidance; no prompt or skill is required for tool correctness.

## Schema exploration and diagnostics

Exploration and query operations run in clean workers against a verified temporary
capture. Related operations may reuse a compiled schema when the complete schema
manifest and options match; cached schemas are rebound to the current capture, so
replacing session inputs cannot reuse deleted paths.

`ValidationIssue.location` is a structured path with expanded names and one-based
same-name sibling indexes. It identifies the exact occurrence, not merely the
first element with a matching local name. Query with `result: "subtree"` to
retrieve that occurrence's source bytes and location together.

A subtree result is an exact slice of the captured source, not regenerated XML:

| Key | Value |
| --- | --- |
| `name`, `location` | The bound node's name and its exact structured occurrence address. |
| `encoding` | `"base64"` (also present when the source is unavailable). |
| `data` | Base64 of the original bytes, or `null`. |
| `byte_length` | `end - start`, or `null`. |
| `span` | `{"start": int, "end": int}` in original-encoding bytes, or `null`. |
| `source_faithful` | `true` for a slice, `false` when unavailable. |
| `fault` | `null`, or `{"code": "source_unavailable", "message": ...}`. |

`span` is a half-open range in the original document's encoding; `data` is those
bytes base64-encoded, and `byte_length` equals `end - start`. The slice starts at
the element's opening `<` and ends just past its closing tag or `/>`, so it
carries the element's original prefixes, attribute order and quoting, comments,
CDATA, and character data. It excludes the element's trailing tail text and any
XML declaration, and it is **not** a standalone document in general: namespace
prefixes may be declared on an ancestor and therefore be absent from the slice.
Restore the in-scope declarations before parsing a slice on its own.

The bytes stay in their original encoding. A UTF-16 document returns UTF-16
bytes (with offsets counted in those bytes, not characters); pyxsd does not
re-encode a slice to UTF-8. A `source_unavailable` fault is per result: `data`,
`byte_length`, and `span` are null and `source_faithful` is false, with no
fallback to a serialized or regenerated subtree. The source index is built only
when a page actually selects nodes, under the same strict parser policy and XML
byte/depth/element budgets as the semantic parse. Summary results are unchanged
and do not carry the source keys.

Missing-child diagnostics identify a container rather than a nonexistent child;
check `location_precision` before treating an issue as an exact occurrence.

Reports distinguish aggregate counts from collection suppression and detail
pagination. `pyxsd_validate_document` returns `report: null` and writes no report
artifact or issue page for a valid document. For an invalid document each page
retains that page's issues with aggregate counts, so `pyxsd_explain_issue` can
inspect an issue after session inputs are released. Pass that page's report
artifact and a page-local issue index; a same-code issue on a different page
cannot be distinguished by the code check alone.
Invalid XML is a successful validation with `data.valid=false`;
malformed XML is an `invalid_xml` fault.

Missing inputs fault with `not_found`; unresolved schema dependencies fault with
`schema_incomplete`, and schema report errors fault with `invalid_schema`.
Missing uploaded input faults identify `role`, `path`, and `missing`. Malformed
XML faults retain the parser's diagnosis and line/column when available.
Validation, queries, projection, comparison, and transform validation require
complete schemas; schema inspection remains available to diagnose missing pieces.
`pyxsd_explain_issue` rejects a supplied code that disagrees with a report's issue.
Curated guidance covers both `occurrence-min` and `occurrence-max`.
`compose-cycle` guidance concerns the schema dependency chain, not instance
repair. Unrecognized issue codes return `known: false` and no curated actions.

### Schema diagnostics and namespace evidence

`pyxsd_inspect_schema` also accepts `view: "diagnostics"`:

```json
{
  "workspace_id": "<workspace ID>",
  "schema_ref": {"source": {"path": "schema.xsd"}, "options": {"namespaces": "legacy"}},
  "view": "diagnostics"
}
```

The result contains the complete schema report, a `recommendations` list, and
`namespace_probe`. When a legacy report contains a prefixed
`unknown-substitution-head`, the server compiles the same captured schema once
with strict namespaces solely to measure the alternative. It does not change
the requested reference or reuse the strict schema for the current operation.

A successful probe reports current and alternative error/warning counts and
issue-code counts, plus codes introduced and resolved by strict mode. A
recommendation appears only when strict mode resolves at least one unknown
substitution head, lowers the total error count, and introduces no new issue
code; it then contains a complete replacement `schema_ref` to review and use
explicitly. If the alternate compile fails, `namespace_probe.status` is
`"error"` with a bounded fault while the original report and an empty
recommendation list remain available.

Use `pyxsd_explain_issue` for curated `unknown-substitution-head` checks
(prefix bindings and declaration identity) before changing namespace options.
Strict namespaces are not a remedy for `xsd:element ref` navigation or complex
child access; those remain separate declaration-path concerns.

### Batch validation

For a corpus, prefer `pyxsd_validate_documents` to repeated singular calls:

```json
{
  "workspace_id": "<workspace ID>",
  "documents": [
    {"document": {"path": "one.xml"}, "schema_ref": {"source": {"path": "one.xsd"}}},
    {"document": {"path": "two.xml"}, "schema_ref": {"source": {"path": "two.xsd"}}}
  ]
}
```

One capture and worker call serve the batch; each unique schema is leased once.
Results preserve request order, including duplicates, and include verdict/counts
or a typed item fault. Malformed documents and schema-group failures do not abort
other groups. Missing uploaded references return item faults whose details name
the `path`, all `missing` paths, and whether the first missing `role` is the
`document` or `schema`. Source-tree capture or worker/protocol failures can reject
the whole call. Omitted `schema_ref` uses document schema-location hints.
The existing `corpus_max_files` and `batch_seconds` limits apply when resource
limits are enabled; with limits disabled, no corpus split is required. Valid
documents create no report artifact; invalid documents retain all collected
issues. Use the singular tool for paged, detailed diagnosis.

### Component and model selection

Overview `roots` lists all visible global element component IDs; `entry_roots`
contains only declarations authored in the entry file. `depth` affects compact/full
views, not overview. Searches reject unknown kinds; bare names match local names
across namespaces, while Clark names (`{namespace}name`) match exactly.

`pyxsd_inspect_model` and `pyxsd_validate_data` accept Clark element names,
`element:{namespace}name` IDs, or unambiguous bare local names. Ambiguity returns
`invalid_argument` with candidates. Nested `path` steps use the declaration
spelling selected by namespace mode: legacy/unqualified steps are bare local
names, while strict/qualified steps are Clark names. Mixed content and repeated
composite groups remain core projection limitations.

When the selected declaration cannot be projected, `unsupported_projection`
fault details carry `projection_candidates` found by a bounded descendant
search. Each candidate contains `element`, a mode-compatible `path`, and
`repeated`; every listed route was accepted by the same projection backend.
`searched_declarations` counts the bounded walk, and `search_truncated: true`
means a depth, search, or result cap stopped it — an empty candidate list then
describes only the searched subset. Pass a returned path back to the same tool
rather than editing its spelling by hand.

## Projected records (optional Arrow)

With the optional `pyarrow` dependency installed — individually through
`pyxsd[arrow]`, or as part of `pyxsd[mcp-all]` for the server —
`pyxsd_preview_records` projects bound document rows in a clean worker and
returns at most 20 rows plus a total row count. Counts are exact by default;
`exact_count: false` may report `count_complete: false` when rows exceed the
preview cap. Selection uses either the declaration path (`element` plus `path`)
or an ElementTree occurrence `selector`, and contextual columns map record,
ancestor and root `FieldSource` values.

`pyxsd_export_records` writes a single-document Parquet file or a multi-document
manifested dataset inside disposable worker output space. It is available
whenever Arrow is healthy; it does not require the trusted-local execution
backend because it runs no supplied Python transform. The coordinator, never
the worker, captures bounded regular files, hashes them, and retains them in the
output store; source inputs are never written. Export rejects invalid documents,
and when resource limits are enabled enforces the document-count corpus limit
and output byte budget; it always rejects non-regular or linked output. Datasets
never append to existing output.

## Schema comparison and migration evidence

`pyxsd_compare_schemas` compares an old and a new schema in clean workers and
returns the result directly. The structural diff reports added, removed and changed
components using comparison-specific declaration identities, plus changed-field
details, a namespace-change flag, and exact totals. A target
namespace change re-identifies every component. The summary is deterministic for
unchanged inputs.
Identical schemas moved to another directory produce an empty diff. Named local
declarations are identified by their owner and name; anonymous declarations use
structural roles with collision discriminators. Navigation component IDs remain
unchanged. `changed_details` entries have `id` and `changes`, each containing
`field`, `old`, and `new` JSON values, excluding location noise. Referring elements
can also change when they embed a changed type's constraints.

An optional corpus validates every supplied document under **both** schemas. Each
file lands in exactly one category: `valid`, `regressed`, `resolved`, `invalid`,
`malformed`, `unbindable`, or `skipped`, with per-file old/new verdicts and totals.
Corpus size is capped when limits are enabled (default 100, operator maximum
1,000), and a per-corpus time budget marks remaining files `skipped`. Potential impact is reported separately
from observed corpus regressions; no universal compatibility conclusion follows
from a corpus sample.
Regressed files include bounded new-schema issue `evidence` (code, message,
location); resolved files include old-schema evidence.

Instead of listing every file, pass `corpus_glob` and omit `corpus`; supplying
both is rejected. The glob is workspace/source-root relative POSIX syntax:
`*`, `?`, and character classes match within one segment, while `**` crosses
directories, so `corpus/**/*.xml` selects XML files at and below `corpus/`.
Selection uses the active input set, is lexically deterministic, and follows
the same precedence as every capture: uploaded files win, and an empty
workspace with `--source-root` selects from that read-only tree. The reply adds
`corpus_selection: {"glob": ..., "matched": ...}`; explicit corpus requests
carry `null`. An empty match is a valid zero-file corpus, while a selection or
capture above `corpus_files` fails with `quota_exceeded` rather than silently
truncating. Selected files then pass through the same verified temporary
capture: if source bytes change, one capture retry obtains a consistent
manifest, and persistent changes fault rather than compare mixed revisions.

A schema or profile migration against an existing fixture tree needs no
uploads:

```bash
pyxsd-mcp --source-root /absolute/project
```

```json
{
  "workspace_id": "<empty workspace ID>",
  "old_schema": {"source": {"path": "schemas/2.3/schema.xsd"}},
  "new_schema": {"source": {"path": "schemas/2.4/schema.xsd"}},
  "corpus_glob": "fixtures/**/*.xml"
}
```

The rendering example families (`examples/healthcare`, `examples/citygml`,
`examples/bpmn`, `examples/opendrive`, `examples/xrechnung`, and
`examples/musicxml`) document their own pinned schema and document locations;
read the relevant family README before pointing `--source-root` at its cache.
The comparison remains offline and reads only the operator-selected root.

A migration review typically:

1. Compares old and new schemas with the intended corpus.
2. Previews a transform for each file that needs one, optionally with a target
   schema.
3. Inspects the retained candidate, report, byte diff and structural diff.
4. Saves the candidate with the client's own file tools if it should persist in a
   project.
5. Revalidates the saved files against the target schema.

## Packaged skill discovery

The server implements the [Final skills extension](https://modelcontextprotocol.io/extensions/skills/overview)
under `io.modelcontextprotocol/skills` through the SDK extension interface.
`skills/list` returns one complete page of atomic skill manifests. Each includes
the `SKILL.md` URI, complete frontmatter, and every supporting resource's `uri`,
UTF-8 byte `size`, and `sha256:<hex>` digest. `skills/get` accepts that skill URI
and returns the identical manifest. Unknown skill URIs and cursors are
invalid-parameter errors. Complete responses have public cache scope and a 60,000
ms TTL; clients retrieve file contents using `resources/read`.

Clients without extension support can list and read the same files as ordinary
`pyxsd://skills/<name>/<path>` resources. Content is byte-identical to
`python -m pyxsd.skills export NAME DEST`. Reading content does not activate a
skill; the client decides whether to apply it.

## Local transformation previews

Enable local previews only for code you trust:

```bash
pyxsd-mcp --execution-backend trusted-local
```

For scripted launches, `PYXSD_MCP_EXECUTION_BACKEND=trusted-local` selects the
same backend when no CLI value is supplied. An explicit `disabled` CLI option
or environment value overrides it; record preview and export are unaffected
either way.

This enables `pyxsd_preview_transform`. The server is stdio-only; remote or
untrusted-code execution is not supported, and an unavailable backend never falls
back to another execution mode.

Call `pyxsd_preview_transform` with `workspace_id`, `document`, `source_schema`
and `transform`. An optional `target_schema` supports migration previews. Python
transforms use this form:

```json
{
  "kind": "python",
  "source": "def transform(document, context):\n    return document\n"
}
```

The function receives a `Document` and context and may return a `Document`, a
bound root, or `None` for in-place changes. Pipeline recipes use explicit
registered or supplied definitions, never implicit imports from the current
directory. Source is capped at 64 KiB and recipes at 32 steps when resource
limits are enabled, or lower operator limits.

The call returns a preview record immediately. It contains the workspace and
generation, input/schema/code digests, candidate/report/diff/structural-diff output
references, checkpoint references, validity flags, and warnings. The candidate,
report, byte diff and structural diff are retained artifacts; inspect the exact
candidate bytes with `pyxsd_read_artifact`.

Execution terminates before candidate capture. Separate clean workers validate the
retained bytes and checkpoints; executed code's validity claims do not determine
the verdict. Invalid and malformed candidates remain inspectable. Serialization
may change prefixes, layout, mixed-content presentation and comments; review the
byte diff, not only structural changes. Original input bytes are kept solely in
private temporary capture space through validation and structural comparison, and
are discarded afterward.

There is no commit or publication tool. If a candidate should become part of a
project, read its bytes and write the file with the client's file tools. The
server never writes into the source project. Do not use the local execution
backend for untrusted code.

## Worker and resource model

The coordinator verifies inputs before a clean worker parses or evaluates XML.
Related operations run serially on one cached `Schema`; separate workers provide
isolation and parallelism. IPC uses bounded, role-validated JSON—never pickle or
live `Document` objects. When resource limits are enabled, workers also have
absolute deadlines, queue bounds, operation recycling, death recovery and
coordinated shutdown.

Resource enforcement is disabled by default. When enabled
(`--resource-limits enabled`), the following numeric defaults apply; every field
is tunable with `--limit` or `PYXSD_MCP_<UPPERCASE_FIELD>`. With limits disabled,
every field is `null` and no byte, count, time, depth, page, preview, issue,
message, cache, recycle, worker or queue ceiling applies. Explicit caller-supplied
page/issue/list bounds still apply; omitted ones are unlimited.

| Limit | Default |
| --- | --- |
| XML / individual XSD file | 64 MiB / 16 MiB |
| Schema closure files / bytes | 128 / 64 MiB |
| XML depth / element count | 256 / 1,000,000 |
| Inline upload / MCP request | 1 MiB / 2 MiB |
| Session input bytes / workspaces | 512 MiB / 32 |
| Input entries | 4,096 |
| Path bytes / segments / segment bytes | 4,096 / 128 / 255 |
| Projection search depth / visited / candidates | 8 / 100 / 10 |
| I/O chunk bytes | 65,536 |
| Result payload / page items | 24 KiB / 50 |
| Preview rows | 20 |
| Collected issues / message | 10,000 / 4 KiB |
| Output artifact | 128 MiB |
| Clean cache entries / workers | 8 / 4 |
| Worker operations before recycling | 100 |
| Worker queue slots | 16 |
| Compile or query deadline | 30 seconds |
| Transform / batch deadline | 60 / 300 seconds |
| Transform source / pipeline steps | 64 KiB / 32 |
| Corpus files / file validation budget | 100 / 10 seconds |

Compilation and binding choices are explicit per schema reference: XSD 1.0 or 1.1,
strict or lax best-effort binding, and legacy or strict namespace handling.
Serialized XML is UTF-8, and preview byte diffs report serialization-only changes
rather than hiding them.

The local subprocess runtime is not a sandbox for untrusted Python. Remote or
untrusted-code execution is not included. See {doc}`mcp-deployment` for the
execution boundary.

## Audit records and restart behavior

Exploration operations emit one structured audit event with the operation, status,
duration, workspace/generation, cache outcome, and input digests where applicable.
Audit payloads never include XML bodies, transform source, file contents, host
paths, tokens, or user identities. An embedding may provide a metrics callback,
which receives the same already-redacted event mapping.

Retained artifacts survive restart. Session workspaces, uploaded inputs, page
cursors, compiled schemas, and preview execution context do not. Active operations
have deadlines and process-group cancellation cleanup; interrupted work is not
retried automatically and produces no partial artifact.

- **Missing optional dependency:** reinstall with `pip install 'pyxsd[mcp]'` or
  `pyxsd[mcp-all]` when Arrow tools are needed, or launch through the matching
  `uvx --from` form; the CLI reports this without a traceback.
- **`state_busy`:** a SQLite transaction exceeded the bounded contention wait;
  retry the operation. `state_io` and `state_corrupt` are separate storage failures.
- **`invalid_cursor`:** the cursor is malformed, belongs to a different result, or
  refers to inputs that have changed.
- **Invalid versus malformed XML:** inspect the report for validation findings;
  malformed input is an operational `invalid_xml` fault.
- **Worker timeout or queue overflow:** with resource limits enabled, retry only
  after reviewing document size, complexity and operator budgets. Neither
  produces a partial validation verdict.
- **Execution:** disabled by default; trusted-local stdio previews require explicit
  opt-in. Remote user-code execution is not included.
- **Network transports:** none. The server speaks stdio only; there is no HTTP,
  OAuth/JWKS, multi-tenant, or remote execution mode.
