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

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from pyxsd.validation import ValidationReport


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
      components the parser itself injected (the implicit ``xml``,
      ``xsi`` and ``xlink`` namespace attribute declarations). Kept apart from
      ``namespace_overrides`` because a user document may legally target
      a well-known namespace, and the declaration-legality checks must
      tell the injected declarations from such spliced user ones.

    - ``report``: the report instance-binding issues are routed to while
      this context is active (``None`` during schema compilation, which
      reports through the composition context instead). Set per parse so
      each document bound against a shared :class:`~pyxsd.schema.Schema`
      gets a fresh report.
    """

    components: Any = None
    namespace_overrides: dict[int, str | None] = field(default_factory=dict)
    form_defaults: dict[int, tuple[str | None, str | None]] = field(default_factory=dict)
    xpath_default_namespaces: dict[int, str | None] = field(default_factory=dict)
    injected_builtin_ids: set[int] = field(default_factory=set)
    report: ValidationReport | None = None


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

    Nested activations restore the previously active context. The
    stack itself is per-thread, so two *different* parsers on
    different threads cannot see each other's contexts — but this is
    not a license to share one parser across threads: the
    ``SchemaContext`` object is owned by a single parser (see
    :func:`context_report`), so concurrent use of one
    ``Schema``/parser from several threads races on its shared
    fields, the binding ``report`` included. The contract is
    one-thread-per-Schema at a time.
    """
    stack = _stack()
    stack.append(context)
    try:
        yield context
    finally:
        stack.pop()


@contextmanager
def context_report(context: SchemaContext, report: ValidationReport) -> Iterator[SchemaContext]:
    """Installs *report* as *context*'s routing report for the block.

    Diagnostic routing reads the active context's report first (see
    ``SchemaBase._report_issue`` and the ER-layer ``_reportSchemaError``):
    schema compilation installs the schema-phase report for the run, and
    a caller binding an instance document installs its fresh per-parse
    report here; the previous value is restored afterwards.

    The install mutates the single ``report`` field of the *shared*
    context object owned by the parser, even though the activation
    stack is per-thread: two threads calling ``Schema.parse`` on the
    same schema concurrently overwrite each other's instance report.
    The restore-previous behaviour keeps repeated sequential parses
    correct; cross-thread safety is the one-thread-per-Schema
    contract (see :func:`active_context`). Making the report itself
    thread-local is future work, not done here.
    """
    previous = context.report
    context.report = report
    try:
        yield context
    finally:
        context.report = previous
