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

## Store and transport

The server creates a private store automatically at
`~/.local/state/pyxsd/mcp`, or at the path selected with `--state-dir`. The store
contains a small SQLite artifact index and retained output files. Use
`--no-persist` for a temporary store removed on orderly shutdown. One server
process owns a persistent store through `coordinator.lock`; do not run two
coordinators against the same directory.

Session inputs and operation captures live under `store-v2/scratch/` for the
lifetime of the operation and are removed afterward. They are not durable
workspace revisions. Retained artifacts remain until explicitly deleted with
`pyxsd_delete_artifact`.

Legacy `state.sqlite3` and `workspaces/` data in the selected directory is not
opened, migrated, or deleted. A new `store-v2` directory is used instead.

The MCP transport is stdio only. There is no HTTP listener, OAuth/JWKS
verification, remote multi-client mode, or network file route. Do not expose the
stdio process through an untrusted relay.

## Local boundaries

Source roots and the store directory stay under operator control. Bound request
bodies, queues, reports, artifacts and execution time through `Settings.limits`.
The initial topology is one coordinator with its own private store and worker
subprocesses, not a distributed or shared-filesystem deployment.

For mutually untrusted code or tenants, use a separate operator-designed sandbox
with its own threat model. Local workers do not establish a public-hosting or
untrusted-code safety claim.
