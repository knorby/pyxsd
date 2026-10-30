"""Session capture manifests and the bounded resource policy used by workers."""

import io
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from pyxsd.namespaces import NamespaceContext
from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.validation import ValidationReport

from .config import Limits
from .errors import ToolError
from .safe_io import SafeDirectory, validate_paths


class _CompletedParser(Protocol):
    root: ET.Element


@dataclass(frozen=True)
class CaptureFile:
    path: str
    digest: str
    size: int


@dataclass(frozen=True)
class CaptureDependency:
    path: str
    digest: str | None
    size: int


@dataclass(frozen=True)
class InputCapture:
    """A private temporary input set, never a durable input history."""

    workspace_id: str
    generation: int
    root: Path
    files: tuple[CaptureFile, ...]


class InputPolicyError(ResourcePolicyError):
    """A typed resource denial that core composition must never downgrade."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class InputResourcePolicy:
    """Read only manifested capture files; enforce XML budgets inside a worker."""

    def __init__(self, capture: InputCapture, limits: Limits) -> None:
        try:
            validate_paths([item.path for item in capture.files])
        except ToolError as exc:
            raise InputPolicyError(exc.fault.code, "Invalid capture paths") from exc
        self.capture, self.limits = capture, limits
        self._files = {item.path: item for item in capture.files}
        self._dependencies: dict[str, CaptureDependency] = {}
        self._schema_bytes = 0

    def rebind(self, capture: InputCapture) -> None:
        """Point a cached schema at the current temporary capture of the same bytes."""
        try:
            validate_paths([item.path for item in capture.files])
        except ToolError as exc:
            raise InputPolicyError(exc.fault.code, "Invalid capture paths") from exc
        self.capture = capture
        self._files = {item.path: item for item in capture.files}

    def new_report(self) -> ValidationReport:
        return ValidationReport(
            max_issues=self.limits.collected_issues,
            max_message_bytes=self.limits.message_bytes,
        )

    @property
    def dependencies(self) -> tuple[CaptureDependency, ...]:
        return tuple(self._dependencies[path] for path in sorted(self._dependencies))

    def _path(self, source: str, base: str | None) -> str:
        if source.startswith("/workspace/"):
            candidate = posixpath.normpath(source)
        elif source.startswith("/") or ":" in source or "\\" in source:
            raise InputPolicyError("invalid_path", "Unadmitted resource name")
        else:
            if base is None or base == "<memory>":
                parent = "/workspace"
            elif base.startswith("/workspace/"):
                parent = posixpath.dirname(base)
            else:
                raise InputPolicyError("invalid_path", "Unadmitted resource base")
            candidate = posixpath.normpath(posixpath.join(parent, source))
        if not candidate.startswith("/workspace/"):
            raise InputPolicyError("invalid_path", "Resource escapes its input capture")
        path = candidate.removeprefix("/workspace/")
        try:
            validate_paths([path])
        except ToolError as exc:
            raise InputPolicyError(exc.fault.code, "Invalid resource name") from exc
        return path

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        path = self._path(source, base)
        schema = purpose != "instance"
        new_dependency = schema and path not in self._dependencies
        if new_dependency and len(self._dependencies) >= self.limits.schema_closure_files:
            raise InputPolicyError("schema_limit", "Schema closure file limit exceeded")
        item = self._files.get(path)
        if item is None:
            if schema:
                self._dependencies[path] = CaptureDependency(path, None, 0)
            raise FileNotFoundError(path)
        budget = self.limits.schema_file_bytes if schema else self.limits.xml_file_bytes
        if item.size > budget or (
            new_dependency and self._schema_bytes + item.size > self.limits.schema_closure_bytes
        ):
            raise InputPolicyError(
                "schema_limit" if schema else "xml_limit", "Resource byte limit exceeded"
            )
        try:
            with SafeDirectory(self.capture.root) as directory:
                captured = directory.read(path, max_bytes=budget)
        except FileNotFoundError as exc:
            raise InputPolicyError("source_changed", "Captured resource disappeared") from exc
        except ToolError as exc:
            raise InputPolicyError(exc.fault.code, "Resource cannot be admitted") from exc
        if captured.digest != item.digest or len(captured.content) != item.size:
            raise InputPolicyError("source_changed", "Capture bytes no longer match the manifest")
        if new_dependency:
            self._dependencies[path] = CaptureDependency(path, item.digest, item.size)
            self._schema_bytes += item.size
        return ResourceData(f"/workspace/{path}", captured.content)

    def parse(self, data: ResourceData, context: NamespaceContext) -> ET.Element:
        if len(data.content) > self.limits.xml_file_bytes:
            raise InputPolicyError("xml_limit", "XML byte limit exceeded")
        pending: list[tuple[str, str]] = []
        stack: list[dict[str, str]] = [{}]
        count = 0
        try:
            with io.BytesIO(data.content) as source:
                events = DefusedET.iterparse(
                    source,
                    events=("start", "end", "start-ns", "end-ns"),
                    forbid_dtd=True,
                    forbid_entities=True,
                    forbid_external=True,
                )
                for event, payload in events:
                    if event == "start-ns":
                        prefix, uri = payload
                        pending.append((prefix or "", uri))
                    elif event == "start":
                        count += 1
                        if len(stack) > self.limits.xml_depth or count > self.limits.xml_elements:
                            raise InputPolicyError(
                                "xml_limit", "XML depth or element limit exceeded"
                            )
                        frame = dict(stack[-1])
                        frame.update(pending)
                        pending.clear()
                        stack.append(frame)
                        context.record(payload, frame)
                    elif event == "end":
                        stack.pop()
                return cast(_CompletedParser, events).root
        except DefusedXmlException as exc:
            raise InputPolicyError(
                "dangerous_xml", "DTD/entities/external references are forbidden"
            ) from exc
