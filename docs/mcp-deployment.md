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

Local workers use a bounded, role-validated JSON protocol. Python objects,
pickle, live `Document` instances and arbitrary host file handles never cross
the protocol boundary.

## State and transport

Keep the state directory private and owned by the launching user. It contains
workspaces, immutable revisions, artifacts and restart metadata; it is never
selected by a tool argument. The server holds a process lock so a second
coordinator cannot reset live leases, and it closes workers and state on
shutdown.

The MCP transport is stdio only. There is no HTTP listener, OAuth/JWKS
verification, request-derived principal, remote multi-client mode, or network
file route. Workspace access uses the local process identity. Do not expose the
stdio process through an untrusted relay.

## Local boundaries

Source roots and the state directory stay under operator control. Bound request
bodies, queues, reports, artifacts and execution time through `Settings.limits`.
The initial topology is one coordinator with its own private state and worker
subprocesses, not a distributed or shared-filesystem deployment.

For mutually untrusted code or tenants, use a separate operator-designed sandbox
with its own threat model. Local workers do not establish a public-hosting or
untrusted-code safety claim.
