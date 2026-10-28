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
from .previews import PreviewService
from .publication import PublicationService
from .resources import register_resources
from .services.exploration import ExplorationService
from .services.models import ModelService
from .services.records import RecordService
from .skill_extension import SkillsExtension
from .snapshot_store import SnapshotStore
from .state import State
from .tools import register_tools
from .workers import WorkerPool
from .workspaces import Principal, WorkspaceStore, stdio_principal


class Services:
    """Own all coordinator state for one stdio server; close workers before SQLite."""

    def __init__(self, settings: Settings) -> None:
        if not settings.enabled_groups.issubset({"exploration", "workspaces", "execution"}):
            raise ToolError("invalid_argument", "Unknown MCP tool group")
        self.arrow_available = self._probe_arrow(settings.arrow)
        self.settings = settings
        self.state = State(settings.state_dir, limits=settings.limits)
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
        self.models = ModelService(self.snapshots, self.pool, artifacts=self.artifacts)
        self.records = RecordService(
            self.snapshots,
            self.pool,
            artifacts=self.artifacts,
            arrow=self.arrow_available,
        )
        from .services.comparison import ComparisonService

        self.comparison = ComparisonService(self.snapshots, self.pool)
        self.previews = PreviewService(
            self.snapshots,
            self.pool,
            self.artifacts,
            backend=settings.execution_backend,
        )
        self.publication = PublicationService(self.previews)

    @staticmethod
    def _probe_arrow(mode: str) -> bool:
        """Distinguish absent, broken and healthy installs at startup, never silently."""
        try:
            import pyarrow  # noqa: F401
        except ModuleNotFoundError:
            if mode == "enabled":
                raise ToolError(
                    "arrow_required", "Arrow exports are enabled but pyarrow is not installed"
                ) from None
            return False
        except ImportError as exc:
            raise ToolError(
                "arrow_unavailable",
                f"Installed pyarrow is broken: {str(exc)[:200]}",
            ) from exc
        return mode != "disabled"

    def current_principal(self) -> Principal:
        return self.principal

    async def aclose(self) -> None:
        await self.pool.aclose()
        self.state.close()

    async def __aenter__(self) -> "Services":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def create_server(
    settings: Settings,
    services: Services,
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
        extensions=[SkillsExtension()],
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
