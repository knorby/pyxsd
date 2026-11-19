"""Dependency-free specifications for named contextual scalar columns."""

from dataclasses import dataclass
from typing import Literal

from . import IntegrationError

__all__ = ["FieldSource", "field_source_metadata"]


@dataclass(frozen=True, kw_only=True)
class FieldSource:
    """Select a scalar or attribute by expanded XML names, relative to a row."""

    scope: Literal["record", "ancestor", "root"] = "record"
    path: tuple[str, ...] = ()
    attribute: str | None = None
    levels: int = 0
    datatype: tuple[str, str] | None = None
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        if self.scope not in ("record", "ancestor", "root"):
            raise IntegrationError("source scope must be record, ancestor, or root")
        if type(self.levels) is not int or (
            self.levels <= 0 if self.scope == "ancestor" else self.levels != 0
        ):
            raise IntegrationError(
                "ancestor levels must be positive; other scopes require levels=0"
            )
        if not isinstance(self.path, tuple) or not all(
            isinstance(name, str) and name and name != "$" and not name.startswith("@")
            for name in self.path
        ):
            raise IntegrationError("source path must be a tuple of nonempty expanded element names")
        if self.attribute is not None and (
            not isinstance(self.attribute, str)
            or not self.attribute
            or self.attribute.startswith("@")
        ):
            raise IntegrationError("source attribute must be a nonempty expanded name without @")
        if self.datatype is not None:
            if (
                not isinstance(self.datatype, tuple)
                or len(self.datatype) != 2
                or not all(isinstance(part, str) for part in self.datatype)
            ):
                raise IntegrationError(
                    "source datatype must be a (library URI, type name) tuple of strings"
                )
            if not self.datatype[1]:
                raise IntegrationError("source datatype type name must be nonempty")
        if self.candidate_id is not None and (
            not isinstance(self.candidate_id, str) or not self.candidate_id
        ):
            raise IntegrationError("source candidate_id must be a nonempty string")
        if self.datatype is not None and self.candidate_id is not None:
            raise IntegrationError("source datatype and candidate_id are mutually exclusive")


def field_source_metadata(source: FieldSource) -> dict[str, object]:
    """Serialize one source without adding unset RELAX NG selectors.

    The leading key order matches the historical ``dataclasses.asdict`` output,
    so existing XSD ``pyxsd:source`` metadata stays byte-compatible.
    """
    if not isinstance(source, FieldSource):
        raise TypeError("field_source_metadata requires a FieldSource")
    metadata: dict[str, object] = {
        "scope": source.scope,
        "path": source.path,
        "attribute": source.attribute,
        "levels": source.levels,
    }
    if source.datatype is not None:
        metadata["datatype"] = source.datatype
    if source.candidate_id is not None:
        metadata["candidate_id"] = source.candidate_id
    return metadata
