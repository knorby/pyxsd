# MCP worker deployment

This page describes the `pyxsd.mcp` worker-runtime boundary. It is not a claim
that a hosted MCP service is ready for untrusted production traffic. The local
runtime is deliberately **uncontained**; OCI containment must pass the Linux
runtime checks and the real containment suite before remote use.

## Runtime choices

- `LocalRuntime` starts clean Python worker subprocesses. It provides bounded
  JSON IPC, per-schema serialization, deadlines, recycling and cancellation, but
  no operating-system security boundary. Do not use it to execute untrusted code.
- `OCIRuntime` starts workers in pinned Docker-compatible OCI images. Each worker
  receives one read-only snapshot mount. Changing snapshots or terminating the
  worker forces removal of the old container.
- Execution is disabled by default at the application configuration layer.
  Remote Python execution requires the isolated backend; there is no fallback to
  local execution when OCI startup or preflight fails.

Both runtimes use the same bounded, role-validated JSON protocol. Python objects,
pickle, live `Document` instances and arbitrary host file handles never cross
the protocol boundary.

## OCI security configuration

`OCICommandBuilder` constructs argument arrays without a shell. Every worker has:

- a full image ID or repository digest, never a mutable tag;
- numeric non-root user `65532:65532`, a read-only root filesystem and read-only
  workspace mount;
- `network=none`, all capabilities dropped, `no-new-privileges` and Docker's
  default seccomp profile (not `unconfined`);
- CPU, memory/swap and PID limits, with at most 64 PIDs;
- bounded, `noexec`, `nosuid`, `nodev` tmpfs filesystems for scratch and output;
- an init process, bounded stop timeout and coordinator-forced `docker rm -f`
  cleanup, including startup failure and cancellation paths.

Only existing directories beneath operator-provided managed roots can be
mounted. Symlinks, missing paths and paths outside those roots are refused.
Callers cannot mount the Docker socket, devices, home-directory credentials or
arbitrary host paths. Do not weaken these settings to make a probe pass.

## Build and pin a worker image

Build the wheel first, then build from the repository root:

```bash
uv build --wheel
docker build -f containers/mcp-worker/Dockerfile -t pyxsd/mcp-worker:candidate .
```

The Dockerfile's base image is pinned by digest and installs the wheel plus only
the worker's runtime dependencies. It does not install the coordinator, SDK,
Arrow, a shell-based entry point or user packages. Custom dependencies belong in
an operator-built image with separately reviewed, pinned inputs.

For a local image, use its exact image ID:

```bash
export PYXSD_MCP_OCI_IMAGE="$(
  docker image inspect pyxsd/mcp-worker:candidate --format '{{.Id}}'
)"
```

For a registry deployment, build and push through your controlled registry
workflow, then use the resulting `repository@sha256:<digest>` instead. Record
that digest in deployment configuration; do not resolve `latest` at startup.

## Host preflight and containment gate

`OCIRuntime.preflight()` requires a Linux OCI server, an active seccomp profile
that is not unconfined, and an inspectable exact image. Failures are explicit
`runtime_unavailable` errors. No second attempt uses weaker settings.

Docker applies its default seccomp profile when no override is supplied. Do not
pass `--security-opt seccomp=default`: Docker interprets `default` as a profile
filename, not as a request for the built-in profile.

Unit tests validate deterministic arguments and failure handling only:

```bash
uv run --extra mcp pytest tests/mcp/test_oci_runtime.py -q
```

The real gate requires Linux, a running daemon and the pinned image above:

```bash
PYXSD_RUN_MCP_OCI=1 \
PYXSD_MCP_OCI_IMAGE="$PYXSD_MCP_OCI_IMAGE" \
uv run --extra mcp pytest tests/mcp/test_oci_containment.py -q
```

That suite checks actual network denial, inaccessible host/sibling data, PID,
memory and output exhaustion, descendant cleanup after timeout, and the real
worker protocol inside the container. A skipped test, a fake runner, a macOS
development run, or the presence of Docker flags is **not** containment evidence.

On 2026-10-27, all four tests passed in an aarch64 Linux runner inside Docker's
Linux VM, using worker image
`sha256:622215ab8c08c8f1c4699260554778c60fa1b3251e1484a94373e7ac42a16255`.
This is evidence for that image/runtime combination, not a public-hosting safety
claim. Isolated transformation execution and candidate capture remain separate
implementation gates.

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
execution is rejected over HTTP; isolated execution additionally requires the
containment gate above.

## Remote deployment boundaries

Terminate TLS at a reviewed reverse proxy and use the HTTP authentication and
scope rules from the MCP design. Keep source roots and state directories outside
client control. Bound request bodies, queues, reports, artifacts and execution
time at the coordinator as well as in the container.

Standard containers share the host kernel. For mutually untrusted tenants, use a
stronger operator-provided runtime or dedicated VM boundary and an explicit
threat model; these settings alone do not establish a public-hosting safety
claim. The initial topology is one coordinator with its own private state and
workers, not a distributed/shared-filesystem deployment.
