"""Bounded transform recipes; normalization never executes code or imports names."""

import ast
import json
from typing import Annotated, Literal

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from .config import Limits
from .contracts import WireModel
from .errors import ToolError

TransformName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class PythonTransformSpec(WireModel):
    kind: Literal["python"] = "python"
    source: Annotated[str, Field(min_length=1)]


class PipelineStep(WireModel):
    name: TransformName
    args: tuple[JsonValue, ...] = ()
    kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    checkpoint: bool = False


class PipelineTransformSpec(WireModel):
    kind: Literal["pipeline"] = "pipeline"
    steps: Annotated[tuple[PipelineStep, ...], Field(min_length=1, max_length=32)]
    definitions: dict[TransformName, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def referenced_definitions(self) -> "PipelineTransformSpec":
        if self.definitions.keys() - {step.name for step in self.steps}:
            raise ValueError("Pipeline definitions must be referenced by a step")
        return self


TransformSpec = Annotated[PythonTransformSpec | PipelineTransformSpec, Field(discriminator="kind")]
_SPEC: TypeAdapter[PythonTransformSpec | PipelineTransformSpec] = TypeAdapter(TransformSpec)


def _source_budget(sources: list[str], limits: Limits) -> None:
    if any(not source for source in sources) or sum(
        len(source.encode("utf-8")) for source in sources
    ) > min(64 * 2**10, limits.transform_source_bytes):
        raise ToolError("invalid_transform", "Transform source exceeds its UTF-8 byte budget")


def normalize_transform(
    value: object, limits: Limits
) -> PythonTransformSpec | PipelineTransformSpec:
    """Validate one data-only recipe under hard and operator-configured limits."""
    try:
        if isinstance(value, (PythonTransformSpec, PipelineTransformSpec)):
            value = value.model_dump(mode="json")
        encoded = json.dumps(value, allow_nan=False)
        spec = _SPEC.validate_json(encoded)
        sources = (
            [spec.source]
            if isinstance(spec, PythonTransformSpec)
            else list(spec.definitions.values())
        )
        _source_budget(sources, limits)
        if isinstance(spec, PipelineTransformSpec) and len(spec.steps) > limits.pipeline_steps:
            raise ToolError("invalid_transform", "Pipeline exceeds its step budget")
        return spec
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ToolError("invalid_transform", "Transform specification is invalid") from exc


def parse_pipeline(expression: str, limits: Limits) -> PipelineTransformSpec:
    """Parse CLI-style calls without eval, name resolution, or cwd imports."""
    try:
        _source_budget([expression], limits)
        node = ast.parse(expression, mode="eval").body
        calls = [node]
        if isinstance(node, ast.Compare):
            if not all(isinstance(operator, ast.Gt) for operator in node.ops):
                raise ValueError("Only ordered pipeline separators are allowed")
            calls = [node.left, *node.comparators]
        steps = []
        for call in calls:
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                raise ValueError("Pipeline steps must be named calls")
            kwargs = {}
            for keyword in call.keywords:
                if keyword.arg is None or keyword.arg in kwargs:
                    raise ValueError("Expanded or duplicate keywords are not allowed")
                kwargs[keyword.arg] = ast.literal_eval(keyword.value)
            steps.append(
                {
                    "name": call.func.id,
                    "args": [ast.literal_eval(argument) for argument in call.args],
                    "kwargs": kwargs,
                }
            )
        result = normalize_transform({"kind": "pipeline", "steps": steps}, limits)
        assert isinstance(result, PipelineTransformSpec)
        return result
    except (ValueError, TypeError, SyntaxError, RecursionError) as exc:
        raise ToolError("invalid_transform", "Pipeline syntax is invalid") from exc


def resolve_sources(
    spec: PipelineTransformSpec, registry: dict[str, str], limits: Limits
) -> dict[str, str]:
    """Resolve only explicit recipe definitions and operator-registered sources."""
    sources = {}
    for step in spec.steps:
        source = spec.definitions.get(step.name, registry.get(step.name))
        if source is None:
            raise ToolError("unknown_transform", "Pipeline name is not registered")
        sources[step.name] = source
    _source_budget(list(sources.values()), limits)
    return sources
