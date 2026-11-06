"""Check release identity and downloaded metadata without authenticating or publishing."""

import argparse
import json
import re
from pathlib import Path

from pyxsd import __version__


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_local(root):
    manifest = json.loads((root / "server.json").read_text(encoding="utf-8"))
    (package,) = manifest["packages"]
    require(manifest["version"] == package["version"] == __version__, "Registry version drift")
    require(package["identifier"] == "pyxsd", "PyPI identifier must not contain extras")
    require(
        package["runtimeArguments"]
        == [{"type": "named", "name": "--with", "value": f"pyxsd[mcp]=={__version__}"}],
        "Registry launcher must install the matching MCP extra",
    )
    require(
        f"<!-- mcp-name: {manifest['name']} -->"
        in (root / "README.md").read_text(encoding="utf-8"),
        "README must carry the registry ownership marker before building",
    )
    return manifest


def check_pypi(manifest, response):
    info = response["info"]
    require(info["name"] == "pyxsd", "Unexpected PyPI package")
    require(info["version"] == manifest["version"], "Unexpected PyPI version")
    require(
        f"<!-- mcp-name: {manifest['name']} -->" in info["description"],
        "Published PyPI README is missing the registry ownership marker",
    )
    require(
        any(
            re.fullmatch(r"""mcp(?=[<>=!~;\s]|$).*;\s*extra\s*==\s*(['"])mcp\1\s*""", item)
            for item in info["requires_dist"]
        ),
        "Published package is missing the MCP extra",
    )


def check_registry(manifest, response):
    server = response["server"]
    for field in ("name", "version", "packages"):
        require(server[field] == manifest[field], f"Published registry {field} differs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    responses = parser.add_mutually_exclusive_group()
    responses.add_argument("--pypi", type=Path)
    responses.add_argument("--registry", type=Path)
    arguments = parser.parse_args()
    manifest = check_local(Path(__file__).resolve().parents[1])
    if arguments.pypi:
        check_pypi(manifest, json.loads(arguments.pypi.read_text(encoding="utf-8")))
    elif arguments.registry:
        check_registry(manifest, json.loads(arguments.registry.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
