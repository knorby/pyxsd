"""Offline publication checks; no test authenticates or publishes metadata."""

import copy
import importlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.check_mcp_registry import check_local, check_pypi, check_registry
from scripts.check_mcp_registry import main as check_main

from pyxsd import __version__

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli_main():
    module = importlib.import_module("pyxsd.__main__")
    del sys.modules["pyxsd.__main__"]
    return module.main


@pytest.mark.parametrize("arguments", [["--version"], ["--help"]])
def test_registry_cli_alias(arguments):
    result = subprocess.run(
        [sys.executable, "-m", "pyxsd", "mcp", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    if arguments == ["--version"]:
        assert result.stdout.strip() == __version__
    else:
        assert "--execution-backend" in result.stdout


def test_registry_manifest_tracks_package_and_optional_dependencies():
    assert (ROOT / "server.json").is_file(), "Registry manifest is missing"
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    (package,) = manifest["packages"]
    assert package["identifier"] == "pyxsd"
    assert package["version"] == __version__
    assert package["runtimeArguments"] == [
        {"type": "named", "name": "--with", "value": f"pyxsd[mcp]=={__version__}"}
    ]
    assert package["packageArguments"] == [{"type": "positional", "value": "mcp"}]


def test_console_dispatch_preserves_xml_and_mcp_exit_status(monkeypatch, capsys, cli_main):
    monkeypatch.setattr(sys, "argv", ["pyxsd", "mcp", "--version"])
    assert cli_main() == 0
    assert capsys.readouterr().out.strip() == __version__
    with pytest.raises(SystemExit) as module_exit:
        runpy.run_module("pyxsd.__main__", run_name="__main__")
    assert module_exit.value.code == 0
    assert capsys.readouterr().out.strip() == __version__
    with pytest.raises(SystemExit) as error:
        cli_main(["--version"])
    assert error.value.code == 0
    assert capsys.readouterr().out.strip() == f"PyXSD {__version__}"


def test_console_dispatch_preserves_xml_processing(tmp_path, cli_main):
    fixture = ROOT / "tests/fixtures/inventory"
    output = tmp_path / "parsed.xml"
    assert (
        cli_main(
            [
                "-i",
                str(fixture / "instance.xml"),
                "--schema",
                str(fixture / "schema.xsd"),
                "-k",
                "-p",
                str(output),
                "-o",
                str(tmp_path / "transformed.xml"),
            ]
        )
        is None
    )
    assert "<inventory" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("mutation", ["valid", "version", "identifier", "extras", "marker"])
def test_local_publication_checks(tmp_path, mutation, monkeypatch):
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if mutation == "version":
        manifest["packages"][0]["version"] = "0.0.0"
    elif mutation == "identifier":
        manifest["packages"][0]["identifier"] = "pyxsd[mcp]"
    elif mutation == "extras":
        manifest["packages"][0]["runtimeArguments"] = []
    elif mutation == "marker":
        readme = "No verification marker"
    (tmp_path / "server.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    if mutation == "valid":
        assert check_local(tmp_path)["name"] == "io.github.knorby/pyxsd"
        monkeypatch.setattr(sys, "argv", ["check_mcp_registry.py"])
        check_main()
        runpy.run_path(str(ROOT / "scripts/check_mcp_registry.py"), run_name="__main__")
    else:
        with pytest.raises(ValueError):
            check_local(tmp_path)


@pytest.mark.parametrize("kind", ["pypi", "registry"])
@pytest.mark.parametrize("mutation", ["valid", "version", "identity", "launch", "package"])
def test_publication_response_checks(tmp_path, kind, mutation, monkeypatch):
    assert (ROOT / "scripts/check_mcp_registry.py").is_file(), "Publication checker is missing"
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    if kind == "pypi":
        data = {
            "info": {
                "name": "pyxsd",
                "version": __version__,
                "description": "<!-- mcp-name: io.github.knorby/pyxsd -->",
                "requires_dist": ['mcp>=2.3; extra == "mcp"'],
            }
        }
        key = {
            "version": "version",
            "identity": "description",
            "launch": "requires_dist",
            "package": "name",
        }
        if mutation != "valid":
            data["info"][key[mutation]] = [] if mutation == "launch" else "incorrect"
    else:
        data = {"server": copy.deepcopy(manifest)}
        if mutation == "version":
            data["server"]["version"] = "0.0.0"
        elif mutation == "identity":
            data["server"]["name"] = "io.github.someone/other"
        elif mutation == "launch":
            data["server"]["packages"][0]["runtimeArguments"] = []
        elif mutation == "package":
            data["server"]["packages"][0]["identifier"] = "other"
    checker = check_pypi if kind == "pypi" else check_registry
    if mutation == "valid":
        checker(manifest, data)
    else:
        with pytest.raises(ValueError):
            checker(manifest, data)
    response = tmp_path / "response.json"
    response.write_text(json.dumps(data), encoding="utf-8")
    if mutation == "valid":
        monkeypatch.setattr(sys, "argv", ["check_mcp_registry.py", f"--{kind}", str(response)])
        check_main()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_mcp_registry.py"), f"--{kind}", str(response)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert (result.returncode == 0) is (mutation == "valid"), result.stderr


@pytest.mark.parametrize(
    "dependency", ["unrelated>=1", 'mcp-helper>=1; extra == "mcp"', 'mcp>=2.3; extra == "other"']
)
def test_pypi_check_requires_the_actual_mcp_dependency(dependency):
    manifest = check_local(ROOT)
    data = {
        "info": {
            "name": "pyxsd",
            "version": __version__,
            "description": "<!-- mcp-name: io.github.knorby/pyxsd -->",
            "requires_dist": [dependency],
        }
    }
    with pytest.raises(ValueError, match="MCP extra"):
        check_pypi(manifest, data)


@pytest.mark.parametrize("marker", ["extra == 'mcp'", 'extra == "mcp"'])
def test_pypi_check_accepts_standard_metadata_marker_quotes(marker):
    manifest = check_local(ROOT)
    data = {
        "info": {
            "name": "pyxsd",
            "version": __version__,
            "description": "<!-- mcp-name: io.github.knorby/pyxsd -->",
            "requires_dist": [f"mcp<3,>=2.3; {marker}"],
        }
    }
    check_pypi(manifest, data)
