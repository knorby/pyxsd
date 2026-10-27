"""MCP SDK server assembly and one owned services/shutdown boundary."""

from collections.abc import Callable
from pathlib import Path

import anyio
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings

from .. import __version__
from .artifacts import ArtifactStore
from .audit import AuditLogger
from .config import Settings
from .errors import ToolError
from .jobs import JobManager
from .previews import PreviewOperation, PreviewService
from .resources import register_resources
from .services.exploration import ExplorationService
from .snapshot_store import SnapshotStore
from .state import State
from .tools import register_tools
from .worker_protocol import Operation, WorkerReply
from .workers import WorkerPool
from .workspaces import Principal, WorkspaceStore, stdio_principal


class Services:
    """Own all coordinator state for one stdio server; close workers before SQLite."""

    def __init__(self, settings: Settings) -> None:
        if not settings.enabled_groups.issubset({"exploration", "workspaces", "execution"}):
            raise ToolError("invalid_argument", "Unknown MCP tool group")
        if settings.execution_backend == "isolated" or (
            settings.execution_backend == "trusted-local" and settings.transport != "stdio"
        ):
            raise ToolError("execution_unavailable", "No supported execution backend configured")
        self.settings = settings
        self.state = State(settings.state_dir)
        self.principal = stdio_principal()
        self.workspaces = WorkspaceStore(self.state, settings.limits)
        self.artifacts = ArtifactStore(self.workspaces)
        self.pool = WorkerPool(settings.limits)
        self.jobs = JobManager(self.workspaces, self)
        source_root: Callable[[Principal, str], Path] | None = None
        if settings.source_roots:

            def source_root(principal: Principal, workspace_id: str) -> Path:
                try:
                    return settings.source_roots[principal.owner_id]
                except KeyError as exc:
                    raise ToolError(
                        "capture_unavailable", "No source grant for this owner"
                    ) from exc

        self.snapshots = SnapshotStore(
            self.workspaces,
            source_root=source_root,
            discovery=self.pool,
        )
        self.exploration = ExplorationService(
            self.snapshots,
            self.pool,
            artifacts=self.artifacts,
            audit=AuditLogger(),
        )
        self.previews = PreviewService(
            self.snapshots,
            self.pool,
            self.artifacts,
            backend=settings.execution_backend,
            transport=settings.transport,
            jobs=self.jobs,
        )

    async def run(self, operation: Operation, *, deadline: float) -> WorkerReply:
        if isinstance(operation, PreviewOperation):
            return await self.previews.run(operation, deadline=deadline)
        return await self.pool.run(operation, deadline=deadline)

    def current_principal(self) -> Principal:
        if self.settings.transport == "stdio":
            return self.principal
        token = get_access_token()
        if token is not None and token.subject:
            return Principal(f"http:{token.subject}", frozenset(token.scopes))
        if self.settings.http is not None and self.settings.http.loopback_dev:
            return Principal(
                "loopback",
                frozenset(
                    {
                        "pyxsd:read",
                        "pyxsd:upload",
                        "pyxsd:execute",
                        "pyxsd:write",
                        "pyxsd:replace",
                        "pyxsd:write-invalid",
                    }
                ),
            )
        raise ToolError("forbidden", "Authentication is required")

    async def aclose(self) -> None:
        await self.jobs.aclose()
        await self.pool.aclose()
        self.state.close()

    async def __aenter__(self) -> "Services":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def create_server(
    settings: Settings,
    services: Services,
    *,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
) -> MCPServer:
    if settings is not services.settings:
        raise ToolError("invalid_argument", "Server and services settings differ")
    server = MCPServer(
        name="pyxsd",
        title="pyxsd XML Schema tools",
        instructions=(
            "Explore schemas, validate XML, follow exact diagnostic occurrences, "
            "and retain bounded immutable artifacts. "
            f"Execution backend: {settings.execution_backend}."
        ),
        version=__version__,
        token_verifier=token_verifier,
        auth=auth,
    )
    register_tools(server, services)
    register_resources(server, services)
    return server


async def _run(settings: Settings) -> None:
    services = Services(settings)
    try:
        server = create_server(settings, services)
        await server.run_stdio_async()
    finally:
        await services.aclose()


def run(settings: Settings) -> None:
    if settings.transport != "stdio":
        raise ToolError("unsupported_transport", "Use the HTTP application for streamable HTTP")
    anyio.run(_run, settings)
