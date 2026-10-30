# MCP integration

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

An explicit state directory wins over the project default and deliberately shares
artifacts with other servers selecting it. Relative paths are frozen against the
launch working directory. There is no Git-root search or client-controlled scope
switching. Projects normally gitignore `.pyxsd/`; pyxsd never edits `.gitignore`.
For a read-only project, use `--no-persist` or select a writable private
`--state-dir`. An unwritable default does not prevent discovery; retaining an
output reports a storage fault instead.

For an operator-granted read-only source tree, add `--source-root /absolute/root`.
Only that configured root is captured; a client cannot mount an arbitrary host
directory. Normal session inputs use `pyxsd_put_files` and need no source root.
Create an empty workspace and use paths relative to the source root directly;
operations capture only the referenced files and discovered dependencies. Source
captures carry the real workspace ID and generation `0`, with file digests used
for result identity. Once a workspace has uploads, operations use only those
uploads: missing paths are never silently filled from the source tree.
Persistent state is excluded from source captures, including discovered schema
dependencies. A source root cannot itself be inside the state directory. Uploaded
files with similar relative names are ordinary session inputs.

## Persistent store and session inputs

Servers for different projects are independent. Servers selecting the same store
share retained artifacts through short SQLite transactions; no lifetime process
lock is held. The store holds an artifact index plus each retained output file:

```text
<store>/store-v2/
├── index.sqlite3
└── artifacts/<random-id>/<filename>
```

Workspaces, uploaded input sets, page cursors, compiled schemas, and transform
execution context are **session-only**. Restarting the server discards them. A
restarted session can still list and read previously retained artifacts by ID.
Each server owns separate temporary scratch outside persistent state. Closing one
server never cleans another server's scratch. Discovery, uploads, valid validation
without output, and reads/lists/deletes against an absent store do not create
`.pyxsd/`. `--no-persist` keeps all outputs temporary.

SQLite uses rollback journaling and a 5000 ms contention timeout. Busy/locked
transactions return retryable `state_busy`; other failures are not labeled busy.
Files are staged and fsynced before publication. An already-open read can finish
after another server deletes the artifact; later reads return `not_found`.

Startup does not reconcile or remove unindexed files: they may belong to a live
writer. Crashes may leave unlisted staging or orphan files. To reset the store,
stop **all** servers using it, then remove only its dedicated MCP directory (by
default `.pyxsd/mcp`); this deletes retained outputs too. No online garbage
collection or orphan adoption is performed.

The old user-global default is not migrated, discovered, or deleted. To reuse its
artifacts, stop old servers first and explicitly select that directory with
`--state-dir`. Mixed old/new servers sharing state are unsupported. Legacy
`state.sqlite3` and `workspaces/` files remain untouched.

## Basic workflow

Call `pyxsd_manage_workspace` with `action: "create"`, upload files with
`pyxsd_put_files`, and refer to them by workspace-relative paths. Each upload
atomically adds or replaces the supplied paths; omitted files remain in the
session input set. An invalid batch leaves the previous set intact.
An empty upload is a no-op and returns the unchanged generation and manifest.
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

## Tool catalog

| Tool | What it does |
| --- | --- |
| `pyxsd_capabilities` | Reports version, enabled groups, execution policy, limits, source root, and adapter status. |
| `pyxsd_manage_workspace` | Creates, lists, or releases a session-only input set. |
| `pyxsd_put_files` | Atomically adds or replaces supplied paths in the session input set, retaining omitted files, and returns its generation and file manifest. |
| `pyxsd_inspect_schema` | Returns an overview, compact/full stubs, or constraint-rich component descriptions with bounded depth. |
| `pyxsd_find_schema_components` | Searches literal text, kind, name and namespace filters, or traverses `uses` / `used_by` relationships. |
| `pyxsd_validate_document` | Returns verdicts, exact issue locations, aggregate counts, paged issues, and fingerprints; retains a report artifact only for invalid documents. |
| `pyxsd_validate_documents` | Validates a batch with per-document schemas and outcomes in one capture and worker call; retains reports only for invalid documents. |
| `pyxsd_query_document` | Runs XPath over the bound projection and returns scalars, node summaries, exact locations, or exact source subtrees. |
| `pyxsd_explain_issue` | Gives curated issue-code guidance and can read an exact issue from a retained report. |
| `pyxsd_inspect_model` | Projects JSON Schema for a selected declaration (optional Pydantic integration). |
| `pyxsd_validate_data` | Checks JSON data against a projected contract (optional Pydantic integration). |
| `pyxsd_preview_records` | Projects document rows through Arrow with a 20-row preview cap and optional contextual columns. |
| `pyxsd_export_records` | Writes Parquet or a manifested dataset as retained, digest-verified outputs. |
| `pyxsd_compare_schemas` | Diffs two schemas and optionally validates a corpus under both. |
| `pyxsd_preview_transform` | Runs a trusted-local transform, independently validates the candidate, and retains candidate/report/diff outputs. |
| `pyxsd_list_artifacts` | Lists retained outputs in stable creation order with an opaque cursor. |
| `pyxsd_read_artifact` | Returns a bounded byte range as base64 with digest, media type, and local path. |
| `pyxsd_delete_artifact` | Explicitly deletes one retained output; repeating the call is harmless. |

## Artifacts

Outputs are never removed automatically by time, workspace release, or preview
completion. They remain until `pyxsd_delete_artifact` is called. Validation reports,
response spillover bodies, record exports, and preview outputs are all indexed the
same way. Listing and reading return the local absolute path, size, SHA-256 digest,
media type, creation time, and kind.

`pyxsd_read_artifact` reads exact ranges:

```json
{"artifact_id": "<32-hex ID>", "offset": 0, "length": 8192}
```

The MCP envelope carries base64 expansion, so request bounded ranges. The
coordinator verifies the full file against its indexed digest before returning the
range. The response's `length` is the actual returned byte count, which can be
smaller than requested at end of file; `size` remains the complete artifact size.
A missing or modified file is a typed error, never substituted content.
If the base64 reply cannot fit the response budget, `response_limit` asks for a
smaller range. Artifact reads never spill recursively into another artifact.
Datasets and previews are a set of independent artifacts; deleting one does not
cascade to another.

Large structured results that exceed the response budget are written once as a
`result` artifact. The tool returns a summary and artifact reference instead of
truncated JSON. Read the complete body by range and concatenate it.
The summary preserves pagination, warnings, provenance, and existing artifact
references. Its `bytes` field measures the serialized envelope, not a source file.
`pyxsd_list_artifacts` puts `returned`, `next_cursor`, and `truncated` in the
envelope's `page`, so a spilled listing remains pageable without first reading
its full-result artifact.

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
artifact for a valid document; for an invalid document it retains the full report
so `pyxsd_explain_issue` can inspect an issue after session inputs are released.
Invalid XML is a successful validation with `data.valid=false`;
malformed XML is an `invalid_xml` fault.

Missing inputs fault with `not_found`; unresolved schema dependencies fault with
`schema_incomplete`, and schema report errors fault with `invalid_schema`.
Validation, queries, projection, comparison, and transform validation require
complete schemas; schema inspection remains available to diagnose missing pieces.
`pyxsd_explain_issue` rejects a supplied code that disagrees with a report's issue.
Curated guidance covers both `occurrence-min` and `occurrence-max`.

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
The existing `corpus_max_files` and `batch_seconds` limits apply; split larger
corpora into batches. Valid documents create no report artifact; invalid documents
retain all collected issues within the existing collection/output limits. Use the
singular tool for paged, detailed diagnosis.

### Component and model selection

Overview `roots` lists all visible global element component IDs; `entry_roots`
contains only declarations authored in the entry file. `depth` affects compact/full
views, not overview. Searches reject unknown kinds; bare names match local names
across namespaces, while Clark names (`{namespace}name`) match exactly.

`pyxsd_inspect_model` and `pyxsd_validate_data` accept Clark element names,
`element:{namespace}name` IDs, or unambiguous bare local names. Ambiguity returns
`invalid_argument` with candidates. Nested `path` steps still require Clark names.
Mixed content and repeated composite groups remain core projection limitations;
scope a path to a supported descendant when the root cannot be projected.

## Projected records (optional Arrow)

With the optional `pyarrow` dependency installed, `pyxsd_preview_records` projects
bound document rows in a clean worker and returns at most 20 rows plus a total row
count. Counts are exact by default; `exact_count: false` may report
`count_complete: false` when rows exceed the preview cap. Selection uses either
the declaration path (`element` plus `path`) or an ElementTree occurrence
`selector`, and contextual columns map record, ancestor and root `FieldSource`
values.

`pyxsd_export_records` writes a single-document Parquet file or a multi-document
manifested dataset inside disposable worker output space. The coordinator, never
the worker, captures bounded regular files, hashes them, and retains them in the
output store. Export rejects invalid documents, enforces the document-count corpus
limit and output byte budget, and rejects non-regular or linked output. Datasets
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
Corpus size is capped (default 100, operator maximum 1,000), and a per-corpus time
budget marks remaining files `skipped`. Potential impact is reported separately
from observed corpus regressions; no universal compatibility conclusion follows
from a corpus sample.
Regressed files include bounded new-schema issue `evidence` (code, message,
location); resolved files include old-schema evidence.

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
directory. Source is capped at 64 KiB and recipes at 32 steps, or lower operator
limits.

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

The coordinator verifies inputs and budgets before a clean worker parses or
evaluates XML. Related operations run serially on one cached `Schema`; separate
workers provide isolation and parallelism. IPC uses bounded, role-validated
JSON—never pickle or live `Document` objects. Workers have absolute deadlines,
queue bounds, operation recycling, death recovery and coordinated shutdown.

Important active limits (all operator-configurable in `Settings.limits`):

| Limit | Default |
| --- | --- |
| XML / individual XSD file | 64 MiB / 16 MiB |
| Schema closure files / bytes | 128 / 64 MiB |
| XML depth / element count | 256 / 1,000,000 |
| Inline upload / MCP request | 1 MiB / 2 MiB |
| Session input bytes / workspaces | 512 MiB / 32 |
| Input entries | 4,096 |
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
  launch through `uvx --from 'pyxsd[mcp]'`; the CLI reports this without a
  traceback.
- **`state_busy`:** a SQLite transaction exceeded the bounded contention wait;
  retry the operation. `state_io` and `state_corrupt` are separate storage failures.
- **`invalid_cursor`:** the cursor is malformed, belongs to a different result, or
  refers to inputs that have changed.
- **Invalid versus malformed XML:** inspect the report for validation findings;
  malformed input is an operational `invalid_xml` fault.
- **Worker timeout or queue overflow:** retry only after reviewing document size,
  complexity and operator budgets. Neither produces a partial validation verdict.
- **Execution:** disabled by default; trusted-local stdio previews require explicit
  opt-in. Remote user-code execution is not included.
- **Network transports:** none. The server speaks stdio only; there is no HTTP,
  OAuth/JWKS, multi-tenant, or remote execution mode.
