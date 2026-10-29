---
name: pyxsd-mcp
description: Drive pyxsd through the local stdio Model Context Protocol — session inputs, validation with exact occurrences, direct comparisons, transform previews, exports, and retained local outputs.
---

# pyxsd MCP workflow

Use this skill when an MCP client must use the pyxsd server. The primary launch
method is `uvx --from 'pyxsd[mcp]' pyxsd-mcp`. Alternatively, install with
`pip install 'pyxsd[mcp]'` and launch `pyxsd-mcp` directly. The server is local
stdio only; there is no HTTP or remote mode. Everything below also has a
Python/CLI fallback when the server is absent.

## Discovery

Call `pyxsd_capabilities` first. It reports the version, enabled tool groups,
execution backend, source root, limits, and adapter availability. Do not assume
execution or Arrow features — check `adapters.arrow.available` and
`execution_backend`.

## Basic workflow

1. `pyxsd_manage_workspace` with `action: "create"`.
2. `pyxsd_put_files` with schema and document texts. The reply carries the
   session generation and file manifest. Uploading again atomically adds or
   replaces the supplied paths and retains omitted files; there are no durable
   revisions.
   With `--source-root`, leave the workspace empty instead and pass relative
   paths into that tree. Captures use generation 0 plus file digests. Uploaded
   inputs and source-root files are never merged implicitly. Empty uploads are
   a no-op.
3. `pyxsd_validate_document` — invalid XML is a **successful call** with
   `data.valid == false`; malformed XML is an error fault (`invalid_xml`).
   Counts are aggregate; issue details page via `page.next_cursor`. Cursors are
   bound to the query result and current input generation. Valid documents return
   `report: null` and create no report artifact; invalid documents retain one.
4. `pyxsd_query_document` follows an issue to its exact occurrence: repeated
   nodes are identified by same-name sibling indexes in `location.path`, and
   `result: "subtree"` regenerates that occurrence's XML from bound data; it is
   not source-faithful and does not preserve comments or formatting.
5. `pyxsd_explain_issue` gives curated guidance. Pass the retained report
   artifact to inspect one exact issue; reports remain readable after session
   inputs are released.
6. `pyxsd_manage_workspace` with `action: "release"` removes only session inputs.
   It never deletes retained artifacts.

For a corpus, use `pyxsd_validate_documents` with `documents` items containing
`document: {path: ...}` and optional `schema_ref: {source: {path: ...}}`.
One capture/worker call handles the batch; results retain request order and
contain per-document verdicts/counts or faults. Reports are retained only for
invalid documents. Split at `limits.corpus_max_files`; use the singular tool for
paged diagnostics. Missing uploaded inputs, malformed XML, and schema-group
failures return item faults; source-tree capture failures may reject a whole batch.
Missing-input fault details include `role`, `path`, and `missing`.

`not_found` means an input is missing; `schema_incomplete` means a required
include/import is missing; `invalid_schema` means schema errors. These are not
evidence that a document is invalid. Inspect incomplete schemas to diagnose them.

## Outputs

Validation reports, oversized response bodies, record exports, and transform
previews are stored as local artifacts with an ID, absolute path, size, SHA-256
digest, media type, creation time, and kind.

- `pyxsd_list_artifacts` pages in stable creation order using envelope
  `page.next_cursor`; the cursor survives response spillover.
- `pyxsd_read_artifact` returns a bounded base64 range plus actual `length`, total
  `size`, digest, media type, and local path. `length` can be smaller than
  requested at end of file. Concatenate ranged reads for a large body; reduce
  length on `response_limit` (reads never spill recursively).
- `pyxsd_delete_artifact` explicitly deletes one output. Repeating it is
  harmless. Nothing is removed automatically by time, workspace release, or
  preview completion.

Artifacts survive a server restart. Session inputs, page cursors, and preview
execution context do not; recreate them and rerun the operation.

## Comparison and migration

`pyxsd_compare_schemas` returns **structural impact** separately from **corpus
evidence** (each file classified valid/regressed/resolved/invalid/malformed/
unbindable/skipped under both schemas). Identical schemas moved to another path
produce an empty diff. Named local declarations stay stable across sibling edits;
`changed_details` contains actual old/new field values. Regressed/resolved corpus
items carry bounded issue evidence from the failing side. A no-regressions result
applies only to the tested files and options.

For a source-to-target migration: compare old/new with the corpus, preview a
transform (`pyxsd_preview_transform` — validation happens in clean workers on
the retained candidate bytes), and inspect the candidate, report, byte diff, and
structural diff. There is no commit or publication tool. If the candidate should
persist in a project, read its bytes and write the file with the client's own file
tools.

## Previews and exports

`pyxsd_preview_transform` requires explicit trusted-local execution:
`pyxsd-mcp --execution-backend trusted-local`. It runs code with the launching
user's privileges, not in a sandbox. Invalid and malformed candidates remain
inspectable. Original inputs are held only in temporary operation space through
independent validation, then discarded; candidate/report/diff outputs are
retained.

`pyxsd_export_records` (execution group + Arrow) writes Parquet or a manifested
dataset as retained digest-verified artifacts. Download via `pyxsd_read_artifact`
and verify the digest. `pyxsd_preview_records` gives bounded row previews with
exact/incomplete counts.

## Fallback

With `pyxsd[mcp]` not installed, use the pyxsd Python/CLI workflow directly (see
the pyxsd-development skill). Do not claim MCP features exist when the server is
not running.
