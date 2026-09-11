"""Parse modes and the binding policies they select.

Validation reporting is always strict: a parse mode changes only what
value is bound into the tree when a document is not strictly valid,
never whether the problem is reported to
:attr:`~pyxsd.parser.PyXSD.report`. That separation lets a validation
user trust the report while a data-mapping user still gets usable
objects out of a messy, real-world document.

Policies are frozen dataclasses. :class:`ParseModes` is a namespace of
named presets; a policy can also be built directly and passed to
``PyXSD(mode=...)`` for per-field control, so the API can grow new
fields without changing call signatures.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

InvalidValue = Literal["drop", "raw"]
UnresolvedType = Literal["error", "generic"]
UndeclaredContent = Literal["error", "generic"]
WhitespaceHandling = Literal["xsd", "compat"]


@dataclass(frozen=True)
class BindingPolicy:
    """How instance binding behaves when a document is not strictly valid.

    - ``invalid_value`` - for an element whose lexical value does not
      validate: ``"drop"`` binds ``None`` (each type's "unvalidated"
      stand-in); ``"raw"`` binds the original text as a plain string so
      no data is lost.
    - ``unresolved_type`` - when an element's declared type cannot be
      resolved: ``"error"`` records the problem and drops the subtree;
      ``"generic"`` records the problem too, but also binds the subtree
      through the wildcard pass-through path.
    - ``undeclared_content`` - elements that no content-model particle
      accepts: ``"error"`` records the problem; ``"generic"`` also
      binds them generically so unrecognized markup survives.
    - ``whitespace`` - ``"xsd"`` applies XSD 1.0 whitespace processing
      (space/tab/CR/LF only); ``"compat"`` additionally folds other
      Unicode whitespace (for example NBSP) the way Python's own
      ``str.strip`` does.
    """

    invalid_value: InvalidValue = "drop"
    unresolved_type: UnresolvedType = "error"
    undeclared_content: UndeclaredContent = "error"
    whitespace: WhitespaceHandling = "xsd"

    def replace(self, **changes: object) -> BindingPolicy:
        """Return a copy with the given fields changed.

        ``BindingPolicy().replace(invalid_value="raw")`` is the
        supported way to tweak one field of a preset.
        """
        return replace(self, **changes)  # type: ignore[arg-type]


class ParseModes:
    """Named :class:`BindingPolicy` presets for ``PyXSD(mode=...)``.

    Each attribute is an ordinary policy, so a caller can pass
    ``ParseModes.LAX`` for a convenient preset or a hand-built
    ``BindingPolicy`` for granular control. New presets belong here;
    no call signature has to change.
    """

    #: Today's behavior: problems are reported and nothing invalid is
    #: bound into the tree.
    STRICT = BindingPolicy()

    #: Keep the report strict, but bind best-effort values: invalid
    #: lexical values become raw strings, and unresolved or undeclared
    #: subtrees are bound generically instead of dropped.
    LAX = BindingPolicy(
        invalid_value="raw",
        unresolved_type="generic",
        undeclared_content="generic",
    )
