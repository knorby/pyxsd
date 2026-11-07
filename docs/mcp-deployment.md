# MCP worker deployment

This page covers storage, input access, worker limits, and process trust. For
installation and tool usage, start with {doc}`mcp`. The server is a
**local stdio process** that runs beside the MCP client. It is not a hosted
multi-tenant service, and no network listener is included.

## Runtime choices

- Clean workers are Python subprocesses with bounded JSON IPC, per-schema
  serialization, deadlines, recycling and process-group cleanup, but no
  operating-system security boundary.
- Execution is disabled by default. Trusted-local transformation previews require
  an explicit `--execution-backend trusted-local` opt-in, or
  `PYXSD_MCP_EXECUTION_BACKEND=trusted-local` when no CLI value is supplied.
  An explicit `disabled` value overrides either. Invalid values abort startup.
  Trusted-local previews run with the launching user's privileges, not in a
  sandbox. Do not use them for untrusted code, and there is no fallback to
  another execution mode.
- Arrow record preview and export run in clean workers whenever PyArrow is
  available (`pyxsd[mcp-all]` for the complete server install). Preview returns
  bounded rows; export retains digest-verified Parquet/dataset artifacts.
  Neither executes supplied Python transforms.
- No OCI/Docker backend or worker image is included.

Local workers use a bounded, role-validated JSON protocol. Python objects, pickle,
live `Document` instances and arbitrary host file handles never cross the protocol
boundary. Workers receive a content-verified temporary input capture and return a
bounded reply.

The server is also **offline by construction**. It never selects
`RemoteResourcePolicy` and has no network loader: every captured schema and
instance document is read from the session input set or the configured source
root. The semantic parse of XML and schema bytes uses `defusedxml` under
`forbid_dtd=True`, `forbid_entities=True`, and `forbid_external=True`, plus the
operator's XML byte, depth, and element limits. Subtree queries also run the
source index, a raw Expat pass configured with `XmlParserPolicy.strict()` that
applies the same declaration policy and limits without any external-resource
fetcher. A remote `schemaLocation`, include, or import is not fetched; the
reference fails rather than retrieving anything. Protecting a non-MCP
application is a separate decision documented in {doc}`xml-security`.

## Store and transport

The server retains artifacts at `<project-root>/.pyxsd/mcp`, or at the path
selected with `--state-dir`. Project root defaults to the launch working directory;
use `--project-root` when the host launches elsewhere. Persistent directories and
the SQLite index are created only when retaining the first artifact, not during
discovery or normal no-output use. Use `--no-persist` for temporary outputs removed
on orderly shutdown. Multiple local servers may share the same selected scope;
SQLite coordinates short transactions with a 5000 ms timeout (`state_busy` is
retryable). Network filesystem sharing is unsupported.

Session inputs and operation captures live in separate per-server temporary
scratch, outside the persistent store, and are removed afterward. They are not durable
workspace revisions. Retained artifacts remain until explicitly deleted with
`pyxsd_delete_artifact`.

Legacy `state.sqlite3` and `workspaces/` data in the selected directory is not
opened, migrated, or deleted. A new `store-v2` directory is used instead.
The old global default is not discovered or migrated. Stop old servers before
explicitly reusing it; mixed old/new versions sharing state are unsupported.
Startup never removes unindexed files, which might belong to a peer writer. For
offline cleanup, stop every server sharing a store before removing only that
dedicated MCP directory, losing its retained outputs. Projects normally gitignore
`.pyxsd/`; the server does not edit `.gitignore`.

The MCP transport is stdio only. There is no HTTP listener, OAuth/JWKS
verification, remote multi-client mode, or network file route. Do not expose the
stdio process through an untrusted relay.

## Local boundaries

Source roots and the store directory stay under operator control. Resource
limits are disabled by default; enable and tune them with `--resource-limits
enabled` plus `--limit FIELD=VALUE`, or the matching `PYXSD_MCP_*` environment
variables. Without limits, request bodies, queues, reports, artifacts, workers
and execution time are unbounded; that is a deliberate local-only tradeoff, not
a sandbox. Every server has independent inputs, scratch and worker subprocesses;
project scoping is organizational isolation, not an OS-user security boundary.
State is excluded from source capture, including discovered dependencies. This
is not a distributed or network-filesystem deployment.

For mutually untrusted code or tenants, use a separate operator-designed sandbox
with its own threat model. Local workers do not establish a public-hosting or
untrusted-code safety claim.

## Source roots

`--source-root /absolute/root` grants read-only access to one operator-selected
tree. Clients cannot mount arbitrary host directories. Create an empty workspace
and refer to paths relative to that root; operations capture referenced files and
dependencies. Those captures use the workspace ID, generation `0`, and per-file
digests for identity. Once a workspace has uploads, it uses only uploads: missing
files are never filled from the source tree.

State is excluded from captures, including dependency discovery. A source root
cannot lie inside the state directory. Uploaded files with similar names are
ordinary session inputs. Relative configured paths are frozen against the launch
working directory; there is no Git-root discovery or client-controlled scope
switching.

`corpus_glob` selection follows the same boundary. Enumeration is
descriptor-relative and deterministic, never follows file or directory symlinks,
prunes the excluded state tree before descent, and counts every visited entry
against the input-entry budget. A matched non-regular entry is an `unsafe_path`
fault. Selection names files without reading their bytes; the subsequent
verified capture copies and hashes them, retries once if source bytes change,
and fails rather than comparing a mixed manifest when changes persist. Matches
above the corpus budget fail with `quota_exceeded`; no silent prefix is taken.

## Store layout and recovery

The selected store contains `store-v2/index.sqlite3` and
`store-v2/artifacts/<random-id>/<filename>`. An explicit `--state-dir` overrides
the project default and shares artifacts with other processes selecting it.
For read-only projects, choose a writable private directory or `--no-persist`.
An unwritable default permits discovery but retaining an output reports a fault.

SQLite uses rollback journaling, short transactions, and no lifetime process
lock. Output files are staged and fsynced before publication. An open read may
finish after another process deletes an artifact; subsequent reads return
`not_found`. Each process owns temporary scratch outside the store and never
cleans a peer's scratch. Discovery, uploads, validation without output, and
operations against an absent store do not create persistent state.

Crashes may leave unlisted staging/orphan files. There is no online garbage
collection or adoption: stop every process sharing a store before resetting its
dedicated directory. Resetting it deletes retained outputs too. Legacy state
and the old user-global default are not migrated or discovered automatically;
stop old servers before explicitly reusing that path.
