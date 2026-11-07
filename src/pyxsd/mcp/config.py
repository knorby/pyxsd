"""Explicit startup settings and positive, operator-controlled budgets."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from .contracts import PositiveBudget, WireModel

ResourceBudget = PositiveBudget | None


def default_state_dir(project_root: Path) -> Path:
    return project_root / ".pyxsd/mcp"


class Limits(WireModel):
    enforced: bool = False
    xml_file_bytes: ResourceBudget = 64 * 2**20
    schema_file_bytes: ResourceBudget = 16 * 2**20
    schema_closure_files: ResourceBudget = 128
    schema_closure_bytes: ResourceBudget = 64 * 2**20
    xml_depth: ResourceBudget = 256
    xml_elements: ResourceBudget = 1_000_000
    inline_bytes: ResourceBudget = 2**20
    request_bytes: ResourceBudget = 2 * 2**20
    workspace_bytes: ResourceBudget = 512 * 2**20
    workspaces: ResourceBudget = 32
    input_entries: ResourceBudget = 4096
    cache_entries: ResourceBudget = 8
    workers: ResourceBudget = 4
    worker_queue: ResourceBudget = 16
    compile_query_seconds: ResourceBudget = 30
    transform_seconds: ResourceBudget = 60
    batch_seconds: ResourceBudget = 300
    transform_source_bytes: ResourceBudget = 64 * 2**10
    pipeline_steps: ResourceBudget = 32
    result_bytes: ResourceBudget = 24 * 2**10
    page_items: ResourceBudget = 50
    preview_rows: ResourceBudget = 20
    collected_issues: ResourceBudget = 10_000
    message_bytes: ResourceBudget = 4 * 2**10
    output_bytes: ResourceBudget = 128 * 2**20
    cache_idle_seconds: ResourceBudget = 900
    worker_operations: ResourceBudget = 100
    corpus_files: ResourceBudget = 100
    corpus_max_files: ResourceBudget = 1000
    corpus_file_seconds: ResourceBudget = 10
    projection_search_depth: ResourceBudget = 8
    projection_searched_declarations: ResourceBudget = 100
    projection_candidates: ResourceBudget = 10
    path_bytes: ResourceBudget = 4096
    path_segments: ResourceBudget = 128
    path_segment_bytes: ResourceBudget = 255
    io_chunk_bytes: ResourceBudget = 65536

    @model_validator(mode="after")
    def validate_resource_limits(self) -> Self:
        resource_fields = (name for name in type(self).model_fields if name != "enforced")
        if not self.enforced:
            for name in resource_fields:
                object.__setattr__(self, name, None)
            return self
        for name in resource_fields:
            if getattr(self, name) is None:
                raise ValueError(f"{name} must be set when resource limits are enforced")
        if (
            self.corpus_files is not None
            and self.corpus_max_files is not None
            and self.corpus_files > self.corpus_max_files
        ):
            raise ValueError("corpus_files must not exceed corpus_max_files")
        return self


class Settings(WireModel):
    project_root: Path = Field(default_factory=Path.cwd)
    state_dir: Path = Path(".pyxsd/mcp")
    persist: bool = True
    execution_backend: Literal["disabled", "trusted-local"] = "disabled"
    limits: Limits = Field(default_factory=Limits)
    source_root: Path | None = None
    enabled_groups: set[str] = Field(default_factory=lambda: {"exploration", "workspaces"})
    pydantic: bool = True
    arrow: Literal["auto", "enabled", "disabled"] = "auto"

    @model_validator(mode="after")
    def resolve_scope(self) -> Self:
        project = self.project_root.absolute()
        if not project.is_dir():
            raise ValueError("project_root must be an existing directory")
        state = (
            self.state_dir.absolute()
            if "state_dir" in self.model_fields_set
            else default_state_dir(project)
        )
        source = None if self.source_root is None else self.source_root.absolute()
        if self.persist and source is not None and source.resolve().is_relative_to(state.resolve()):
            raise ValueError("state_dir must not contain the source_root")
        object.__setattr__(self, "project_root", project)
        object.__setattr__(self, "state_dir", state)
        object.__setattr__(self, "source_root", source)
        return self
