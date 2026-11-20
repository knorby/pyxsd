"""Explicit startup settings and positive, operator-controlled budgets."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pyxsd.relaxng.limits import RelaxNGLimits

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
    batch_issue_items: ResourceBudget = 10
    artifact_read_bytes: ResourceBudget = 8 * 2**10
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
    # RELAX NG work budgets exhaust independently; the capture and diagnostic
    # budgets bound retained records instead of raising.
    relaxng_pattern_nodes: ResourceBudget = 1_000_000
    relaxng_derivative_states: ResourceBudget = 1_000_000
    relaxng_memo_entries: ResourceBudget = 1_000_000
    relaxng_evidence_states: ResourceBudget = 1_000_000
    relaxng_evidence_edges: ResourceBudget = 1_000_000
    relaxng_scalar_witnesses: ResourceBudget = 1_000_000
    relaxng_binding_summaries: ResourceBudget = 1_000_000
    relaxng_capture_entries: ResourceBudget = 10_000
    relaxng_projection_routes: ResourceBudget = 10_000
    relaxng_diagnostics: ResourceBudget = 10_000

    @field_validator("*", mode="after")
    @classmethod
    def normalize_disabled_limits(
        cls, value: ResourceBudget | bool, info: ValidationInfo
    ) -> ResourceBudget | bool:
        if info.field_name != "enforced" and not info.data.get("enforced", False):
            return None
        return value

    @model_validator(mode="after")
    def validate_resource_limits(self) -> Self:
        resource_fields = (name for name in type(self).model_fields if name != "enforced")
        if not self.enforced:
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


def relaxng_limits_for_mcp(limits: Limits) -> RelaxNGLimits:
    """Map the server's fixed budgets onto the RELAX NG compiler declaration.

    Every RELAX NG unit is mapped independently. Disabled server limits become
    ``None`` (unlimited) through :class:`Limits` normalization, and the
    capture and diagnostic fields keep their truncation semantics rather than
    becoming work budgets.
    """
    return RelaxNGLimits(
        max_pattern_nodes=limits.relaxng_pattern_nodes,
        max_derivative_states=limits.relaxng_derivative_states,
        max_memo_entries=limits.relaxng_memo_entries,
        max_evidence_states=limits.relaxng_evidence_states,
        max_evidence_edges=limits.relaxng_evidence_edges,
        max_scalar_witnesses=limits.relaxng_scalar_witnesses,
        max_binding_summaries=limits.relaxng_binding_summaries,
        max_capture_entries=limits.relaxng_capture_entries,
        max_projection_routes=limits.relaxng_projection_routes,
        max_diagnostics=limits.relaxng_diagnostics,
    )


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
