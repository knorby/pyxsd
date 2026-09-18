"""Thread-local construction context for schema representatives.

Historically a parser installed module-level globals (the namespace
overrides, the source form defaults, and the active component table) and
every ``Schema`` representative captured whatever happened to be
installed when it was constructed. Two parsers interleaved across
threads could therefore capture each other's context, and a failed
parse could leave its context installed.

The context is now an explicit, parser-owned object that is activated
only for the duration of a parser's schema construction and instance
binding. Activation is thread-local and stack-scoped: concurrent
parsers on different threads cannot see each other's context, and a
nested parser restores the enclosing context when it finishes.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

_T = TypeVar("_T", bound=Callable[..., Any])


@dataclass
class SchemaContext:
    """The parser-owned state visible while representatives are built.

    - ``components``: the parser's component table once the schema
      representative exists (``None`` while it is being constructed).
    - ``namespace_overrides``: per-component namespace overrides for
      declarations spliced in from imported/included documents.
    - ``form_defaults``: the source document's
      ``(elementFormDefault, attributeFormDefault)`` per spliced
      component.
    - ``injected_builtin_ids``: ``id(xsdElement)`` of the built-in
      components the parser itself injected (the implicit ``xml`` and
      ``xsi`` namespace attribute declarations). Kept apart from
      ``namespace_overrides`` because a user document may legally target
      a well-known namespace, and the declaration-legality checks must
      tell the injected declarations from such spliced user ones.
    """

    components: Any = None
    namespace_overrides: dict[int, str | None] = field(default_factory=dict)
    form_defaults: dict[int, tuple[str | None, str | None]] = field(default_factory=dict)
    injected_builtin_ids: set[int] = field(default_factory=set)


_local = threading.local()


def _stack() -> list[SchemaContext]:
    stack = getattr(_local, "stack", None)
    if stack is None:
        stack = []
        _local.stack = stack
    return stack


def current_context() -> SchemaContext | None:
    """Returns the active context for this thread, or ``None``."""
    stack = _stack()
    return stack[-1] if stack else None


def ambient_context() -> SchemaContext:
    """Returns the thread's fallback context for direct API use.

    The module-level ``get_active_*``/``set_active_*`` helpers predate
    the stack; callers outside a parser (tests, direct representative
    construction) install into this per-thread context.
    """
    context = getattr(_local, "ambient", None)
    if context is None:
        context = SchemaContext()
        _local.ambient = context
    return context


def context_or_ambient() -> SchemaContext:
    """The active stack context, or the thread's ambient context."""
    return current_context() or ambient_context()


def remember_components(components: Any) -> None:
    """Records the most recently completed parser's component table.

    Historical module-level lookups (``ElementRepresentative.getFromName``
    and the ``registry`` proxy) resolve against the last parser run on
    this thread. That view is compatible with the previous behavior but
    is per-thread and explicit, so it cannot be corrupted by a parser
    that is still constructing.
    """
    _local.last_components = components


def last_components() -> Any:
    """The most recently completed parser's component table, or ``None``."""
    return getattr(_local, "last_components", None)


@contextmanager
def active_context(context: SchemaContext) -> Iterator[SchemaContext]:
    """Makes *context* current for the duration of the block.

    Nested activations restore the previously active context, and the
    stack is per-thread, so concurrent parsers stay isolated.
    """
    stack = _stack()
    stack.append(context)
    try:
        yield context
    finally:
        stack.pop()


def with_schema_context(method: _T) -> _T:
    """Decorates a ``PyXSD`` method so it runs under ``self.schemaContext``.

    The context is pushed on entry and popped on exit (including
    exceptions), which preserves the enclosing context for nested
    parser construction.
    """

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        context = getattr(self, "schemaContext", None)
        if context is None:
            return method(self, *args, **kwargs)
        with active_context(context):
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
