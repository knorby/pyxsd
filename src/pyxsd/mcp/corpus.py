"""Portable, bounded glob selection for corpus inputs."""

import re
from collections.abc import Iterable

from .config import Limits
from .errors import ToolError


def portable_parts(value: str, limits: Limits | None = None) -> tuple[str, ...]:
    """Validate relative POSIX spelling and return its path segments."""
    path_bytes = None if limits is None else limits.path_bytes
    path_segments = None if limits is None else limits.path_segments
    segment_bytes = None if limits is None else limits.path_segment_bytes
    try:
        encoded = len(value.encode("utf-8")) if isinstance(value, str) else 0
        parts = value.split("/") if isinstance(value, str) else ()
        valid = (
            isinstance(value, str)
            and bool(value)
            and (path_bytes is None or encoded <= path_bytes)
            and (path_segments is None or len(parts) <= path_segments)
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            and not any(char in value for char in ("\\", ":"))
            and all(
                part not in ("", ".", "..")
                and (segment_bytes is None or len(part.encode("utf-8")) <= segment_bytes)
                for part in parts
            )
        )
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise ToolError("invalid_path", "Expected an unambiguous relative POSIX path")
    return tuple(value.split("/"))


def _character_class(segment: str, start: int) -> tuple[str, int]:
    end = segment.find("]", start + 1)
    if end < 0:
        raise ToolError("invalid_argument", "Malformed corpus glob character class")
    body = segment[start + 1 : end]
    negated = body.startswith("!")
    if negated:
        body = body[1:]
    if not body:
        raise ToolError("invalid_argument", "Malformed corpus glob character class")
    return ("[^" if negated else "[") + body + "]", end + 1


def _segment_expression(segment: str, *, final: bool) -> str:
    if segment == "**":
        return "[^/]+(?:/[^/]+)*" if final else "(?:[^/]+/)*"
    if "**" in segment:
        raise ToolError("invalid_argument", "Recursive glob must be a complete segment")
    expression: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            expression.append("[^/]*")
        elif char == "?":
            expression.append("[^/]")
        elif char == "[":
            class_expression, index = _character_class(segment, index)
            expression.append(class_expression)
            continue
        else:
            expression.append(re.escape(char))
        index += 1
    if not final:
        expression.append("/")
    return "".join(expression)


class CorpusGlob:
    """One validated corpus pattern shared by manifests and source trees."""

    def __init__(self, pattern: str, limits: Limits | None = None) -> None:
        parts = portable_parts(pattern, limits)
        expressions = [
            _segment_expression(part, final=index == len(parts) - 1)
            for index, part in enumerate(parts)
        ]
        try:
            self._expression = re.compile("".join(expressions))
        except re.error as exc:
            raise ToolError("invalid_argument", "Malformed corpus glob pattern") from exc

    def matches(self, path: str) -> bool:
        return self._expression.fullmatch(path) is not None


def _positive_budget(value: object, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ToolError("invalid_argument", f"{label} must be a positive integer")
    return value


def select_manifest_paths(
    pattern: str,
    paths: Iterable[str],
    *,
    max_matches: int | None,
    limits: Limits | None = None,
) -> tuple[str, ...]:
    """Select lexical manifest paths without reading or capturing their bytes."""
    limit = _positive_budget(max_matches, "Match budget")
    candidates = tuple(paths)
    for path in candidates:
        portable_parts(path, limits)
    glob = CorpusGlob(pattern, limits)
    selected: list[str] = []
    for path in sorted(set(candidates)):
        if glob.matches(path):
            selected.append(path)
            if limit is not None and len(selected) > limit:
                raise ToolError("quota_exceeded", "Corpus glob exceeds match budget")
    return tuple(selected)
