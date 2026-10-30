"""Documented launch paths work from an installed distribution, not only source fixtures."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")
pytest.importorskip("mcp")

import anyio
from mcp.client.stdio import stdio_client

from mcp import ClientSession, StdioServerParameters

ROOT = Path(__file__).resolve().parents[2]


def test_documented_module_launch_reports_version_and_help():
    version = subprocess.run(
        [sys.executable, "-m", "pyxsd.mcp", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert version.returncode == 0, version.stderr
    assert re.fullmatch(r"\d+\.\d+\.\d+\S*", version.stdout.strip())
    help_text = subprocess.run(
        [sys.executable, "-m", "pyxsd.mcp", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert help_text.returncode == 0, help_text.stderr
    assert "--state-dir" in help_text.stdout
    assert "trusted-local" in help_text.stdout
    assert "isolated" not in help_text.stdout


def test_documented_console_script_launch():
    script = Path(sys.executable).with_name("pyxsd-mcp")
    assert script.is_file()
    completed = subprocess.run(
        [str(script), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--state-dir" in completed.stdout
    assert "trusted-local" in completed.stdout


@pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx is not installed")
def test_documented_uvx_launch():
    completed = subprocess.run(
        [shutil.which("uvx"), "--from", f"{ROOT}[mcp]", "pyxsd-mcp", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--state-dir" in completed.stdout
    assert "trusted-local" in completed.stdout


def test_uvx_is_primary_and_direct_install_remains_documented():
    guide = (ROOT / "docs/mcp.md").read_text(encoding="utf-8")
    install = guide.split("## Install and launch", 1)[1].split("##", 1)[0]
    assert install.index("uvx --from 'pyxsd[mcp]' pyxsd-mcp") < install.index(
        "pip install 'pyxsd[mcp]'"
    )
    assert "--state-dir" in install
    assert "--no-persist" in install
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "uvx --from 'pyxsd[mcp]' pyxsd-mcp" in readme
    skill = (ROOT / "src/pyxsd/skills/pyxsd-mcp/SKILL.md").read_text(encoding="utf-8")
    assert "uvx --from 'pyxsd[mcp]' pyxsd-mcp" in skill


def test_documented_stdio_workflow_runs_against_the_installed_entry_point(tmp_path):
    async def scenario():
        state = tmp_path / "state"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pyxsd.mcp", "--state-dir", str(state)],
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as client,
        ):
            with anyio.fail_after(30):
                initialized = await client.initialize()
                assert initialized.capabilities.tools is not None
                created = await client.call_tool(
                    "pyxsd_manage_workspace",
                    {"action": "create"},
                )
                workspace = created.structured_content["data"]["id"]
                await client.call_tool(
                    "pyxsd_put_files",
                    {
                        "workspace_id": workspace,
                        "files": [
                            {
                                "path": "order.xsd",
                                "text": (
                                    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                                    '<xs:element name="order" type="xs:int"/></xs:schema>'
                                ),
                            },
                            {"path": "order.xml", "text": "<order>42</order>"},
                        ],
                    },
                )
                validated = await client.call_tool(
                    "pyxsd_validate_document",
                    {
                        "workspace_id": workspace,
                        "document": {"path": "order.xml"},
                        "schema_ref": {"source": {"path": "order.xsd"}},
                    },
                )
                assert validated.structured_content["data"]["valid"] is True
        assert not state.exists()

    anyio.run(scenario)
