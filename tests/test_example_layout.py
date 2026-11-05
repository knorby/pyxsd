"""Pin the examples' source-family ownership, including their servers."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

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


def _assert_local_example_import(family: str, name: str) -> None:
    if not name.startswith("examples."):
        return
    parts = name.split(".")
    assert parts[1] == family, f"{family}/server.py imports another family: {name}"


def _assert_local_imports(family: str, tree: ast.AST) -> None:
    package = ["examples", family]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _assert_local_example_import(family, alias.name)
        elif isinstance(node, ast.ImportFrom):
            parts = package[: len(package) - (node.level - 1)] if node.level else []
            if node.module:
                parts.extend(node.module.split("."))
            if parts:
                _assert_local_example_import(family, ".".join(parts))
            if (node.level and not node.module) or (not node.level and node.module == "examples"):
                for alias in node.names:
                    _assert_local_example_import(family, ".".join([*parts, alias.name]))


@pytest.mark.parametrize(
    "source",
    [
        "from ..bpmn.common import sources",
        "from examples import bpmn",
        "from examples import server",
    ],
)
def test_layout_guard_rejects_cross_family_imports(source):
    tree = ast.parse(source)
    with pytest.raises(AssertionError, match="another family"):
        _assert_local_imports("citygml", tree)


def test_family_servers_do_not_import_other_families():
    for family in FAMILIES:
        module = EXAMPLES / family / "server.py"
        _assert_local_imports(family, ast.parse(module.read_text(encoding="utf-8")))


def test_central_showcase_server_is_removed():
    assert not (EXAMPLES / "server/app.py").exists()
    assert not (EXAMPLES / "server/README.md").exists()


@pytest.mark.parametrize("family", FAMILIES)
def test_family_readme_local_links_resolve(family):
    readme = EXAMPLES / family / "README.md"
    for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", readme.read_text(encoding="utf-8")):
        relative = target.split("#", 1)[0]
        if not relative or "://" in relative or relative.startswith("mailto:"):
            continue
        destination = (readme.parent / relative).resolve()
        assert destination.exists(), f"{readme}: broken local link {target!r}"


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
