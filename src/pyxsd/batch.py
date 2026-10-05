"""Sequential parsing of explicitly identified local XML files.

Records are frozen, but a contained Document remains mutable.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from xml.etree import ElementTree as ET

from pyxsd.exceptions import PyXSDError, ValidationError

if TYPE_CHECKING:
    from pyxsd.document import Document
    from pyxsd.schema import Schema
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


def _parse_source(
    schema: Schema, source: DocumentSource, errors: Literal["raise", "report"]
) -> ParseOutcome:
    try:
        document = schema.parse(source.path)
    except PyXSDError as exc:
        # Only the parser's explicit native input causes qualify. Binding
        # defects and unclassified PyXSDErrors must never become input errors.
        cause = exc.__cause__
        if isinstance(cause, OSError):
            kind: Literal["io", "malformed_xml"] = "io"
        elif isinstance(cause, ET.ParseError):
            kind = "malformed_xml"
        else:
            raise
        outcome = ParseOutcome(source, "input_error", None, (), InputFailure(kind, str(cause)))
        if errors == "raise":
            raise BatchParseError(outcome) from exc
        return outcome

    outcome = ParseOutcome(
        source,
        "valid" if document.is_valid else "invalid",
        document,
        tuple(document.report.issues),
        None,
    )
    if errors == "raise" and not document.is_valid:
        try:
            document.require_valid()
        except ValidationError as exc:
            raise BatchParseError(outcome) from exc
    return outcome


def iter_parse(
    schema: Schema,
    sources: Iterable[DocumentSource],
    *,
    errors: Literal["raise", "report"] = "raise",
) -> Iterator[ParseOutcome]:
    """Parse in caller order, lazily, without retaining earlier outcomes.

    Configuration and schema checks run on first iteration. Expected input
    failures stop with BatchParseError by default; report mode yields them.
    Do not parse concurrently or reentrantly on this Schema.
    """
    if errors not in ("raise", "report"):
        raise ValueError("errors must be 'raise' or 'report'")
    schema.require_valid()
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, DocumentSource):
            raise TypeError("sources must contain DocumentSource records")
        if source.id in seen:
            raise ValueError(f"duplicate source id: {source.id!r}")
        seen.add(source.id)
        yield _parse_source(schema, source, errors)
