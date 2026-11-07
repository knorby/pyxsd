"""Launch the optional stdio MCP server without weakening base-install imports."""

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from .config import Limits

_EXECUTION_BACKENDS = ("disabled", "trusted-local")
_EXECUTION_BACKEND_ENV = "PYXSD_MCP_EXECUTION_BACKEND"
_RESOURCE_LIMITS_ENV = "PYXSD_MCP_RESOURCE_LIMITS"
_RESOURCE_LIMIT_PREFIX = "PYXSD_MCP_"
_RESOURCE_LIMIT_TOGGLES = ("disabled", "enabled")


def _resolve_execution_backend(
    cli_value: str | None, environment: Mapping[str, str]
) -> Literal["disabled", "trusted-local"]:
    """Resolve the execution backend: explicit CLI value, then environment, then disabled."""
    value = cli_value
    if value is None:
        candidate = environment.get(_EXECUTION_BACKEND_ENV)
        if candidate is not None and candidate.strip():
            value = candidate.strip()
            if value not in _EXECUTION_BACKENDS:
                raise ValueError(
                    f"{_EXECUTION_BACKEND_ENV} must be one of: disabled, trusted-local"
                )
    if value is None:
        return "disabled"
    # argparse already restricts explicit CLI values and the environment was validated.
    return cast(Literal["disabled", "trusted-local"], value)


def _parse_limit_override(raw: str, fields: Sequence[str]) -> tuple[str, int]:
    field, separator, value = raw.partition("=")
    if not separator or field not in fields:
        raise ValueError(f"Unknown resource limit: {raw}")
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError(f"Resource limit {field} must be a positive decimal integer")
    return field, int(value)


def _resolve_resource_limits(
    cli_value: str | None,
    overrides: Sequence[str],
    environment: Mapping[str, str],
) -> "Limits":
    """Resolve the resource-limit toggle and per-field overrides."""
    from .config import Limits

    fields = tuple(name for name in Limits.model_fields if name != "enforced")
    value: str | None = cli_value
    candidate = environment.get(_RESOURCE_LIMITS_ENV)
    if candidate is not None and candidate.strip():
        environment_value = candidate.strip()
        if environment_value not in _RESOURCE_LIMIT_TOGGLES:
            raise ValueError(f"{_RESOURCE_LIMITS_ENV} must be one of: disabled, enabled")
        if value is None:
            value = environment_value
    if value is None:
        value = "disabled"
    if value not in _RESOURCE_LIMIT_TOGGLES:
        raise ValueError(f"{_RESOURCE_LIMITS_ENV} must be one of: disabled, enabled")

    selected: dict[str, int] = {}
    for raw in overrides:
        field, parsed = _parse_limit_override(raw, fields)
        selected[field] = parsed
    environment_overrides: dict[str, int] = {}
    for field in fields:
        raw_value = environment.get(f"{_RESOURCE_LIMIT_PREFIX}{field.upper()}")
        if raw_value is not None and raw_value.strip():
            environment_overrides[field] = _parse_limit_override(
                f"{field}={raw_value.strip()}", fields
            )[1]
    for field, parsed in environment_overrides.items():
        selected.setdefault(field, parsed)

    if value == "disabled":
        if selected:
            raise ValueError("Resource limit overrides require --resource-limits enabled")
        return Limits()
    return Limits(enforced=True, **selected)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyxsd-mcp", description="pyxsd MCP stdio server")
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="Persistent store path (default: <project-root>/.pyxsd/mcp; created on first output)",
    )
    parser.add_argument(
        "--no-persist",
        dest="persist",
        action="store_false",
        help="Use a temporary store removed when the server exits",
    )
    parser.add_argument("--project-root", type=Path, help="Project directory (default: launch cwd)")
    parser.add_argument("--source-root", type=Path, help="Operator-granted read-only source root")
    parser.add_argument(
        "--execution-backend",
        choices=_EXECUTION_BACKENDS,
        default=None,
        help=(
            "Trusted-local runs code with your privileges; stdio only, not a sandbox "
            f"(env {_EXECUTION_BACKEND_ENV}; default disabled)"
        ),
    )
    parser.add_argument(
        "--resource-limits",
        choices=_RESOURCE_LIMIT_TOGGLES,
        default=None,
        help=(
            "Enforce resource budgets; disabled by default so local workloads are not "
            f"artificially capped (env {_RESOURCE_LIMITS_ENV})"
        ),
    )
    parser.add_argument(
        "--limit",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="Override one enabled resource budget (repeatable; requires --resource-limits enabled)",
    )
    parser.add_argument("--version", action="store_true", help="Print pyxsd version and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.version:
        from pyxsd import __version__

        print(__version__)
        return 0
    if not arguments.persist and arguments.state_dir is not None:
        parser.error("--state-dir cannot be combined with --no-persist")
    try:
        execution_backend = _resolve_execution_backend(
            arguments.execution_backend, dict(os.environ)
        )
        resource_limits = _resolve_resource_limits(
            arguments.resource_limits, arguments.limit, dict(os.environ)
        )
    except ValueError as exc:
        parser.error(str(exc))
    try:
        from .config import Settings
        from .errors import ToolError
        from .server import run
    except ModuleNotFoundError as exc:
        if exc.name in {"mcp", "pydantic", "anyio", "defusedxml"}:
            print(
                "MCP support requires the optional dependencies: pip install 'pyxsd[mcp]'",
                file=sys.stderr,
            )
            return 2
        raise
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    settings = Settings(
        **({"state_dir": arguments.state_dir} if arguments.state_dir is not None else {}),
        **({"project_root": arguments.project_root} if arguments.project_root is not None else {}),
        persist=arguments.persist,
        source_root=arguments.source_root,
        execution_backend=execution_backend,
        limits=resource_limits,
        enabled_groups=(
            {"exploration", "workspaces", "execution"}
            if execution_backend != "disabled"
            else {"exploration", "workspaces"}
        ),
    )
    try:
        run(settings)
    except ToolError as exc:
        print(f"{exc.fault.code}: {exc.fault.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
