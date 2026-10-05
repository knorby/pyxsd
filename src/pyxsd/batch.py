"""Sequential parsing of explicitly identified local XML files.

Records are frozen, but a contained Document remains mutable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pyxsd.exceptions import PyXSDError

if TYPE_CHECKING:
    from pyxsd.document import Document
    from pyxsd.validation import ValidationIssue


@dataclass(frozen=True)
class DocumentSource:
    """Caller label and local path, anchored when constructed."""

    id: str
    path: str | os.PathLike[str]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str):
            raise TypeError("source id must be a nonempty string")
        if not self.id:
            raise ValueError("source id must be a nonempty string")
        if not isinstance(self.path, (str, os.PathLike)):
            raise TypeError("source path must be a text path")
        if not isinstance(os.fspath(self.path), str):
            raise TypeError("source path must be a text path")
        object.__setattr__(self, "path", Path(self.path).absolute())


@dataclass(frozen=True)
class InputFailure:
    """Expected input failure, without a live exception or traceback."""

    kind: Literal["io", "malformed_xml"]
    message: str


@dataclass(frozen=True)
class ParseOutcome:
    """One input's result with a snapshot of its validation issues."""

    source: DocumentSource
    status: Literal["valid", "invalid", "input_error"]
    document: Document | None
    issues: tuple[ValidationIssue, ...]
    error: InputFailure | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))


class BatchParseError(PyXSDError):
    """First failed input in raise mode; inspect :attr:`outcome`."""

    def __init__(self, outcome: ParseOutcome) -> None:
        super().__init__(f"source {outcome.source.id!r}: {outcome.status}")
        self.outcome = outcome
