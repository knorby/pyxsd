"""XSD type-derivation compatibility checking.

Used by ``xsi:type`` dispatch (at the document root and on child
elements): the type named by ``xsi:type`` must be *validly derived*
from the element's declared type, and the derivation must not be
blocked by the element or the declared type. The generated Python
classes mirror the schema's derivation hierarchy (a type's generated
class inherits its base type's class), so ``issubclass`` plus the
recorded derivation method provides the check.
"""

from __future__ import annotations

from typing import Any

# ``xs:boolean`` is modeled as an ``Integer`` subclass in the Python
# lattice for its numeric behaviour, but XSD does not derive it from
# ``xs:integer``; the compatibility check must not treat it as if it
# did.
_BOOLEAN_INTEGER_EXCEPTION = ("Boolean", "Integer")


def blockTokens(value: Any) -> frozenset[str]:
    """Splits an XSD ``block``/``final`` attribute into its tokens."""
    if not value:
        return frozenset()
    return frozenset(str(value).split())


def _pythonDerived(override: type, declared: type) -> bool:
    try:
        return issubclass(override, declared)
    except TypeError:
        return False


def _builtinNotDerived(override: type, declared: type) -> bool:
    from pyxsd import xsd_data_types

    boolean = getattr(xsd_data_types, "Boolean", None)
    integer = getattr(xsd_data_types, "Integer", None)
    if boolean is None or integer is None:
        return False
    return (
        _pythonDerived(override, boolean)
        and _pythonDerived(declared, integer)
        and not _pythonDerived(declared, boolean)
    )


def combinedBlock(elementBlock: Any, declaredCls: type | None) -> frozenset[str]:
    """Returns the union of an element's and its type's ``block`` tokens."""
    tokens = set(blockTokens(elementBlock))
    tokens |= blockTokens(getattr(declaredCls, "_block_", None))
    return frozenset(tokens)


def _blocked_step(override: type, declared: type, tokens: frozenset[str]) -> bool:
    """Whether any derivation step from *override* up to *declared* is
    blocked.

    A derivation chain is only as good as its weakest link: the corpus
    (particlesIg003) pins an ``xsi:type`` override invalid when a
    *transitive* step's derivation method appears in the blocked set,
    not just the override's own final step. A generated class with no
    recorded derivation sits either on another generated class (a
    simpleContent extension, whose marker the class builder loses —
    particlesElemT058 pins such a step *unblocked*) or directly on the
    ur-type, which it restricts implicitly; only the latter counts as a
    ``restriction`` step.
    """
    from pyxsd.schema_base import SchemaBase

    mro = override.__mro__

    def step_method(index: int, cls: type) -> str | None:
        method = getattr(cls, "_derivation_", None)
        if method is not None:
            return str(method)
        if cls is declared or cls is SchemaBase:
            # The step *to* the declared type and the ur-type stand-in
            # carry no marker of their own.
            return None
        successor = mro[index + 1] if index + 1 < len(mro) else None
        if successor is not None and getattr(successor, "_contentKind_", None) is not None:
            # A generated parent: the derivation marker is lost, not
            # implicit — do not guess.
            return None
        return "restriction"

    for index, cls in enumerate(mro):
        if cls is declared:
            return False
        method = step_method(index, cls)
        if method is not None and method in tokens:
            return True
    return False


def is_validly_derived(
    override: type | None,
    declared: type | None,
    blocked: Any = None,
) -> str | None:
    """Returns ``None`` when *override* may stand in for *declared*.

    Otherwise returns a short reason: ``"not-derived"`` when the type
    is unrelated to the declared type, or ``"blocked"`` when the
    applicable ``block`` value forbids the derivation method.
    """
    if override is None or declared is None:
        return None
    if override is declared:
        return None
    derived = _pythonDerived(override, declared)
    if derived and _builtinNotDerived(override, declared):
        derived = False
    if not derived:
        return "not-derived"

    tokens = frozenset(blocked) if isinstance(blocked, (set, frozenset)) else blockTokens(blocked)
    if "#all" in tokens:
        return "blocked"
    if tokens and _blocked_step(override, declared, tokens):
        return "blocked"
    return None


def derivationMessage(override: Any, declared: Any, reason: str) -> str:
    """A human-readable explanation for a rejected ``xsi:type``."""
    override_name = getattr(override, "name", getattr(override, "__name__", override))
    declared_name = getattr(declared, "name", getattr(declared, "__name__", declared))
    if reason == "blocked":
        return (
            f"type '{override_name}' may not replace the declared type "
            f"'{declared_name}' because the derivation method is blocked"
        )
    return f"type '{override_name}' is not validly derived from the declared type '{declared_name}'"
