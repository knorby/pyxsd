"""Launch the optional stdio MCP server without weakening base-install imports."""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path


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
        choices=("disabled", "trusted-local"),
        default="disabled",
        help="Trusted-local runs code with your privileges; stdio only, not a sandbox",
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
        execution_backend=arguments.execution_backend,
        enabled_groups=(
            {"exploration", "workspaces", "execution"}
            if arguments.execution_backend != "disabled"
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
