"""Bounded reads relative to an admitted directory descriptor, never symlinks."""

import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import Limits
from .contracts import InlineFile
from .errors import ToolError


def validate_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Preserve relative POSIX names while rejecting ambiguous portable spellings."""
    spellings: dict[str, str] = {}
    files: set[str] = set()
    for path in paths:
        try:
            valid = (
                isinstance(path, str)
                and bool(path)
                and len(path.encode("utf-8")) <= 4096
                and len(path.split("/")) <= 128
                and not any(ord(char) < 32 or ord(char) == 127 for char in path)
                and not any(char in path for char in ("\\", ":"))
                and all(
                    part not in ("", ".", "..") and len(part.encode("utf-8")) <= 255
                    for part in path.split("/")
                )
            )
        except UnicodeEncodeError:
            valid = False
        if not valid:
            raise ToolError("invalid_path", "Expected an unambiguous relative POSIX path")
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            key = unicodedata.normalize("NFC", unicodedata.normalize("NFC", prefix).casefold())
            if key in files or (
                key in spellings and (spellings[key] != prefix or index == len(parts))
            ):
                raise ToolError("path_collision", "Paths collide after case/Unicode normalization")
            spellings[key] = prefix
        files.add(key)
    return tuple(paths)


@dataclass(frozen=True)
class AdmittedFile:
    path: str
    content: bytes


def admit_inline(files: Sequence[InlineFile], limits: Limits) -> tuple[AdmittedFile, ...]:
    """Encode a small UTF-8 bundle without parsing XML on the coordinator.

    Only complete leading XML declarations are inspected for conflicting encoding
    labels. Well-formedness and hostile XML checks belong to constrained workers;
    other files, such as transformation source, need not contain XML at all.
    """
    validate_paths([item.path for item in files])
    admitted: list[AdmittedFile] = []
    total = 0
    for item in files:
        try:
            content = item.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ToolError("invalid_encoding", "Inline text must be valid UTF-8") from exc
        total += len(content)
        if total > limits.inline_bytes or len(content) > limits.xml_file_bytes:
            raise ToolError("quota_exceeded", "Inline file byte budget exceeded")
        start = re.match(r"\ufeff?<\?xml(?=\s)", item.text)
        if start is not None:
            end = item.text.find("?>", start.end())
            if end >= 0:
                encoding = re.search(
                    r"\bencoding\s*=\s*(['\"])([^'\"]*)\1", item.text[start.end() : end]
                )
                if encoding is not None and encoding[2].lower() not in ("utf-8", "utf8"):
                    raise ToolError(
                        "invalid_encoding",
                        "Inline XML must declare UTF-8; upload original bytes otherwise",
                    )
        admitted.append(AdmittedFile(item.path, content))
    return tuple(admitted)


@dataclass(frozen=True)
class CapturedFile:
    path: str
    content: bytes
    digest: str
    identity: tuple[int, int, int, int, int]


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class SafeDirectory:
    """An operator-admitted root; client paths cannot replace its open descriptor."""

    def __init__(self, root: Path) -> None:
        if os.open not in os.supports_dir_fd or not all(
            hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
        ):
            raise ToolError("unsupported_platform", "Safe descriptor-relative reads unavailable")
        try:
            self._fd: int | None = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ToolError("unsafe_path", "Input root cannot be admitted") from exc

    def read(self, path: str, *, max_bytes: int) -> CapturedFile:
        """Read original bytes, checking regular-file type and stability on the FD."""
        validate_paths([path])
        if type(max_bytes) is not int or max_bytes < 0:
            raise ToolError("invalid_argument", "Read budget must be a nonnegative integer")
        if self._fd is None:
            raise ToolError("closed", "Input directory is closed")
        current = os.dup(self._fd)
        try:
            parts = path.split("/")
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ToolError("unsafe_path", "Only regular input files are admitted")
                if before.st_size > max_bytes:
                    raise ToolError("quota_exceeded", "Input file exceeds byte budget")
                chunks: list[bytes] = []
                total = 0
                while chunk := os.read(fd, min(65536, max_bytes - total + 1)):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ToolError("quota_exceeded", "Input file exceeds byte budget")
                    chunks.append(chunk)
                if _identity(before) != _identity(os.fstat(fd)) or total != before.st_size:
                    raise ToolError(
                        "source_changed", "Input changed during capture", retryable=True
                    )
                content = b"".join(chunks)
                return CapturedFile(
                    path, content, hashlib.sha256(content).hexdigest(), _identity(before)
                )
            finally:
                os.close(fd)
        except FileNotFoundError:
            # Composition distinguishes a missing import from an access denial.
            raise
        except (OSError, UnicodeError) as exc:
            raise ToolError("unsafe_path", "Input path cannot be admitted") from exc
        finally:
            os.close(current)

    def verify(self, captured: CapturedFile, *, max_bytes: int) -> None:
        """Re-read through the root, detecting replacement and content changes."""
        try:
            current = self.read(captured.path, max_bytes=max_bytes)
        except FileNotFoundError as exc:
            raise ToolError(
                "source_changed", "Input disappeared after capture", retryable=True
            ) from exc
        if (current.identity, current.digest) != (captured.identity, captured.digest):
            raise ToolError("source_changed", "Input changed after capture", retryable=True)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "SafeDirectory":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
