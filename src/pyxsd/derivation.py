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
    A declared type that is itself derived from the same primitive is
    not corrected: a user restriction of ``xs:date`` is a ``Date``
    subclass too, and it *is* validly derived from another ``xs:date``
    restriction (only cross-family pairs, like ``xs:time`` over
    ``xs:string``, are unrelated).
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
        if (
            primitive is not None
            and _pythonDerived(override, primitive)
            and not _pythonDerived(declared, primitive)
        ):
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
    # ``_unionMembers`` is read from the class's own namespace, never
    # inherited: a restriction of a union inherits the attribute through
    # its MRO but is not itself a union, and a member type is not
    # validly derived from the *restricted* union (MS stZ073b).
    base_members = vars(base).get("_unionMembers")
    if not base_members:
        return False
    for ancestor in derived.__mro__:
        if any(isinstance(b, type) and issubclass(ancestor, b) for b in base_members):
            return True
        ancestor_members = vars(ancestor).get("_unionMembers")
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


def _isSimpleTypeClass(cls: type) -> bool:
    """Whether a Python type class stands for an XSD *simple* type.

    The built-in datatypes and generated simple types (whose classes
    inherit the base datatype) subclass :class:`XsdDataType`; complex
    types subclass :class:`SchemaBase` without a datatype base.
    """
    from pyxsd import xsd_data_types

    try:
        return issubclass(cls, xsd_data_types.XsdDataType)
    except TypeError:
        return False


def is_valid_xsi_type(
    override: type | None,
    declared: type | None,
    blocked: Any = None,
) -> str | None:
    """Instance-type validity of an ``xsi:type`` override.

    Like :func:`is_validly_derived`, plus the ur-type clauses:
    every type is validly derived from ``xs:anyType``, and every
    *simple* type from ``xs:anySimpleType`` — but ``xs:anyType`` itself
    is a complex type and may not replace a simple-typed declaration
    (stZ056). Used by the xsi:type dispatch sites (document root,
    child elements, substitution members).
    """
    from pyxsd import xsd_data_types
    from pyxsd.schema_base import SchemaBase

    if declared is SchemaBase:
        # An element declaration with no ``type`` and no inline type has
        # the ur-type as its type definition; :meth:`Element.getType`
        # stands it in as ``SchemaBase``. Every type is validly derived
        # from the ur-type, but the element's ``block`` still constrains
        # the derivation *method* used to reach the override (MS
        # particlesIg003: ``block="restriction"`` rejects an override
        # whose chain to the ur-type contains a restriction step, while
        # particlesIg002's ``block="extension"`` admits a simple type,
        # whose chain is all restrictions).
        if override is None:
            return None
        tokens = (
            frozenset(blocked) if isinstance(blocked, (set, frozenset)) else blockTokens(blocked)
        )
        if "#all" in tokens:
            return "blocked"
        if tokens:
            if _pythonDerived(override, SchemaBase):
                if _blocked_step(override, SchemaBase, tokens):
                    return "blocked"
            elif "restriction" in tokens:
                # A simple type is reached from the ur-type only through
                # restrictions (anySimpleType, anyAtomicType, ...), so a
                # restriction block forbids every simple override.
                return "blocked"
        return None
    if declared is xsd_data_types.AnyType:
        return None
    if declared is xsd_data_types.AnySimpleType:
        if override is None or override is xsd_data_types.AnyType:
            return "not-derived"
        if not _isSimpleTypeClass(override):
            return "not-derived"
        return None
    return is_validly_derived(override, declared, blocked)


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
    if not derived and derived_from_union_member(override, declared):
        # Type Derivation OK (Simple), union clause: a type is derived
        # from a union when it is (or derives from) one of the union's
        # member types. An ``xsi:type`` naming a union member (or a
        # restriction of a member) is therefore a valid override of a
        # union-typed declaration (MS elemT071/072/073).
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
