"""The local MCP server exposes a stdio-only surface."""

import pytest

pytest.importorskip("mcp")
pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_server import connected

HTTP_OPTIONS = (
    ("--transport", "streamable-http"),
    ("--issuer-url", "https://issuer.example"),
    ("--audience", "pyxsd"),
    ("--jwks-url", "https://issuer.example/jwks"),
    ("--http-host", "127.0.0.1"),
    ("--http-port", "8000"),
    ("--allowed-host", "localhost"),
    ("--allowed-origin", "https://app.example"),
    ("--loopback-dev", None),
)


def test_parser_accepts_local_arguments_only(tmp_path):
    from pyxsd.mcp.__main__ import _parser

    parsed = _parser().parse_args(["--state-dir", str(tmp_path), "--execution-backend", "disabled"])
    assert parsed.state_dir == tmp_path
    for option, value in HTTP_OPTIONS:
        argv = [option] if value is None else [option, value]
        with pytest.raises(SystemExit):
            _parser().parse_args(argv)


def test_settings_have_no_remote_transport_fields(tmp_path):
    from pydantic import ValidationError
    from pyxsd.mcp.config import Settings

    settings = Settings(state_dir=tmp_path)
    assert set(settings.model_dump()) == {
        "project_root",
        "state_dir",
        "persist",
        "execution_backend",
        "limits",
        "source_root",
        "enabled_groups",
        "pydantic",
        "arrow",
    }
    with pytest.raises(ValidationError):
        Settings(state_dir=tmp_path, transport="stdio")
    with pytest.raises(ValidationError):
        Settings(state_dir=tmp_path, http=None)


def test_stdio_client_lists_the_ordinary_tools(tmp_path):
    async def scenario():
        async with connected(tmp_path) as (client, _services):
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert "pyxsd_capabilities" in names
            assert "pyxsd_validate_document" in names

    anyio.run(scenario)
