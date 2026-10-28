---
name: pyxsd-mcp
description: Drive pyxsd through the local stdio Model Context Protocol — workspaces, validation with exact occurrences, direct comparisons, previews, and exports with immutable artifacts.
---

# pyxsd MCP workflow

Use this skill when an MCP client must use the pyxsd server. The primary launch
method is `uvx --from 'pyxsd[mcp]' pyxsd-mcp --state-dir <private path>`.
Alternatively, install with `pip install 'pyxsd[mcp]'` and launch
`pyxsd-mcp --state-dir <private path>` directly. The server is local stdio only;
there is no HTTP or remote mode. Everything below also has a Python/CLI fallback
when the server is absent.

## Discovery

Call `pyxsd_capabilities` first: it reports the version, enabled tool groups,
execution backend, and adapter availability. Do not assume execution or Arrow
features — check `adapters.arrow.available` and `execution_backend`.

## Basic workflow

1. `pyxsd_manage_workspace` (`action: "create"`, optional `idempotency_key`).
2. `pyxsd_put_files` with schema and document texts; the reply carries the
   immutable revision. Pin `revision` on later references for repeatable
   reads.
3. `pyxsd_validate_document` — invalid XML is a **successful call** with
   `data.valid == false`; malformed XML is an error fault (`invalid_xml`).
   Counts are aggregate; issue details page via `page.next_cursor`. Cursors
   are bound to the query — never reuse one for a different request.
4. `pyxsd_query_document` follows an issue to its exact occurrence:
   repeated nodes are identified by same-name sibling indexes in
   `location.path`, and `result: "subtree"` returns that occurrence's XML.
5. `pyxsd_explain_issue` gives curated guidance for a code; unknown codes say
   so rather than inventing repairs.

## Direct results and session previews

`pyxsd_compare_schemas` and `pyxsd_export_records` return their result payloads
directly — there is no polling or cancel step. `pyxsd_preview_transform` returns
a preview record immediately. Preview records are session-scoped: commit them in
the same server process, and rerun the preview after a restart. Publication uses
the preview's exact retained bytes and a stable `idempotency_key`; the same key
and input replay the receipt, while different input is an `idempotency_conflict`.

## Comparison and migration

`pyxsd_compare_schemas` reports **structural impact** (added, removed,
changed components — anonymous declarations are content-addressed, so edits
appear as add/remove pairs) separately from **corpus evidence** (each file
classified valid/regressed/resolved/invalid/malformed/unbindable/skipped
under both schemas). The result explicitly states that no universal
compatibility conclusion follows from a corpus sample; no-regressions
applies only to the tested files and options.

A source→target migration: compare old/new with the corpus, preview a
transform (`pyxsd_preview_transform` — validation happens in a separate
clean worker on coordinator-retained bytes), then commit the retained
candidate (`pyxsd_commit_transform`) which republishes the exact validated
bytes without rerunning user code.

## Exports

`pyxsd_export_records` (execution group + Arrow) writes Parquet or a
manifested dataset as immutable digest-verified artifacts; download via
`pyxsd_read_artifact` and verify the digest. `pyxsd_preview_records` gives
bounded row previews with exact/incomplete counts.

## Fallback

With `pyxsd[mcp]` not installed, use the pyxsd Python/CLI workflow directly
(see the pyxsd-development skill). Do not claim MCP features exist when the
server is not running.
