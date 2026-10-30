"""Real SDK clients initialize and use the subprocess stdio server."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from mcp.client.stdio import stdio_client
from tests.mcp.test_exploration import VALID, XSD
from tests.mcp.test_server import create_workspace, structured

from mcp import ClientSession, StdioServerParameters
from pyxsd import __version__


def test_stdio_initialization_discovery_workflow_logs_and_teardown(tmp_path):
    async def scenario():
        logs = (tmp_path / "stderr.log").open("w+")
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pyxsd.mcp", "--state-dir", str(tmp_path / "state")],
            cwd=str(tmp_path),
        )
        async with (
            stdio_client(parameters, errlog=logs) as streams,
            ClientSession(*streams) as client,
        ):
            initialized = await client.initialize()
            assert initialized.server_info.name == "pyxsd"
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "pyxsd_validate_document" in names
            workspace = await create_workspace(client)
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {"path": "schema.xsd", "text": XSD},
                        {"path": "document.xml", "text": VALID},
                    ],
                },
            )
            result = await client.call_tool(
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "document.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            assert structured(result)["data"]["valid"] is True
        logs.seek(0)
        rendered = logs.read()
        logs.close()
        assert "Traceback" not in rendered
        assert "<root>" not in rendered
        assert "validate_document" in rendered

    anyio.run(scenario)


def test_main_missing_extra_is_actionable(monkeypatch, capsys):
    import builtins

    from pyxsd.mcp.__main__ import main

    for module in list(sys.modules):
        if module == "mcp" or module.startswith("mcp.") or module == "pyxsd.mcp.server":
            monkeypatch.delitem(sys.modules, module, raising=False)
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert main([]) == 2
    captured = capsys.readouterr()
    assert "pyxsd[mcp]" in captured.err
    assert "Traceback" not in captured.err


def test_main_run_configuration_errors_and_module_exit(tmp_path, monkeypatch, capsys):
    import runpy

    from pyxsd.mcp import __main__ as entrypoint
    from pyxsd.mcp import server
    from pyxsd.mcp.__main__ import main
    from pyxsd.mcp.errors import ToolError

    seen = []
    monkeypatch.setattr(server, "run", seen.append)
    assert (
        main(["--state-dir", str(tmp_path / "state"), "--source-root", str(tmp_path / "source")])
        == 0
    )
    assert seen[0].source_root == tmp_path / "source"

    def denied(_settings):
        raise ToolError("denied", "Denied")

    monkeypatch.setattr(server, "run", denied)
    assert main([]) == 2
    assert "denied" in capsys.readouterr().err

    def interrupted(_settings):
        raise KeyboardInterrupt

    monkeypatch.setattr(server, "run", interrupted)
    assert main([]) == 130

    monkeypatch.setattr(sys, "argv", ["pyxsd-mcp", "--version"])
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(Path(entrypoint.__file__), run_name="__main__")
    assert exited.value.code == 0


def test_main_reraises_unrelated_import_error(monkeypatch):
    from pyxsd.mcp.__main__ import main

    monkeypatch.setitem(sys.modules, "pyxsd.mcp.config", None)
    with pytest.raises(ModuleNotFoundError):
        main([])


def test_main_rejects_removed_http_options(tmp_path):
    from pyxsd.mcp.__main__ import main

    with pytest.raises(SystemExit):
        main(["--transport", "stdio", "--state-dir", str(tmp_path)])
    assert not (tmp_path / "state").exists()


def test_cli_execution_requires_explicit_stdio_opt_in(tmp_path, monkeypatch):
    from pyxsd.mcp import server
    from pyxsd.mcp.__main__ import main

    settings = []
    monkeypatch.setattr(server, "run", settings.append)
    assert main(["--state-dir", str(tmp_path)]) == 0
    assert settings[-1].execution_backend == "disabled"
    assert "execution" not in settings[-1].enabled_groups
    assert main(["--state-dir", str(tmp_path), "--execution-backend", "trusted-local"]) == 0
    assert settings[-1].execution_backend == "trusted-local"
    assert "execution" in settings[-1].enabled_groups


def test_main_version_does_not_require_sdk(capsys):
    from pyxsd.mcp.__main__ import main

    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out
