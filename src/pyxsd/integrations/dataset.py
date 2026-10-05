"""Dependency-free results and errors for local manifested Parquet datasets."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import IntegrationError

__all__ = ["DatasetExportError", "DatasetResult"]


@dataclass(frozen=True)
class DatasetResult:
    """Published dataset summary; partial/failed results are not full successes."""

    destination: Path
    status: Literal["complete", "partial", "failed"]
    inputs: int
    succeeded: int
    failed: int
    rows: int
    manifest: Path


class DatasetExportError(IntegrationError):
    """Fatal export failure with stage/source context and a chained cause."""

    def __init__(self, message: str, *, stage: str, source_id: str | None = None) -> None:
        self.source_id = source_id
        self.stage = stage
        super().__init__(f"dataset {stage} (source {source_id!r}): {message}")
