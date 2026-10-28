# MCP worker deployment

This page describes the `pyxsd.mcp` worker-runtime boundary. It is not a claim
that a hosted MCP service is ready for untrusted production traffic. The local
runtime is deliberately **uncontained**; remote execution of user Python is not
supported. No OCI/Docker backend or worker image is included.

## Runtime choices

- `LocalRuntime` starts clean Python worker subprocesses. It provides bounded
  JSON IPC, per-schema serialization, deadlines, recycling and cancellation, but
  no operating-system security boundary. Do not use it to execute untrusted code.
- Execution is disabled by default at the application configuration layer.
  Trusted-local transformation previews are available only through explicit
  stdio opt-in; there is no fallback to another execution mode.

Local clean workers use a bounded, role-validated JSON protocol. Python objects,
pickle, live `Document` instances and arbitrary host file handles never cross
the protocol boundary.

## HTTP operation

`create_http_app()` shares the same services and domain operations as stdio, with
per-request principals derived from `AccessToken.subject` and scopes. The
`pyxsd-mcp` CLI exposes the same application through Uvicorn. Configure the
issuer, audience, fixed JWKS URL, allowed hosts/origins, and private state root;
do not put those values in tool arguments. Startup performs the configured JWKS
preflight and starts the MCP session manager, then closes workers and state at
shutdown.

Use `ConfiguredTokenVerifier` for the built-in fixed-issuer JWT support or inject
an SDK `TokenVerifier` for an embedding-specific identity system. Supported
algorithms are restricted to `RS256`, `ES256`, and `EdDSA`; symmetric/shared-key
modes are not accepted. Key refresh is bounded and happens only after the cache
TTL or an unknown key ID. Redirected JWKS responses, empty key sets, invalid
signatures, wrong issuer/audience/algorithm, expired tokens, and missing subjects
are refused.

Protected `PUT /files/{workspace}/{path}` and `GET /artifacts/{id}` routes share
the same principal and ownership checks as MCP tools. The request body limit is
2 MiB, ranges are exact, and every download includes its digest. Trusted-local
execution is rejected over HTTP; remote user-code execution is not supported.

## Remote deployment boundaries

Terminate TLS at a reviewed reverse proxy and use the HTTP authentication and
scope rules from the MCP design. Keep source roots and state directories outside
client control. Bound request bodies, queues, reports, artifacts and execution
time at the coordinator and at any operator-provided worker boundary.

For mutually untrusted tenants, use a stronger operator-provided runtime or
dedicated VM boundary and an explicit threat model; local workers do not
establish a public-hosting safety claim. The initial topology is one coordinator
with its own private state and workers, not a distributed/shared-filesystem
deployment.
