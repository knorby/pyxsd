"""Documented launch paths work from an installed distribution, not only source fixtures."""

import json
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
    assert "https://pyxsd.knorby.com/mcp.html" in readme
    skill = (ROOT / "src/pyxsd/skills/pyxsd-mcp/SKILL.md").read_text(encoding="utf-8")
    assert "uvx --from 'pyxsd[mcp]' pyxsd-mcp" in skill


def test_complete_mcp_install_and_execution_environment_are_documented():
    guide = (ROOT / "docs/mcp.md").read_text(encoding="utf-8")
    install = guide.split("## Install and launch", 1)[1].split("##", 1)[0]
    assert "pyxsd[mcp-all]" in install
    assert "PYXSD_MCP_EXECUTION_BACKEND" in guide
    assert "PYXSD_MCP_EXECUTION_BACKEND=disabled" in guide
    quickstart = (ROOT / "docs/quickstart.md").read_text(encoding="utf-8")
    assert "pyxsd[mcp-all]" in quickstart
    integrations = (ROOT / "docs/integrations.md").read_text(encoding="utf-8")
    assert "pyxsd[mcp-all]" in integrations
    deployment = (ROOT / "docs/mcp-deployment.md").read_text(encoding="utf-8")
    assert "PYXSD_MCP_EXECUTION_BACKEND" in deployment


def test_new_mcp_workflow_guidance_is_documented():
    guide = (ROOT / "docs/mcp.md").read_text(encoding="utf-8")
    assert '"view": "diagnostics"' in guide
    assert "namespace_probe" in guide
    assert "projection_candidates" in guide
    assert "search_truncated" in guide
    assert "corpus_glob" in guide
    assert "corpus_selection" in guide
    skill = (ROOT / "src/pyxsd/skills/pyxsd-mcp/SKILL.md").read_text(encoding="utf-8")
    assert "pyxsd[mcp-all]" in skill
    assert "diagnostics" in skill
    assert "projection_candidates" in skill
    assert "corpus_glob" in skill
    assert "execution group + Arrow" not in skill
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "mcp-all" in changelog
    assert "PYXSD_MCP_EXECUTION_BACKEND" in changelog
    assert "corpus_glob" in changelog


def test_readme_setup_includes_library_mcp_and_skills():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Install\n" in readme
    install = readme.split("## Install", 1)[1].split("## ", 1)[0]
    assert "pip install pyxsd" in install
    assert "pip install 'pyxsd[mcp]'" in install
    assert "npx skills add knorby/pyxsd" in install
    config = json.loads(install.split("```json", 1)[1].split("```", 1)[0])
    assert config == {
        "mcpServers": {
            "pyxsd": {
                "command": "uvx",
                "args": ["--from", "pyxsd[mcp]", "pyxsd-mcp"],
            }
        }
    }


@pytest.mark.parametrize("launch", ["module", "registry"])
def test_documented_stdio_workflow_runs_against_the_installed_entry_point(tmp_path, launch):
    async def scenario():
        state = tmp_path / "state"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pyxsd.mcp", "--state-dir", str(state)],
        )
        if launch == "registry":
            import os

            if shutil.which("uvx") is None:
                pytest.skip("uvx is not installed")
            manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
            (package,) = manifest["packages"]
            directory = Path(os.environ.get("PYXSD_DIST_DIR", tmp_path / "wheels")).resolve()
            if "PYXSD_DIST_DIR" not in os.environ:
                subprocess.run(
                    [shutil.which("uv"), "build", "--wheel", "--out-dir", str(directory)],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            (wheel,) = directory.glob("pyxsd-*.whl")
            assert wheel.is_file()
            # Same argv as VS Code's McpServerManifestParser. A local index
            # supplies the not-yet-published wheel without overriding extras
            # or uvx's command-name and version parsing.
            arguments = []
            for argument in package["runtimeArguments"]:
                arguments.extend([argument["name"], argument["value"]])
            arguments.append(f"{package['identifier']}@{package['version']}")
            arguments.extend(argument["value"] for argument in package["packageArguments"])
            parameters = StdioServerParameters(
                command=shutil.which("uvx"),
                args=[*arguments, "--state-dir", str(state)],
                env={"UV_FIND_LINKS": str(directory), "UV_ISOLATED": "1"},
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


def test_guide_python_client_is_complete(tmp_path):
    guide = (ROOT / "docs/mcp.md").read_text(encoding="utf-8")
    blocks = guide.split("```python\n")[1:]
    assert blocks, "Supply a complete Python client interaction"
    script = tmp_path / "client.py"
    script.write_text(blocks[0].split("```", 1)[0], encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "valid: True"
    assert not (tmp_path / ".pyxsd").exists()
