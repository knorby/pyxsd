# MCP worker deployment

This page describes the `pyxsd.mcp` worker-runtime boundary. The server is a
**local stdio process** that runs beside the MCP client. It is not a hosted
multi-tenant service, and no network listener is included.

## Runtime choices

- Clean workers are Python subprocesses with bounded JSON IPC, per-schema
  serialization, deadlines, recycling and process-group cleanup, but no
  operating-system security boundary.
- Execution is disabled by default. Trusted-local transformation previews require
  an explicit `--execution-backend trusted-local` opt-in and run with the
  launching user's privileges, not in a sandbox. Do not use them for untrusted
  code, and there is no fallback to another execution mode.
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

Source roots and the store directory stay under operator control. Bound request
bodies, queues, reports, artifacts and execution time through `Settings.limits`.
Each server has independent inputs, scratch and worker subprocesses; project
scoping is organizational isolation, not an OS-user security boundary. State is
excluded from source capture, including discovered dependencies. This is not a
distributed or network-filesystem deployment.

For mutually untrusted code or tenants, use a separate operator-designed sandbox
with its own threat model. Local workers do not establish a public-hosting or
untrusted-code safety claim.
