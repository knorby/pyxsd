"""Transform recipes are bounded data, not implicit imports or expressions."""

import json

import pytest

pytest.importorskip("pydantic")

from pyxsd.mcp.config import Limits
from pyxsd.mcp.errors import ToolError


def test_python_source_is_retained_without_execution(tmp_path):
    from pyxsd.mcp.transforms import normalize_transform

    marker = tmp_path / "executed"
    source = f"open({str(marker)!r}, 'w').write('bad')\ndef transform(document, context): pass"
    result = normalize_transform({"kind": "python", "source": source}, Limits())
    assert result.source == source
    assert not marker.exists()
    assert result.kind == "python"


@pytest.mark.parametrize("size,allowed", [(65536, True), (65537, False)])
def test_source_budget_counts_utf8_bytes(size, allowed):
    from pyxsd.mcp.transforms import normalize_transform

    source = "#" + "x" * (size - 3) + "é"
    if allowed:
        assert (
            len(normalize_transform({"kind": "python", "source": source}, Limits()).source.encode())
            == size
        )
    else:
        with pytest.raises(ToolError, match="invalid_transform"):
            normalize_transform({"kind": "python", "source": source}, Limits())


def test_pipeline_preserves_json_arguments_checkpoints_and_explicit_code():
    from pyxsd.mcp.transforms import normalize_transform

    result = normalize_transform(
        {
            "kind": "pipeline",
            "steps": [
                {
                    "name": "Repair",
                    "args": [1, "x"],
                    "kwargs": {"enabled": True},
                    "checkpoint": True,
                },
                {"name": "Finish"},
            ],
            "definitions": {"Repair": "def transform(document, context, *args, **kwargs): pass"},
        },
        Limits(),
    )
    assert result.steps[0].args == (1, "x")
    assert result.steps[0].kwargs == {"enabled": True}
    assert result.steps[0].checkpoint is True
    assert result.steps[1].checkpoint is False
    assert "Repair" in result.definitions
    assert json.loads(result.model_dump_json())["steps"][1]["name"] == "Finish"


def test_cli_recipe_is_parsed_as_literals_without_resolving_imports(tmp_path, monkeypatch):
    from pyxsd.mcp.transforms import parse_pipeline

    monkeypatch.chdir(tmp_path)
    (tmp_path / "repair.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")
    result = parse_pipeline('Repair(2, label="x>y") > Finish()', Limits())
    assert [step.name for step in result.steps] == ["Repair", "Finish"]
    assert result.steps[0].kwargs == {"label": "x>y"}


@pytest.mark.parametrize(
    "expression",
    [
        "os.system('x')",
        "Repair(open('secret'))",
        "Repair(**{})",
        "Repair() >",
        "Repair(x=1, x=2)",
        "Repair() < Finish()",
    ],
)
def test_recipe_refuses_executable_or_ambiguous_syntax(expression):
    from pyxsd.mcp.transforms import parse_pipeline

    with pytest.raises(ToolError, match="invalid_transform"):
        parse_pipeline(expression, Limits())


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "python", "source": ""},
        {"kind": "python", "source": "x", "unexpected": 1},
        {"kind": "pipeline", "steps": []},
        {"kind": "pipeline", "steps": [{"name": "x"}] * 33},
        {"kind": "pipeline", "steps": [{"name": "pkg.Name"}]},
        {"kind": "pipeline", "steps": [{"name": "x", "args": [float("nan")]}]},
        {"kind": "shell", "source": "x"},
        {"kind": "pipeline", "steps": [{"name": "X"}], "definitions": {"Unused": "pass"}},
    ],
)
def test_invalid_specs_are_typed_errors(value):
    from pyxsd.mcp.transforms import normalize_transform

    with pytest.raises(ToolError, match="invalid_transform"):
        normalize_transform(value, Limits())


def test_operator_budgets_and_explicit_registry_resolution():
    from pyxsd.mcp.transforms import normalize_transform, resolve_sources

    with pytest.raises(ToolError, match="invalid_transform"):
        normalize_transform({"kind": "python", "source": "abcd"}, Limits(transform_source_bytes=3))
    with pytest.raises(ToolError, match="invalid_transform"):
        normalize_transform(
            {"kind": "pipeline", "steps": [{"name": "X"}] * 2}, Limits(pipeline_steps=1)
        )
    spec = normalize_transform({"kind": "pipeline", "steps": [{"name": "X"}]}, Limits())
    with pytest.raises(ToolError, match="unknown_transform"):
        resolve_sources(spec, {}, Limits())
    source = "def transform(document, context): return document"
    assert resolve_sources(spec, {"X": source}, Limits()) == {"X": source}
    explicit = spec.model_copy(update={"definitions": {"X": source + "\n"}})
    assert resolve_sources(explicit, {"X": source}, Limits())["X"] == source + "\n"
    with pytest.raises(ToolError, match="invalid_transform"):
        resolve_sources(spec, {"X": "x" * 65537}, Limits())
