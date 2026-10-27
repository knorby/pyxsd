"""MCP SDK server assembly and one owned services/shutdown boundary."""

from collections.abc import Callable
from pathlib import Path

import anyio
from mcp.server import MCPServer

from .. import __version__
from .artifacts import ArtifactStore
from .audit import AuditLogger
from .config import Settings
from .errors import ToolError
from .resources import register_resources
from .services.exploration import ExplorationService
from .snapshot_store import SnapshotStore
from .state import State
from .tools import register_tools
from .workers import WorkerPool
from .workspaces import Principal, WorkspaceStore, stdio_principal


class Services:
    """Own all coordinator state for one stdio server; close workers before SQLite."""

    def __init__(self, settings: Settings) -> None:
        if settings.transport != "stdio":
            raise ToolError(
                "unsupported_transport", "HTTP transport is configured in a later milestone"
            )
        if not settings.enabled_groups.issubset({"exploration", "workspaces"}):
            raise ToolError("invalid_argument", "Unknown MCP tool group")
        if settings.execution_backend != "disabled":
            raise ToolError("execution_unavailable", "Execution is not enabled in this build")
        self.settings = settings
        self.state = State(settings.state_dir)
        self.principal = stdio_principal()
        self.workspaces = WorkspaceStore(self.state, settings.limits)
        self.artifacts = ArtifactStore(self.workspaces)
        self.pool = WorkerPool(settings.limits)
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

    async def aclose(self) -> None:
        await self.pool.aclose()
        self.state.close()

    async def __aenter__(self) -> "Services":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def create_server(settings: Settings, services: Services) -> MCPServer:
    if settings is not services.settings:
        raise ToolError("invalid_argument", "Server and services settings differ")
    server = MCPServer(
        name="pyxsd",
        title="pyxsd XML Schema tools",
        instructions=(
            "Explore schemas, validate XML, follow exact diagnostic occurrences, "
            "and retain bounded immutable artifacts. Execution is disabled."
        ),
        version=__version__,
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
    anyio.run(_run, settings)
