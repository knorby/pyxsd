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

#: Built-in types pyxsd stores under ``String`` for textual binding, but
#: that XSD defines as primitives (or as list types) and therefore does
#: *not* derive from ``xs:string``/``xs:normalizedString``/``xs:token``.
#: The storage lattice makes them subclasses, so the relation has to be
#: corrected here (wild062.n3: an ``xsi:type="xs:time"`` governing type
#: is not substitutable for a locally declared ``xs:string``).
_STRING_STORAGE_PRIMITIVES = (
    "AnyURI",
    "QName",
    "NOTATION",
    "HexBinary",
    "Base64Binary",
    "DateTime",
    "Date",
    "Time",
    "GYear",
    "GYearMonth",
    "GMonth",
    "GMonthDay",
    "GDay",
    "Duration",
    "IDREFS",
    "ENTITIES",
    "NMTOKENS",
)


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


def _integerDerivesFromDecimal(override: type, declared: type) -> bool:
    """The XSD ``xs:decimal`` → ``xs:integer`` step, missing from Python.

    XSD derives the whole integer family from ``xs:decimal``, but the
    Python lattice maps ``xs:integer`` onto ``int`` and ``xs:decimal``
    onto ``decimal.Decimal``, which are unrelated classes. Only the
    built-in ``xs:decimal`` itself bridges: a user type derived from it
    is a restriction, and an integer is not derived from just any
    restriction of decimal.
    """
    from pyxsd import xsd_data_types

    decimal = getattr(xsd_data_types, "Decimal", None)
    integer = getattr(xsd_data_types, "Integer", None)
    if decimal is None or integer is None or declared is not decimal:
        return False
    return _pythonDerived(override, integer)


def _stringPrimitiveNotDerived(override: type, declared: type) -> bool:
    """Whether the storage lattice wrongly derives *override* from *declared*.

    Fires only for an XSD string-family *declared* type and an
    *override* that XSD defines as a primitive or list type: the Python
    ``str`` hierarchy derives them from ``String`` where XSD does not.
    """
    from pyxsd import xsd_data_types

    string = getattr(xsd_data_types, "String", None)
    if string is None:
        return False
    try:
        if not issubclass(declared, string):
            return False
    except TypeError:
        return False
    for name in _STRING_STORAGE_PRIMITIVES:
        primitive = getattr(xsd_data_types, name, None)
        if primitive is not None and _pythonDerived(override, primitive):
            return True
    return False


def derived_from_union_member(derived: type | None, base: type | None) -> bool:
    """Whether *derived* is validly derived from a (transitive) member
    type of a union base (Type Derivation OK (Simple), union clause).

    A restricting type may also restrict a *member that is itself a
    union* (saxonSimple012: ``sub-chap`` restricts ``dt``, a member of
    ``chap``): such an ancestor is a subtype of the base union exactly
    when its own (flattened) members are all members of the base. Used
    by the particle-derivation type clause and by the dynamic EDC check
    (wild066: a governing ``xs:date`` is compatible with a locally
    declared ``union(xs:date, xs:time)``).
    """
    if derived is None or base is None:
        return False
    base_members = getattr(base, "_unionMembers", None)
    if not base_members:
        return False
    for ancestor in derived.__mro__:
        if any(isinstance(b, type) and issubclass(ancestor, b) for b in base_members):
            return True
        ancestor_members = getattr(ancestor, "_unionMembers", None)
        if ancestor_members and all(
            any(isinstance(b, type) and issubclass(member, b) for b in base_members)
            for member in ancestor_members
        ):
            return True
    return False


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
    if derived and (
        _builtinNotDerived(override, declared) or _stringPrimitiveNotDerived(override, declared)
    ):
        derived = False
    if not derived and _integerDerivesFromDecimal(override, declared):
        derived = True
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
