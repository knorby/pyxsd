"""Launch the optional stdio MCP server without weakening base-install imports."""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyxsd-mcp", description="pyxsd MCP stdio server")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/pyxsd/mcp")
    parser.add_argument("--source-root", type=Path, help="Operator-granted read-only source root")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--issuer-url")
    parser.add_argument("--audience")
    parser.add_argument("--jwks-url")
    parser.add_argument("--http-host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=8000)
    parser.add_argument("--allowed-host", action="append")
    parser.add_argument("--allowed-origin", action="append")
    parser.add_argument("--loopback-dev", action="store_true")
    parser.add_argument("--version", action="store_true", help="Print pyxsd version and exit")
    return parser


def _run_http(arguments: argparse.Namespace) -> int:
    import uvicorn

    from .auth import ConfiguredTokenVerifier
    from .config import HTTPSettings, Settings
    from .errors import ToolError
    from .http import create_http_app
    from .server import Services

    if not arguments.loopback_dev and not all(
        (arguments.issuer_url, arguments.audience, arguments.jwks_url)
    ):
        raise ToolError("auth_required", "HTTP requires issuer, audience and JWKS URL")
    issuer = arguments.issuer_url or "http://localhost:8000"
    audience = arguments.audience or issuer
    jwks_url = arguments.jwks_url or issuer
    http = HTTPSettings(
        issuer_url=issuer,
        audience=audience,
        jwks_url=jwks_url,
        allowed_hosts=set(arguments.allowed_host or {arguments.http_host + ":*"}),
        allowed_origins=set(arguments.allowed_origin or []),
        loopback_dev=arguments.loopback_dev,
    )
    settings = Settings(
        state_dir=arguments.state_dir,
        transport="streamable-http",
        source_roots={} if arguments.source_root is None else {"stdio": arguments.source_root},
        http=http,
    )
    verifier = (
        None
        if arguments.loopback_dev
        else ConfiguredTokenVerifier(issuer=issuer, audience=audience, jwks_url=jwks_url)
    )
    app = create_http_app(settings, Services(settings), verifier)
    uvicorn.Server(
        uvicorn.Config(app, host=arguments.http_host, port=arguments.http_port, log_level="warning")
    ).run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.version:
        from pyxsd import __version__

        print(__version__)
        return 0
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
        state_dir=arguments.state_dir,
        source_roots={} if arguments.source_root is None else {"stdio": arguments.source_root},
    )
    try:
        if arguments.transport == "streamable-http":
            return _run_http(arguments)
        run(settings)
    except ToolError as exc:
        print(f"{exc.fault.code}: {exc.fault.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
