"""Session-scoped input sets and bounded temporary operation captures."""

import secrets
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import anyio

from .captures import CaptureDependency, CaptureFile, InputCapture
from .config import Limits
from .contracts import FileRef, InlineFile, SchemaRef
from .errors import ToolError
from .safe_io import AdmittedFile, CapturedFile, SafeDirectory, admit_inline, validate_paths


class DiscoveryWorker(Protocol):
    async def discover(
        self, capture: InputCapture, refs: tuple[FileRef | SchemaRef, ...]
    ) -> Sequence[CaptureDependency]: ...


@dataclass(frozen=True)
class Workspace:
    id: str
    created_at: float


@dataclass
class _Area:
    files: dict[str, AdmittedFile]
    generation: int
    created: float
    lock: anyio.Lock = field(default_factory=anyio.Lock)


class SessionInputs:
    """Own transient workspace inputs; outputs belong to the separate store."""

    def __init__(
        self,
        scratch: Path,
        limits: Limits,
        *,
        source_root: Path | None = None,
        discovery: DiscoveryWorker | None = None,
        excluded_source_root: Path | None = None,
    ) -> None:
        self.scratch, self.limits = scratch, limits
        self.source_root, self.discovery = source_root, discovery
        self.excluded_source_root = excluded_source_root
        self.scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._root = self.scratch / "inputs"
        self._root.mkdir(mode=0o700, exist_ok=True)
        self._captures = self.scratch / "captures"
        self._captures.mkdir(mode=0o700, exist_ok=True)
        self._areas: dict[str, _Area] = {}
        self._lock = anyio.Lock()

    def _area(self, workspace_id: str) -> _Area:
        area = self._areas.get(workspace_id)
        if area is None:
            raise ToolError("not_found", "Workspace not found")
        return area

    async def has_files(self, workspace_id: str) -> bool:
        """Whether the session workspace currently holds uploaded files."""
        async with self._lock:
            return bool(self._area(workspace_id).files)

    async def create(self) -> Workspace:
        async with self._lock:
            if len(self._areas) >= self.limits.workspaces:
                raise ToolError("quota_exceeded", "Session workspace limit reached")
            for _ in range(4):
                identifier = secrets.token_hex(16)
                path = self._root / identifier
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError:
                    continue
                self._areas[identifier] = _Area({}, 0, time.time())
                return Workspace(identifier, time.time())
            raise ToolError(
                "allocation_conflict", "Workspace identifier is unavailable", retryable=True
            )

    async def list(self) -> list[Workspace]:
        return [Workspace(identifier, area.created) for identifier, area in self._areas.items()]

    async def _new_directory(self, prefix: str) -> Path:
        return Path(
            await anyio.to_thread.run_sync(
                lambda: tempfile.mkdtemp(prefix=prefix, dir=self._captures)
            )
        )

    async def release(self, workspace_id: str) -> None:
        async with self._lock:
            area = self._area(workspace_id)
            async with area.lock:
                self._areas.pop(workspace_id, None)
            await anyio.to_thread.run_sync(shutil.rmtree, self._root / workspace_id, True)

    async def put_files(self, workspace_id: str, files: Sequence[InlineFile]) -> None:
        admitted = admit_inline(files, self.limits)
        async with self._lock:
            area = self._area(workspace_id)
            async with area.lock:
                if not admitted:
                    return
                merged = {**area.files, **{item.path: item for item in admitted}}
                self._validate(merged)
                directory = await self._new_directory("upload-")
                try:
                    await anyio.to_thread.run_sync(
                        self._write_batch, directory, tuple(merged.values())
                    )
                    current = self._root / workspace_id / "files"
                    previous = self._root / workspace_id / "previous"
                    replaced = current.exists()
                    if replaced:
                        current.rename(previous)
                    try:
                        directory.rename(current)
                    except BaseException:
                        if replaced:
                            previous.rename(current)
                        raise
                    if previous.exists():
                        shutil.rmtree(previous)
                    area.files, area.generation = merged, area.generation + 1
                finally:
                    shutil.rmtree(directory, ignore_errors=True)

    async def manifest(self, workspace_id: str) -> InputCapture:
        """Return generation and admitted-file metadata without copying inputs.

        This metadata snapshot is not a verified operation capture.
        """
        async with self._lock:
            area = self._area(workspace_id)
            return InputCapture(
                workspace_id,
                area.generation,
                self._root / workspace_id / "files",
                tuple(
                    CaptureFile(path, self._digest(item), len(item.content))
                    for path, item in sorted(area.files.items())
                ),
            )

    def _validate(self, files: dict[str, AdmittedFile]) -> None:
        validate_paths(list(files))
        entries = set(files)
        for path in files:
            parts = path.split("/")
            for index in range(1, len(parts)):
                entries.add("/".join(parts[:index]))
        if len(entries) > self.limits.input_entries:
            raise ToolError("quota_exceeded", "Input entry limit exceeded")
        if sum(len(item.content) for item in files.values()) > self.limits.workspace_bytes:
            raise ToolError("quota_exceeded", "Input byte limit exceeded")

    @staticmethod
    def _write_batch(root: Path, files: Sequence[AdmittedFile]) -> None:
        root.mkdir(mode=0o700, exist_ok=True)
        for item in files:
            path = root / item.path
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("xb") as output:
                output.write(item.content)

    @asynccontextmanager
    async def capture(self, workspace_id: str) -> AsyncIterator[InputCapture]:
        directory = await self._new_directory("capture-")
        try:
            async with self._lock:
                area = self._area(workspace_id)
                async with area.lock:
                    files = await anyio.to_thread.run_sync(self._copy_area, area, directory)
                    capture = InputCapture(workspace_id, area.generation, directory, files)
            yield capture
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _copy_area(self, area: _Area, directory: Path) -> tuple[CaptureFile, ...]:
        current = (
            self._root
            / next(identifier for identifier, candidate in self._areas.items() if candidate is area)
            / "files"
        )
        files = []
        if area.files:
            with SafeDirectory(current) as source:
                for path in sorted(area.files):
                    expected = area.files[path]
                    item = source.read(path, max_bytes=self.limits.xml_file_bytes)
                    if item.content != expected.content or item.digest != self._digest(expected):
                        raise ToolError("source_changed", "Session input changed during capture")
                    destination = directory / path
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    destination.write_bytes(item.content)
                    files.append(CaptureFile(path, item.digest, len(item.content)))
        return tuple(files)

    @staticmethod
    def _digest(item: AdmittedFile) -> str:
        import hashlib

        return hashlib.sha256(item.content).hexdigest()

    @asynccontextmanager
    async def capture_source(
        self, workspace_id: str, refs: Sequence[FileRef | SchemaRef]
    ) -> AsyncIterator[InputCapture]:
        if self.source_root is None or self.discovery is None:
            raise ToolError("capture_unavailable", "No source grant/discovery worker configured")
        async with self._lock:
            self._area(workspace_id)
        directory = await self._new_directory("capture-")
        try:
            try:
                capture = await self._capture_source_once(directory, tuple(refs), workspace_id)
            except ToolError as exc:
                if exc.fault.code != "source_changed":
                    raise
                shutil.rmtree(directory)
                directory.mkdir(mode=0o700)
                capture = await self._capture_source_once(directory, tuple(refs), workspace_id)
            yield capture
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    @asynccontextmanager
    async def operation_capture(
        self, workspace_id: str, refs: Sequence[FileRef | SchemaRef]
    ) -> AsyncIterator[InputCapture]:
        """Select the input set for one operation.

        Uploaded workspace files always win and are never merged with the
        configured source tree. An empty workspace with a configured source root
        captures just the referenced files and their discovered dependencies;
        an empty workspace without one keeps the plain (empty) capture so the
        caller reports the missing typed inputs.
        """
        if await self.has_files(workspace_id):
            async with self.capture(workspace_id) as captured:
                yield captured
        elif self.source_root is not None and self.discovery is not None:
            async with self.capture_source(workspace_id, refs) as captured:
                yield captured
        else:
            async with self.capture(workspace_id) as captured:
                yield captured

    async def _capture_source_once(
        self, directory: Path, refs: tuple[FileRef | SchemaRef, ...], workspace_id: str
    ) -> InputCapture:
        assert self.source_root is not None and self.discovery is not None
        sources = [
            file
            for ref in refs
            for file in (
                (ref.source, *ref.options.namespace_schemas.values())
                if isinstance(ref, SchemaRef)
                else (ref,)
            )
        ]
        if not sources:
            raise ToolError("invalid_argument", "Source capture needs at least one file")
        sources = list({file.path: file for file in sources}.values())
        validate_paths([file.path for file in sources])
        schema_paths = {
            file.path
            for ref in refs
            if isinstance(ref, SchemaRef)
            for file in (ref.source, *ref.options.namespace_schemas.values())
        }
        captured: dict[str, AdmittedFile] = {}
        digests: dict[str, str] = {}
        observed: dict[str, CapturedFile] = {}
        missing: set[str] = set()
        materialized: set[str] = set()
        source_root = self.source_root
        with SafeDirectory(source_root) as source:

            def check_schema_limits() -> None:
                items = [captured[path] for path in schema_paths if path in captured]
                if (
                    len(schema_paths) > self.limits.schema_closure_files
                    or any(len(item.content) > self.limits.schema_file_bytes for item in items)
                    or sum(len(item.content) for item in items) > self.limits.schema_closure_bytes
                ):
                    raise ToolError("schema_limit", "Capture schema closure limit exceeded")

            def read(paths: Sequence[str], *, required: bool) -> bool:
                if not required:
                    schema_paths.update(paths)
                check_schema_limits()
                changed = False
                for path in paths:
                    if self.excluded_source_root is not None and (
                        source_root.resolve() / path
                    ).is_relative_to(self.excluded_source_root.resolve()):
                        raise ToolError("invalid_argument", "MCP state is not a source input")
                    if path in captured or path in missing:
                        continue
                    if len(captured) + len(missing) >= self.limits.input_entries:
                        raise ToolError("quota_exceeded", "Capture entry limit exceeded")
                    remaining = self.limits.workspace_bytes - sum(
                        len(item.content) for item in captured.values()
                    )
                    try:
                        item = source.read(
                            path, max_bytes=min(remaining, self.limits.xml_file_bytes)
                        )
                    except FileNotFoundError as exc:
                        if required:
                            raise ToolError("not_found", "Capture source not found") from exc
                        missing.add(path)
                    else:
                        captured[path] = AdmittedFile(path, item.content)
                        digests[path] = item.digest
                        observed[path] = item
                        check_schema_limits()
                        changed = True
                validate_paths([*captured, *missing])
                return changed

            await anyio.to_thread.run_sync(
                lambda: read(tuple(file.path for file in sources), required=True)
            )
            while True:
                pending = tuple(item for path, item in captured.items() if path not in materialized)
                await anyio.to_thread.run_sync(self._write_batch, directory, pending)
                materialized.update(captured)
                files = tuple(
                    CaptureFile(path, digests[path], len(captured[path].content))
                    for path in sorted(captured)
                )
                found = await self.discovery.discover(
                    InputCapture(workspace_id, 0, directory, files), refs
                )
                names = tuple(dict.fromkeys(item.path for item in found))
                validate_paths(names)

                def expand(paths: Sequence[str] = names) -> None:
                    changed = read(paths, required=False)
                    for item in captured.values():
                        source.verify(observed[item.path], max_bytes=self.limits.xml_file_bytes)
                    if changed:
                        raise _MoreFiles

                try:
                    await anyio.to_thread.run_sync(expand)
                except _MoreFiles:
                    continue
                break
            return InputCapture(
                workspace_id,
                0,
                directory,
                tuple(
                    CaptureFile(path, digests[path], len(captured[path].content))
                    for path in sorted(captured)
                ),
            )


class _MoreFiles(Exception):
    pass
