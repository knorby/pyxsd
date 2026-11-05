"""Pin the examples' source-family ownership, including their servers."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
FAMILIES = ("bpmn", "citygml", "musicxml", "opendrive", "xrechnung")
GUIDE = ROOT / "docs/rendering-examples.md"
INDEX = EXAMPLES / "README.md"


def test_each_source_family_owns_a_server():
    for family in FAMILIES:
        module = EXAMPLES / family / "server.py"
        assert module.is_file(), f"{family} must own its server example"
        tree = ast.parse(module.read_text(encoding="utf-8"))
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "app" for target in node.targets)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "create_app"
        ]
        assert assignments, f"{family}/server.py must expose app = create_app()"


def test_family_servers_do_not_import_other_families():
    for family in FAMILIES:
        module = EXAMPLES / family / "server.py"
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for name in imported:
            if not name.startswith("examples."):
                continue
            parts = name.split(".")
            assert parts[1] == family, f"{family}/server.py imports another family: {name}"
            assert parts[1] != "server", f"{family}/server.py imports the central server: {name}"


def test_central_showcase_server_is_removed():
    assert not (EXAMPLES / "server/app.py").exists()
    assert not (EXAMPLES / "server/README.md").exists()


def test_docs_describe_family_servers_not_domain_dispatch():
    guide = GUIDE.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    for family in FAMILIES:
        assert f"examples.{family}.server:app" in guide
        assert f"examples.{family}.server:app" in index
    for title, text in ((str(GUIDE), guide), (str(INDEX), index)):
        for forbidden in (
            "examples.server",
            "examples/server",
            "/render/{domain}",
            "/validate/{domain}",
        ):
            assert forbidden not in text, f"{title} still contains {forbidden!r}"
