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


_Token = str | re.Pattern[str]
_Segment = tuple[_Token, ...] | None


def _segment_tokens(segment: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "[":
            class_expression, index = _character_class(segment, index)
            tokens.append(re.compile(class_expression))
            continue
        tokens.append(char)
        index += 1
    return tuple(tokens)


def _token_matches(token: _Token, char: str) -> bool:
    if token == "?":
        return True
    if isinstance(token, str):
        return token == char
    return token.fullmatch(char) is not None


def _segment_matches(tokens: tuple[_Token, ...], text: str) -> bool:
    positions = [index == 0 for index in range(len(text) + 1)]
    for token in tokens:
        following = [False] * len(positions)
        if token == "*":
            reachable = False
            for index, active in enumerate(positions):
                reachable = reachable or active
                following[index] = reachable
        else:
            for index, active in enumerate(positions[:-1]):
                if active and _token_matches(token, text[index]):
                    following[index + 1] = True
        positions = following
    return positions[-1]


class CorpusGlob:
    """One validated corpus pattern shared by manifests and source trees."""

    def __init__(self, pattern: str, limits: Limits | None = None) -> None:
        parts = portable_parts(pattern, limits)
        segments: list[_Segment] = []
        try:
            for part in parts:
                if part == "**":
                    segments.append(None)
                else:
                    if "**" in part:
                        raise ToolError(
                            "invalid_argument", "Recursive glob must be a complete segment"
                        )
                    segments.append(_segment_tokens(part))
        except re.error as exc:
            raise ToolError("invalid_argument", "Malformed corpus glob pattern") from exc
        self._segments = tuple(segments)

    def matches(self, path: str) -> bool:
        parts = path.split("/")
        states = {0}
        for index, segment in enumerate(self._segments):
            if segment is None:
                minimum = int(index == len(self._segments) - 1)
                states = {end for start in states for end in range(start + minimum, len(parts) + 1)}
            else:
                states = {
                    start + 1
                    for start in states
                    if start < len(parts) and _segment_matches(segment, parts[start])
                }
            if not states:
                return False
        return len(parts) in states


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
