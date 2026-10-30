"""MCP SDK server assembly and one owned services/shutdown boundary."""

import anyio
from mcp.server import MCPServer

from .. import __version__
from .audit import AuditLogger
from .config import Settings
from .errors import ToolError
from .inputs import SessionInputs
from .output_store import OutputStore
from .previews import PreviewService
from .resources import register_resources
from .services.comparison import ComparisonService
from .services.exploration import ExplorationService
from .services.models import ModelService
from .services.records import RecordService
from .skill_extension import SkillsExtension
from .storage import Storage
from .tools import register_tools
from .workers import WorkerPool


class Services:
    """Own one local store, session inputs, worker pool and output index."""

    def __init__(self, settings: Settings) -> None:
        if not settings.enabled_groups.issubset({"exploration", "workspaces", "execution"}):
            raise ToolError("invalid_argument", "Unknown MCP tool group")
        self.arrow_available = self._probe_arrow(settings.arrow)
        self.settings = settings
        self.storage = Storage(settings.state_dir, persist=settings.persist)
        try:
            self.outputs = OutputStore(self.storage, settings.limits)
            self.pool = WorkerPool(settings.limits)
            self.inputs = SessionInputs(
                self.storage.scratch,
                settings.limits,
                source_root=settings.source_root,
                discovery=self.pool,
                excluded_source_root=settings.state_dir if settings.persist else None,
            )
            audit = AuditLogger()
            self.exploration = ExplorationService(
                self.inputs, self.pool, outputs=self.outputs, audit=audit
            )
            self.models = ModelService(self.inputs, self.pool, outputs=self.outputs)
            self.records = RecordService(
                self.inputs,
                self.pool,
                outputs=self.outputs,
                arrow=self.arrow_available,
            )
            self.comparison = ComparisonService(self.inputs, self.pool)
            self.previews = PreviewService(
                self.inputs,
                self.pool,
                self.outputs,
                backend=settings.execution_backend,
            )
        except BaseException:
            self.storage.close()
            raise

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

    async def aclose(self) -> None:
        await self.pool.aclose()
        self.outputs.close()
        self.storage.close()

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
            "Explore schemas, validate XML, follow exact diagnostic occurrences, and retain "
            "bounded local output artifacts. Inputs and previews last for this server session. "
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
