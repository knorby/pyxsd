"""Mixin extensions for generated classes.

Mixins fold into the classes pyxsd generates at build time; the
per-compile :class:`Extensions` registry selects which mixins apply to
which type declarations.
"""

from collections.abc import Callable
from typing import Any

__all__ = ["Extensions"]

#: A compiled selector: a predicate over a type ER, or a raw path string
#: (an unresolved ``for_path`` entry, resolved by a later task).
_Selector = Callable[[Any], bool] | str

#: One registry entry: ``(selector, mixin, registration order)``.
_Entry = tuple[_Selector, type, int]


def _local_spelling(name: str) -> str:
    """The local part of a prefixed (``a:Patient``) or Clark name.

    A plain name is returned unchanged, so two spellings match when
    their local parts agree.
    """
    if "}" in name:
        return name.rsplit("}", 1)[-1]
    if ":" in name:
        return name.rsplit(":", 1)[-1]
    return name


class Extensions:
    """A per-compile registry of mixin selectors.

    Each registration method may be used as a decorator
    (``@ext.for_type(...)``), which registers the decorated class and
    returns it, or called directly (``ext.for_type(name, mixin=Cls)``).
    Every ``for_*`` selector compiles to a predicate over the type ER
    immediately; ``for_path`` is stored raw for later resolution.

    Registration order is precedence: a later registration outranks an
    earlier one, so :meth:`mixins_for` lists the most recently
    registered match first. Instances are standalone — no global
    registry state.
    """

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._counter = 0

    # -- registration -------------------------------------------------

    def for_type(self, name: str, mixin: type | None = None) -> Any:
        """Registers *mixin* for a type by name.

        *name* is a plain local name (``"Code"``) or a Clark name
        (``"{uri}Code"``). Without ``mixin``, returns a decorator that
        registers the decorated class.
        """

        def matches(er: Any) -> bool:
            return er.name == name or getattr(er, "expandedName", None) == name

        return self._register(matches, mixin)

    def for_namespace(self, uri: str, mixin: type | None = None) -> Any:
        """Registers *mixin* for every type declared in namespace *uri*."""

        def matches(er: Any) -> bool:
            name = getattr(er, "name", None)
            expanded = getattr(er, "expandedName", None)
            # ``expandedName`` is ``clark(namespace, name)``; matching it
            # against the same spelling built from *uri* compares the
            # namespace exactly (a URI containing ``}`` cannot alias a
            # prefix of another).
            return name is not None and expanded == f"{{{uri}}}{name}"

        return self._register(matches, mixin)

    def for_kind(
        self,
        kind: str,
        *,
        derived_from: str | None = None,
        mixin: type | None = None,
    ) -> Any:
        """Registers *mixin* for types of *kind* (``simpleType`` or
        ``complexType``).

        With *derived_from*, only types whose ``superClassNames`` list
        the given base (matched by local or expanded spelling) qualify.
        """

        def matches(er: Any) -> bool:
            if type(er).__name__.lower() != kind.lower():
                return False
            if derived_from is None:
                return True
            supers = getattr(er, "superClassNames", None) or ()
            wanted = _local_spelling(derived_from)
            return any(_local_spelling(str(super)) == wanted for super in supers)

        return self._register(matches, mixin)

    def for_path(self, path: str, mixin: type | None = None) -> Any:
        """Registers *mixin* for declarations at an instance *path*.

        The path is stored raw; resolution happens at instance-binding
        time, so this entry never matches a type ER lookup.
        """
        return self._register(path, mixin)

    def where(self, predicate: Callable[[Any], bool], mixin: type | None = None) -> Any:
        """Registers *mixin* for every type ER satisfying *predicate*."""
        return self._register(predicate, mixin)

    # -- lookup ---------------------------------------------------------

    def mixins_for(self, er: Any) -> tuple[type, ...]:
        """The mixins matching the type ER *er*, in precedence order.

        The first element is the highest-precedence (most recently
        registered) match; a mixin registered by several matching
        selectors appears once, at its highest-precedence position.
        Unresolved path entries are skipped.
        """
        matched: list[tuple[int, type]] = []
        for selector, mixin, order in self._entries:
            if not callable(selector) or not selector(er):
                continue
            matched.append((order, mixin))
        matched.sort(key=lambda entry: entry[0], reverse=True)
        seen: list[type] = []
        for _, mixin in matched:
            if not any(mixin is kept for kept in seen):
                seen.append(mixin)
        return tuple(seen)

    # -- internals ------------------------------------------------------

    def _register(self, selector: _Selector, mixin: type | None) -> Any:
        """Appends one entry; without *mixin*, returns a decorator."""
        if mixin is None:

            def decorator(cls: type) -> type:
                self._append(selector, cls)
                return cls

            return decorator
        self._append(selector, mixin)
        return None

    def _append(self, selector: _Selector, mixin: type) -> None:
        self._counter += 1
        self._entries.append((selector, mixin, self._counter))
