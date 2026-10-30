"""Explicit startup settings and positive, operator-controlled budgets."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from .contracts import PositiveBudget, WireModel


def default_state_dir(project_root: Path) -> Path:
    return project_root / ".pyxsd/mcp"


class Limits(WireModel):
    xml_file_bytes: PositiveBudget = 64 * 2**20
    schema_file_bytes: PositiveBudget = 16 * 2**20
    schema_closure_files: PositiveBudget = 128
    schema_closure_bytes: PositiveBudget = 64 * 2**20
    xml_depth: PositiveBudget = 256
    xml_elements: PositiveBudget = 1_000_000
    inline_bytes: PositiveBudget = 2**20
    request_bytes: PositiveBudget = 2 * 2**20
    workspace_bytes: PositiveBudget = 512 * 2**20
    workspaces: PositiveBudget = 32
    input_entries: PositiveBudget = 4096
    cache_entries: PositiveBudget = 8
    workers: PositiveBudget = 4
    worker_queue: PositiveBudget = 16
    compile_query_seconds: PositiveBudget = 30
    transform_seconds: PositiveBudget = 60
    batch_seconds: PositiveBudget = 300
    transform_source_bytes: PositiveBudget = 64 * 2**10
    pipeline_steps: PositiveBudget = 32
    result_bytes: PositiveBudget = 24 * 2**10
    page_items: PositiveBudget = 50
    preview_rows: PositiveBudget = 20
    collected_issues: PositiveBudget = 10_000
    message_bytes: PositiveBudget = 4 * 2**10
    output_bytes: PositiveBudget = 128 * 2**20
    cache_idle_seconds: PositiveBudget = 900
    worker_operations: PositiveBudget = 100
    corpus_files: PositiveBudget = 100
    corpus_max_files: PositiveBudget = 1000
    corpus_file_seconds: PositiveBudget = 10

    @model_validator(mode="after")
    def validate_corpus_limit(self) -> Self:
        if self.corpus_files > self.corpus_max_files:
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
