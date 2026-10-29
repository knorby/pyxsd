"""Bounded live schemas for clean workers, never the coordinator or user code.

Acquisition is synchronous: the worker dispatcher owns its thread and holds the
cache entry for the complete parse/query/projection operation. Capture manifests
are immutable inputs; changed bytes under an old manifest fail closed and cause
recompilation against the current capture.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field

from pyxsd import Schema
from pyxsd.binding import ParseModes
from pyxsd.schema_context import last_components, remember_components

from .captures import InputCapture, InputPolicyError, InputResourcePolicy
from .config import Limits
from .contracts import SchemaRef
from .errors import ToolError
from .safe_io import validate_paths


def require_complete_schema(schema: Schema) -> None:
    """Fail closed when a leased schema's closure or report is unusable.

    A dependency recorded without a digest means the compiled closure is
    incomplete (for example an unresolved include); schema report errors mean
    the compiled schema itself is invalid. Warnings alone never fail here.
    Introspection-only callers may skip this check to diagnose broken schemas.
    """
    policy = schema.resource_policy
    assert isinstance(policy, InputResourcePolicy)
    missing = [dep.path for dep in policy.dependencies if dep.digest is None]
    if missing:
        raise ToolError(
            "schema_incomplete",
            "Schema closure is missing required files",
            details={"missing": [str(path) for path in missing], "path": missing[0]},
        )
    if schema.report.has_errors:
        raise ToolError(
            "invalid_schema",
            "Schema reported compilation errors",
            details={"errors": schema.report.error_count},
        )


@dataclass(frozen=True)
class CompileRequest:
    capture: InputCapture
    schema_ref: SchemaRef
    hints: tuple[tuple[str | None, str], ...]
    processor_version: str

    def __post_init__(self) -> None:
        # Frozen wire models still contain mutable mappings. Own a private copy.
        object.__setattr__(self, "schema_ref", self.schema_ref.model_copy(deep=True))


@dataclass(frozen=True)
class CacheInfo:
    hits: int
    misses: int
    evictions: int
    bypasses: int
    stale: int
    entries: int
    active: int


@dataclass
class _Entry:
    schema: Schema
    policy: InputResourcePolicy
    last_used: float
    pins: int = 0
    owner: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def _key(request: CompileRequest) -> str:
    refs = [request.schema_ref.source, *request.schema_ref.options.namespace_schemas.values()]
    for ref in refs:
        validate_paths([ref.path])
    data = {
        "files": sorted((item.path, item.digest, item.size) for item in request.capture.files),
        "schema": request.schema_ref.model_dump(mode="json"),
        "hints": request.hints,
        "processor": request.processor_version,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class SchemaCache:
    """Serialize each schema's leases, pin active entries and evict idle LRU entries.

    The count bound includes bypassed temporary compiles. Runtime memory limits
    remain mandatory: entry counts are not estimates of generated object sizes.
    No Document is retained. Derived registries owned by a schema die with it.
    """

    def __init__(
        self, limits: Limits, *, enabled: bool = True, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limits, self.enabled, self.clock = limits, enabled, clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._temporary = 0
        self._closed = False
        self._hits = self._misses = self._evictions = self._bypasses = self._stale = 0

    def info(self) -> CacheInfo:
        with self._lock:
            return CacheInfo(
                self._hits,
                self._misses,
                self._evictions,
                self._bypasses,
                self._stale,
                len(self._entries),
                self._temporary + sum(e.pins for e in self._entries.values()),
            )

    def sweep(self) -> None:
        with self._lock:
            for key, entry in list(self._entries.items()):
                if (
                    not entry.pins
                    and self.clock() - entry.last_used >= self.limits.cache_idle_seconds
                ):
                    del self._entries[key]
                    self._evictions += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for key, entry in list(self._entries.items()):
                if not entry.pins:
                    del self._entries[key]

    def _compile(self, request: CompileRequest) -> _Entry:
        policy = InputResourcePolicy(request.capture, self.limits)
        options = request.schema_ref.options
        mode = ParseModes.LAX if options.binding == "lax" else ParseModes.STRICT
        schema = Schema.compile(
            "/workspace/" + request.schema_ref.source.path,
            mode=mode.replace(namespaces=options.namespaces),
            xsd_version=options.xsd_version,
            namespace_schemas={
                k: "/workspace/" + v.path for k, v in options.namespace_schemas.items()
            },
            schema_location_pairs=[(ns, path) for ns, path in request.hints],
            resource_policy=policy,
        )
        return _Entry(schema, policy, self.clock())

    @contextmanager
    def acquire(self, request: CompileRequest) -> Iterator[Schema]:
        previous = last_components()
        entry: _Entry | None = None
        pinned = False
        key = _key(request)
        try:
            with self._lock:
                if self._closed:
                    raise ToolError("cache_closed", "Schema cache is closed")
                self.sweep()
                entry = self._entries.get(key) if self.enabled else None
                if entry is not None:
                    if entry.owner == threading.get_ident():
                        raise ToolError("cache_busy", "Schema leases are not reentrant")
                    # Verify every dependency against the current capture. A
                    # deleted/changed capture invalidates the entry, not the request.
                    policy = InputResourcePolicy(request.capture, self.limits)
                    try:
                        for dep in entry.policy.dependencies:
                            with suppress(FileNotFoundError):
                                policy.read(dep.path, base=None, purpose="schema")
                    except InputPolicyError as exc:
                        self._stale += 1
                        if exc.code != "source_changed":
                            raise
                        del self._entries[key]
                        entry = None
                    if entry is not None:
                        entry.policy.rebind(request.capture)
                        self._hits += 1
                        self._entries.move_to_end(key)
                if entry is None:
                    if len(self._entries) + self._temporary >= self.limits.cache_entries:
                        idle = next((k for k, e in self._entries.items() if not e.pins), None)
                        if idle is None:
                            raise ToolError("cache_busy", "All schema cache entries are leased")
                        del self._entries[idle]
                        self._evictions += 1
                    entry = self._compile(request)
                    if self.enabled:
                        self._entries[key] = entry
                        self._misses += 1
                    else:
                        self._temporary += 1
                        self._bypasses += 1
                entry.pins += 1
                pinned = True
            with entry.lock:
                entry.owner = threading.get_ident()
                try:
                    yield entry.schema
                finally:
                    entry.owner = None
        finally:
            # Do not alter the library's legacy last-parser behavior globally.
            # Worker operations restore their thread's prior compatibility view.
            remember_components(previous)
            if entry is not None and pinned:
                with self._lock:
                    entry.pins -= 1
                    entry.last_used = self.clock()
                    if not self.enabled:
                        self._temporary -= 1
                    elif self._closed and not entry.pins:
                        self._entries.pop(key, None)
