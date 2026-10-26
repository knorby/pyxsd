"""Explicit startup settings and positive, operator-controlled budgets."""

from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import PositiveBudget, WireModel


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
    artifact_bytes_per_workspace: PositiveBudget = 512 * 2**20
    workspaces: PositiveBudget = 32
    managed_bytes: PositiveBudget = 4 * 2**30
    cache_entries: PositiveBudget = 8
    worker_memory_bytes: PositiveBudget = 512 * 2**20
    workers: PositiveBudget = 4
    active_jobs_per_owner: PositiveBudget = 1
    queued_jobs: PositiveBudget = 16
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
    workspace_idle_seconds: PositiveBudget = 3600
    preview_seconds: PositiveBudget = 1800
    job_result_seconds: PositiveBudget = 1800
    diagnostic_seconds: PositiveBudget = 3600
    worker_operations: PositiveBudget = 100
    worker_pids: PositiveBudget = 64
    corpus_files: PositiveBudget = 100
    corpus_max_files: PositiveBudget = 1000


class Settings(WireModel):
    state_dir: Path
    transport: Literal["stdio", "streamable-http"] = "stdio"
    execution_backend: Literal["disabled", "trusted-local", "isolated"] = "disabled"
    limits: Limits = Field(default_factory=Limits)
