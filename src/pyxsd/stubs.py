"""Inventory of the generated classes a stub file must declare.

A compiled schema's classes are created at runtime from schema
bookkeeping, not authored as Python names: a named type keeps its XSD
local name, but an anonymous inline type is called ``shipTo|complexType``,
a strict-namespace compile registers one class under both a bare and a
Clark name, and two namespaces may declare the same local name. A stub
file needs one plain Python identifier per class.

The anti-goal is a stub that lies: this module never presents a stub
name as the schema's own when it is not one. Every name that had to be
synthesized, sanitized or disambiguated is recorded in the entry's
``note`` (the real bookkeeping name, the namespace a colliding class
belongs to), and ``er`` points at the type declaration the class was
built for whenever the component table still has it — ``None``
otherwise, never a guess. Nothing here calls ``schema.require_valid()``:
an error-bearing schema still compiles classes, and a stub may still be
requested for it.

On top of the inventory, :func:`_members` resolves one class's
renderable members: its own element and attribute descriptors in
declaration order, members attributed from a base class the inventory
does not list (an overlay's generated base), and public functions and
properties an overlay adds beyond the generated descriptors. Element
occurrence is folded through the compiled particle tree, because a
single descriptor's own ``minOccurs``/``maxOccurs`` lie whenever an
ancestor group is optional or repeated — a choice branch, for example,
is absent whenever another branch is taken. Every member carries the
annotation a stub renders and a comment recording what the annotation
cannot say (defaults, nillable, prohibited uses, unresolvable types).

Member annotations model *valid parsed documents* and degrade to ``Any``
with a comment rather than naming a type the stub cannot import: a union
member (a policy degradation — a parsed union value is an instance of
the union's own bookkeeping class, and the comment names the member
types its value space draws from), a recursive back edge (a type
re-entered while its own class resolves), and an unresolvable
declaration all become ``Any`` with the reason in the comment. A
reference to a substitution-group head keeps the head's type, takes the
``None`` arm (a member occurrence satisfies the particle without
filling the head accessor) and comments the member names the runtime
binds instead.
Constructs that reach the instance but have no member to hang on —
mixed content, wildcards, open content — become comment lines on the
class block. The member model is identical in both formatters;
``compact`` records the requested mode, and the compact renderer
rewrites annotation spellings through its own alias table afterwards.

:func:`emit_stub` renders the model as the full ``.pyi`` document: a
provenance and consumption header, imports, one class block per
inventory entry (mirroring the XSD derivation the runtime mirrors, with
simple types as their datatype subclasses and facets as comments), and
the global elements as module-level aliases. ``compact=True`` renders
the agent view instead: a two-line header (provenance and alias
legend), one line per class and per member, facet-only simple types
dropped with their facets inlined onto the members that use them, and a
single trailing comment line listing the root elements. Rendering is
total in both modes: a schema that compiled always emits, and every
construct the stub cannot type truthfully becomes ``Any`` with a
comment instead of an exception.
"""

from __future__ import annotations

import ast
import builtins
import datetime
import decimal
import inspect
import keyword
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from pyxsd import __version__, xsd_data_types
from pyxsd.integrations._shape import particle_elements
from pyxsd.namespaces import XSD_NS, clark, local_name

# ``Attribute``, ``Element`` and ``SchemaBase`` are imported inside the
# functions that use them, never at module scope: those modules sit in
# an import cycle that resolves only when
# ``element_representative`` is entered first (the order
# ``pyxsd.schema``'s own imports establish), and importing this module
# must not enter the cycle from a tag module. Every public entry point
# here takes a compiled schema, so the cycle is already resolved by
# call time.

if TYPE_CHECKING:
    from pyxsd.schema import Schema

#: The ER class names that build instance classes. Every other kind in
#: the component table (elements, groups, facets, ...) is not a class.
_TYPE_ER_NAMES = ("SimpleType", "ComplexType")

#: ``Element.getMaxOccurs`` spells "unbounded" as this sentinel; the
#: compiled particle tree normalizes it to ``None``.
_UNBOUNDED = 99999

#: The XSD builtin data types by identity, mapped to the name a stub
#: annotates them with. ``pyxsd.xsd_data_types`` gives every builtin
#: declaration a ``name`` attribute of its own; the private helper
#: classes in the module do not, which is how they are told apart.
#: Builtins have no ``schema.classes`` entry, so the inventory cannot
#: name them; their class name is the importable stub name.
_BUILTIN_STUB_NAMES: dict[type, str] = {
    cls: cls.__name__
    for cls in vars(xsd_data_types).values()
    if isinstance(cls, type)
    and issubclass(cls, xsd_data_types.XsdDataType)
    and "name" in cls.__dict__
}

#: An alphanumeric run; ``-`` and ``.`` (legal in XSD names) separate
#: words in a synthesized name.
_WORD = re.compile(r"[^\W_]+")

#: The typing generics an overlay signature may name, bare or as a
#: ``typing.`` spelling. They are the ones the full stub's import block
#: can bind: a bare name is imported when the rendered body uses it, and
#: a ``typing.X`` spelling is rewritten to the bare ``X``
#: (:func:`_validated_signature`). Anything else -- ``typing.TypedDict``
#: included -- is not in the contract and degrades the signature.
_TYPING_NAMES = frozenset(
    {
        "Any",
        "List",
        "Dict",
        "Optional",
        "Sequence",
        "Iterable",
        "Iterator",
        "Mapping",
        "Set",
        "FrozenSet",
        "Tuple",
        "Union",
        "Callable",
        "Literal",
        "Type",
    }
)

#: The standard library modules an overlay signature may qualify names
#: with, mapped to the module the validator resolves the attribute
#: against. The renderer emits ``import <module>`` exactly when the
#: rendered body uses the qualifier, matching the ``datetime.``
#: precedent.
_QUALIFIER_MODULES: dict[str, Any] = {"datetime": datetime, "decimal": decimal}

#: The names the full stub's import block can bind without the body
#: referencing them: ``SchemaBase`` is imported unconditionally, and a
#: datatype or typing name's own alias text counts as a body reference,
#: so a root element named after one of these would duplicate a binding.
#: The qualifier modules are absent: their import depends on the body,
#: which their root alias is re-checked against after rendering.
_IMPORTED_STUB_NAMES = (
    frozenset({"SchemaBase"}) | frozenset(_BUILTIN_STUB_NAMES.values()) | _TYPING_NAMES
)


@dataclass(frozen=True)
class _StubClass:
    """One generated class and the stub name it renders under.

    - ``cls`` - the generated class object.
    - ``name`` - the stub-safe class name: a valid Python identifier,
      unique within one inventory result. Renderers must read a
      class's own name as ``cls.__name__``: a schema element literally
      named ``name`` replaces the ``name`` attribute in the generated
      class namespace with a descriptor.
    - ``note`` - what had to change for ``name`` to be usable (the real
      bookkeeping name, a sanitization, a collision and the namespace
      involved), or ``None`` when the class's own name already is the
      stub name.
    - ``er`` - the matching type element representative, when the
      component table still has one; ``None`` for classes with no table
      entry (overlays, builtins).
    """

    cls: type
    name: str
    note: str | None = None
    er: Any | None = None


def _inventory(schema: Schema) -> list[_StubClass]:
    """Returns one entry per generated class, in ``schema.classes`` order.

    The ``"schema"`` root-dispatch class is skipped, and a class
    registered under several keys (bare and Clark) is listed once, at
    its first key's position. Names are unique: a later class wanting a
    name already taken gets a deterministic suffix and a note saying
    why. The result is deterministic for a given schema.
    """
    grouped: list[tuple[type, list[str]]] = []
    keys_by_class: dict[int, list[str]] = {}
    for key, cls in schema.classes.items():
        # Only class objects name a stub; the root dispatcher is keyed
        # "schema", and anything else is not renderable.
        if key == "schema" or not isinstance(cls, type):
            continue
        keys = keys_by_class.get(id(cls))
        if keys is None:
            keys = []
            keys_by_class[id(cls)] = keys
            grouped.append((cls, keys))
        keys.append(key)

    type_ers = _type_ers_by_name(schema)
    used: dict[str, _StubClass] = {}
    entries: list[_StubClass] = []
    for cls, keys in grouped:
        er = _match_type_er(type_ers.get(cls.__name__, []), keys)
        base, facts = _base_stub_name(cls.__name__)
        name = base
        earlier = used.get(name)
        if earlier is not None:
            if earlier.cls.__name__ == cls.__name__:
                # The strict-mode case: two namespaces declare the same
                # local name, so the namespace is the only distinguisher.
                name = _suffixed(name, used, "_ns")
                facts.append(_namespace_note(er))
            else:
                # Two different bookkeeping names folded to one
                # identifier by synthesis or sanitization.
                name = _suffixed(name, used, "_")
                facts.append(f"name collision; an earlier class already uses {base!r}")
        entry = _StubClass(cls=cls, name=name, note="; ".join(facts) if facts else None, er=er)
        used[name] = entry
        entries.append(entry)
    return entries


def _base_stub_name(bookkeeping: str) -> tuple[str, list[str]]:
    """Returns the class's base stub name and the note facts behind it.

    An anonymous inline type's ``element|tag`` bookkeeping name is
    synthesized from the element part (PascalCase); anything else is
    taken as-is. Either way the result is sanitized to a Python
    identifier, and every deviation from the class's own name is
    returned as a fact for the entry's note.
    """
    candidate = bookkeeping
    facts: list[str] = []
    if "|" in bookkeeping:
        local = bookkeeping.split("|", 1)[0]
        candidate = _pascal_case(local)
        facts.append(f"bookkeeping name {bookkeeping!r}; stub name synthesized")
    sanitized = _sanitize(candidate)
    if sanitized != candidate:
        if keyword.iskeyword(candidate):
            facts.append(f"Python keyword {candidate!r}; stub name adjusted")
        else:
            facts.append(f"sanitized from {candidate!r}")
    if not sanitized:
        sanitized = "_"
        facts.append("no usable characters in the name")
    return sanitized, facts


def _pascal_case(local: str) -> str:
    """Returns the words of *local*, each capitalized and joined.

    ``shipTo`` becomes ``ShipTo``; separators that are legal in an XSD
    name but not in a Python identifier (``-``, ``.``) are word
    boundaries, so ``ship-to`` also becomes ``ShipTo``.
    """
    return "".join(word[:1].upper() + word[1:] for word in _WORD.findall(local))


def _sanitize(name: str) -> str:
    """Returns *name* as a valid Python identifier, ``_`` standing in.

    Every character that cannot appear in an identifier is replaced
    with ``_``; a leading digit is kept and the name prefixed with
    ``_``, so the result stays recognizable (``9lives`` → ``_9lives``).
    A Python keyword gets a trailing ``_`` (PEP 8's convention for
    keyword clashes, e.g. ``class`` → ``class_``): keywords are legal
    XSD names but not usable class names.
    """
    sanitized = "".join(ch if ("_" + ch).isidentifier() else "_" for ch in name)
    if sanitized[:1].isdigit():
        sanitized = "_" + sanitized
    if keyword.iskeyword(sanitized):
        sanitized += "_"
    return sanitized


def _suffixed(base: str, used: dict[str, _StubClass], marker: str) -> str:
    """Returns the first ``base + marker + n`` (n from 2) not yet used."""
    counter = 2
    while f"{base}{marker}{counter}" in used:
        counter += 1
    return f"{base}{marker}{counter}"


def _type_ers_by_name(schema: Schema) -> dict[str, list[Any]]:
    """Indexes the table's type representatives by their bookkeeping name.

    The name used here is the representative's own ``name`` attribute —
    the value the generated class was created from — not ``getName()``:
    for an anonymous inline type ``getName()`` re-derives the name
    against the now-populated component table and returns a suffixed
    bookkeeping variant that no class is named after.
    """
    by_name: dict[str, list[Any]] = {}
    seen: set[int] = set()
    for representatives in schema.components.values():
        for er in representatives:
            if type(er).__name__ not in _TYPE_ER_NAMES or id(er) in seen:
                continue
            seen.add(id(er))
            by_name.setdefault(er.name, []).append(er)
    return by_name


def _match_type_er(candidates: list[Any], keys: list[str]) -> Any | None:
    """Returns the type ER the class was built for, or ``None``.

    A class's ``schema.classes`` keys identify it: a strict-namespace
    compile registers the class under its bare name and its Clark name,
    and two same-local-name classes from different namespaces are told
    apart by their Clark keys. A candidate whose expanded name is among
    the keys is the class's own declaration; when several candidates
    remain and none is identified, ``None`` is returned rather than a
    guess.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    key_set = set(keys)
    for er in candidates:
        if clark(er.getNamespace(), er.name) in key_set:
            return er
    return None


def _namespace_note(er: Any | None) -> str:
    """Returns a collision note naming the class's XSD namespace."""
    namespace = er.getNamespace() if er is not None else None
    if isinstance(namespace, str) and namespace:
        return f"name collision; XSD namespace {namespace!r}"
    return "name collision; XSD namespace absent"


# ---------------------------------------------------------------------------
# Member annotation resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Member:
    """One class-body member a stub renders.

    - ``name`` - the runtime accessor name (the descriptor's bound
      class-attribute key, or the overlay member's name). Empty for a
      member that has no usable accessor; the renderer emits those as a
      comment line built from ``comment``.
    - ``annotation`` - the annotation text a stub renders. For element
      and attribute members it is a union of stub type names
      (``Int | str | None``, ``list[Item]``). For extension members it
      is the text following the member name in its ``def`` line
      (``(self, count: int) -> str``), so a renderer emits
      ``def {name}{annotation}`` without re-introspecting.
    - ``kind`` - what the member is: a schema element, a schema
      attribute, or a member an overlay added beyond the descriptors.
    - ``comment`` - what the annotation cannot say (a default reads as
      ``str`` when absent, the element is nillable, an attribute is
      prohibited, the type could not be resolved, the XML name an
      aliased member belongs to), or ``None``.
    - ``is_property`` - True when an overlay bound the member with
      ``@property``; a renderer emits a decorator line and a
      parameterless ``def`` instead of a plain method. The
      ``annotation`` still carries the getter's signature text, so the
      discriminator only selects the spelling.
    """

    name: str
    annotation: str
    kind: Literal["element", "attribute", "extension"]
    comment: str | None = None
    is_property: bool = False


def _members(cls: type, names: dict[int, str], *, compact: bool) -> list[_Member]:
    """Returns ``cls``'s stub-renderable members, in declaration order.

    ``names`` is the class-name resolution map a renderer builds from
    :func:`_inventory` (``{id(entry.cls): entry.name}``): a member's
    type class is annotated with its stub name when the inventory has
    one, a builtin data type's own name when it is one, and ``Any``
    with a comment otherwise.

    The walk is over ``cls.__mro__`` up to ``SchemaBase``. The first
    (most derived) binding of a name wins, and a member whose defining
    class the inventory lists is *not* re-rendered here: its own class
    block renders it, and re-listing it would invent a second accessor.
    A member whose defining class is not in the inventory (an overlay's
    generated base) is attributed to ``cls``, so it still reaches the
    stub. Elements come first, then attributes, then the overlay's own
    public functions and properties, each in declaration order
    (base-class declarations before derived ones, as XSD extension
    order).

    Members that cannot be annotated truthfully are degraded, never
    skipped: an unresolvable type, a union member's value and a
    recursive back edge annotate as ``Any`` with a comment, a
    prohibited attribute use and a name with no usable Python
    accessor become comment-only members, and a member accessor that
    raises is treated as absent rather than propagating. ``compact``
    records the requested formatter; the member model is identical in
    both modes, and the compact renderer maps the annotations' spellings
    to its alias table itself.

    Nothing here calls ``schema.require_valid()``: an error-bearing
    schema still compiles classes, and a stub may still be requested
    for it.
    """
    # Deferred so importing this module does not enter the ER tag
    # cycle from a tag module (see the module-level comment).
    from pyxsd.element_representatives.attribute import Attribute
    from pyxsd.element_representatives.element import Element
    from pyxsd.schema_base import SchemaBase

    model = getattr(cls, "_contentModel_", None)
    inventory = set(names)
    class_names = frozenset(names.values())
    candidates: dict[str, tuple[int, int, int, _Member]] = {}
    #: An inventory class at or below a defining class renders that
    #: class's members itself; once one is met, later (less derived)
    #: classes' descriptors are skipped instead of duplicated.
    blocked = False
    for depth, klass in enumerate(cls.__mro__):
        if klass is SchemaBase or klass is object:
            break
        in_inventory = id(klass) in inventory
        renders = depth == 0 or (not blocked and not in_inventory)
        if depth > 0 and in_inventory:
            blocked = True
        if not renders:
            continue
        #: The class declarations open while this class's members
        #: resolve; a member type re-entering one is a recursive back
        #: edge and degrades to ``Any`` (see :func:`_annotation_base`).
        active = frozenset({id(cls), id(klass)})
        for order, key in enumerate(klass.__dict__.get("_elementNames_", ())):
            descriptor = klass.__dict__.get(key)
            if not isinstance(descriptor, Element):
                continue  # shadowed by a later binding in the same body
            member = _element_member(key, descriptor, names, model, active)
            candidates.setdefault(key, (0, -depth, order, member))
        for order, key in enumerate(klass.__dict__.get("_attributeNames_", ())):
            descriptor = klass.__dict__.get(key)
            if not isinstance(descriptor, Attribute):
                continue
            member = _attribute_member(key, descriptor, names, active)
            candidates.setdefault(key, (1, -depth, order, member))
    # Only the class's own body contributes overlay members: a builtin
    # data type further up the MRO declares public methods too
    # (``lexical``, ``to_date``, ...), and those are base-library API,
    # not members an overlay added.
    for order, (key, value) in enumerate(cls.__dict__.items()):
        if key.startswith("_") or key in candidates:
            continue
        if not isinstance(value, property) and not inspect.isfunction(value):
            continue
        candidates[key] = (2, 0, order, _extension_member(key, value, class_names))
    return [member for _, _, _, member in sorted(candidates.values(), key=lambda item: item[:3])]


def _element_member(
    key: str, descriptor: Any, names: dict[int, str], model: Any, active: frozenset[int]
) -> _Member:
    """Returns the ``_Member`` for one element descriptor bound as *key*.

    Occurs are folded through *model* (the owning class's
    ``_contentModel_``), so an optional or repeated ancestor group
    reaches the annotation. A ``default`` or ``fixed`` value appends
    ``str`` and ``None`` to the union. The comment distinguishes where
    the value constraint lives: a default on the declaration itself is
    what makes its absent read the raw lexical string, while a
    reference site delegates the constraint to the referred declaration
    and still reads ``None`` when absent (the site carries no fallback
    of its own). A reference to a substitution-group head keeps the
    head's type annotation, gains the ``None`` arm (a member occurrence
    satisfies the head's particle without filling the head accessor),
    and comments the member names the runtime binds instead.
    ``nillable`` is a comment, not a union arm: the value class itself
    is unchanged.
    """
    xml_name = _xml_name(descriptor, key)
    if not _usable_identifier(key):
        return _Member(
            name="",
            annotation="",
            kind="element",
            comment=f"element {xml_name!r} is not dot-accessible (bound as {key!r})",
        )
    parts: list[str] = []
    base, problem = _annotation_base(descriptor, xml_name, names, active)
    if problem is not None:
        parts.append(problem)
    substitution = _substitution_comment(descriptor)
    if substitution is not None:
        parts.append(substitution)
    minimum, maximum = _effective_occurs(model, descriptor)
    union = [f"list[{base}]" if maximum is None or maximum > 1 else base]
    own_default = _lexical(descriptor, "default")
    default = _call(descriptor, "getDefault")
    fixed = _call(descriptor, "getFixed")
    if default is not None:
        union += ["str", "None"]
        if own_default is not None:
            parts.append(f"default {own_default!r} reads as str when absent")
        else:
            parts.append(f"default {str(default)!r} from the referenced declaration")
    elif fixed is not None:
        union += ["str", "None"]
        parts.append(f"fixed {_lexical(descriptor, 'fixed', fixed)!r}")
    elif minimum == 0:
        union.append("None")
    if substitution is not None and "None" not in union:
        # A substituting member occurrence satisfies the head's particle
        # without filling the head accessor (members bind under their
        # own names), so None is reachable even when the head itself is
        # required.
        union.append("None")
    if _call(descriptor, "isNillable") is True:
        parts.append("nillable")
    if xml_name != key:
        parts.append(f"xml: {xml_name!r}")
    return _Member(
        name=key,
        annotation=" | ".join(union),
        kind="element",
        comment="; ".join(parts) if parts else None,
    )


def _attribute_member(
    key: str, descriptor: Any, names: dict[int, str], active: frozenset[int]
) -> _Member:
    """Returns the ``_Member`` for one attribute descriptor bound as *key*.

    A prohibited use has no annotation at all: the attribute is not
    part of a valid document, so its only stub presence is a comment.
    ``use="required"`` annotates as the type alone; anything else adds
    ``None``. ``default``/``fixed`` append ``str`` and ``None``, the
    same as elements: an absent attribute reads back the lexical string
    (before the binder converts it), and an unbound instance reads
    ``None``.
    """
    xml_name = _xml_name(descriptor, key)
    if _call(descriptor, "getUse") == "prohibited":
        return _Member(
            name="",
            annotation="",
            kind="attribute",
            comment=f"attribute {xml_name!r} is prohibited",
        )
    if not _usable_identifier(key):
        return _Member(
            name="",
            annotation="",
            kind="attribute",
            comment=f"attribute {xml_name!r} is not dot-accessible (bound as {key!r})",
        )
    parts: list[str] = []
    base, problem = _annotation_base(descriptor, xml_name, names, active)
    if problem is not None:
        parts.append(problem)
    union = [base]
    default = _call(descriptor, "getDefault")
    fixed = _call(descriptor, "getFixed")
    if default is not None:
        union += ["str", "None"]
        parts.append(
            f"default {_lexical(descriptor, 'default', default)!r} reads as str when absent"
        )
    elif fixed is not None:
        union += ["str", "None"]
        parts.append(f"fixed {_lexical(descriptor, 'fixed', fixed)!r}")
    if _call(descriptor, "getUse") != "required" and "None" not in union:
        union.append("None")
    if xml_name != key:
        parts.append(f"xml: {xml_name!r}")
    return _Member(
        name=key,
        annotation=" | ".join(union),
        kind="attribute",
        comment="; ".join(parts) if parts else None,
    )


def _extension_member(key: str, value: Any, class_names: frozenset[str]) -> _Member:
    """Returns the ``_Member`` for an overlay's function or property.

    The annotation is the ``def`` text after the member name, read from
    ``inspect.signature`` (a property is introspected through its
    getter; its setter's asymmetry is out of a stub's reach).
    ``is_property`` records that the overlay bound the name with
    ``@property``, so the renderer emits the decorator form. An
    unannotated return becomes ``Any``; a signature that cannot be read
    degrades to a variadic ``Any`` signature with a comment.

    A signature whose annotations name identifiers the emitted document
    cannot resolve degrades to ``(self) -> Any`` with the identifier in
    the comment (:func:`_validated_signature`): the document binds the
    pyxsd imports, the rendered classes (*class_names*), the typing
    generics, the qualifier modules, and the Python builtins, and
    nothing else.
    """
    is_property = isinstance(value, property)
    target = value.fget if is_property else value
    signature = None
    if target is not None:
        try:
            signature = str(inspect.signature(target))
        except (TypeError, ValueError):
            signature = None
    if signature is None:
        return _Member(
            name=key,
            annotation="(self, *args, **kwargs) -> Any",
            kind="extension",
            comment="signature unavailable; annotated Any",
            is_property=is_property,
        )
    comment = None
    if " -> " not in signature:
        signature += " -> Any"
        if is_property:
            comment = "getter return unannotated; annotated Any"
    signature, problem = _validated_signature(signature, class_names)
    if problem is not None:
        return _Member(
            name=key,
            annotation="(self) -> Any",
            kind="extension",
            comment=problem,
            is_property=is_property,
        )
    return _Member(
        name=key,
        annotation=signature,
        kind="extension",
        comment=comment,
        is_property=is_property,
    )


def _validated_signature(signature: str, class_names: frozenset[str]) -> tuple[str, str | None]:
    """Returns the signature text the emitted document can resolve, or a problem.

    ``inspect.signature`` formats annotations through their defining
    modules (``decimal.Decimal``, ``List[str]``, a quoted forward
    reference), so text copied into the document verbatim can name
    identifiers nothing there binds (mypy's ``name is not defined``).
    Only resolvable identifiers survive:

    - a Python builtin (``str``, ``int``, ``list``, ...), ``Any``, and
      the always-imported ``SchemaBase``;
    - a bare typing generic and a rendered class name, both module-level
      bindings of the emitted document;
    - a ``typing.X`` spelling, rewritten to the bare ``X`` the emitted
      ``from typing import`` line binds; and
    - a ``datetime.``/``decimal.`` qualifier whose first attribute
      exists, resolvable through the ``import`` line the renderer emits
      for the qualifier.

    Everything else -- an unknown bare name, a foreign module qualifier
    such as ``pandas.Series``, a quoted string annotation -- is
    unresolvable: ``(signature, problem)`` returns the problem text for
    the caller to degrade the member to ``(self) -> Any`` with. A
    signature that cannot even parse (a default value's repr is not
    always an expression) degrades the same way. Never raises.
    """
    try:
        module = ast.parse(f"def _f{signature}: ...")
    except (SyntaxError, ValueError):
        return signature, "signature could not be parsed; annotated Any"
    function = module.body[0] if module.body else None
    if not isinstance(function, ast.FunctionDef):
        return signature, "signature could not be parsed; annotated Any"
    problem: str | None = None
    rewrites: list[tuple[int, int, str]] = []
    for annotation in _signature_annotations(function):
        found = _unresolvable_annotation(annotation, class_names, rewrites)
        if found is not None and problem is None:
            problem = found
    if problem is not None:
        return signature, f"unresolvable identifier {problem!r} in signature; annotated Any"
    if not rewrites:
        return signature, None
    rewritten = _rewrite_typing_spellings(signature, rewrites)
    if rewritten is None:  # pragma: no cover - inspect.signature renders one ASCII line
        name = f"typing.{rewrites[0][2]}"
        return signature, f"unresolvable identifier {name!r} in signature; annotated Any"
    return rewritten, None


def _signature_annotations(function: ast.FunctionDef) -> list[ast.expr]:
    """Returns the annotation expressions of one parsed signature."""
    arguments = function.args
    annotations = [
        argument.annotation
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
        if argument.annotation is not None
    ]
    annotations.extend(
        argument.annotation
        for argument in (arguments.vararg, arguments.kwarg)
        if argument is not None and argument.annotation is not None
    )
    if function.returns is not None:
        annotations.append(function.returns)
    return annotations


def _resolvable_bare_name(name: str, class_names: frozenset[str]) -> bool:
    """Returns True when the emitted document binds *name* as an annotation."""
    return (
        name == "SchemaBase"
        or name in class_names
        or name in _TYPING_NAMES
        or hasattr(builtins, name)
    )


def _unresolvable_annotation(
    node: ast.expr,
    class_names: frozenset[str],
    rewrites: list[tuple[int, int, str]],
) -> str | None:
    """Returns the first identifier *node* cannot resolve, or ``None``.

    A resolvable ``typing.X`` spelling is recorded in *rewrites* (the
    node's source span and the bare name) for
    :func:`_rewrite_typing_spellings` to normalize once the whole
    signature is known to be sound. ``Literal``'s arguments are values,
    not annotations, so their string literals do not degrade the union
    they appear in; any other quoted string is a forward reference the
    emitted document cannot see. Shapes the walk does not model are
    named by their unparsed source rather than passed through.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        return None
    if isinstance(node, ast.Name):
        if _resolvable_bare_name(node.id, class_names):
            return None
        return node.id
    if isinstance(node, ast.Attribute):
        return _attribute_problem(node, rewrites)
    if isinstance(node, ast.Subscript):
        problem = _unresolvable_annotation(node.value, class_names, rewrites)
        if problem is not None:
            return problem
        if _is_literal_spelling(node.value):
            return None
        return _unresolvable_annotation(node.slice, class_names, rewrites)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        parts: tuple[ast.expr, ...] = (node.left, node.right)
    elif isinstance(node, (ast.Tuple, ast.List)):
        parts = tuple(node.elts)
    else:
        return ast.unparse(node)
    for part in parts:
        problem = _unresolvable_annotation(part, class_names, rewrites)
        if problem is not None:
            return problem
    return None


def _attribute_problem(node: ast.Attribute, rewrites: list[tuple[int, int, str]]) -> str | None:
    """Returns the problem for one dotted annotation, or ``None``.

    A one-attribute chain qualified by ``typing`` is a spelling
    (:data:`_TYPING_NAMES`) the import block can bind, and is recorded
    in *rewrites*; one qualified by a qualifier module
    (:data:`_QUALIFIER_MODULES`) resolves when the attribute exists on
    the real module. Anything deeper or else qualifies -- ``pandas.
    Series``, ``typing.TypedDict``, ``datetime.no_such_thing`` -- is
    named by its unparsed source.
    """
    attributes: list[str] = []
    root: ast.expr = node
    while isinstance(root, ast.Attribute):
        attributes.append(root.attr)
        root = root.value
    if not isinstance(root, ast.Name) or len(attributes) != 1:
        return ast.unparse(node)
    end = node.end_col_offset
    if root.id == "typing" and attributes[0] in _TYPING_NAMES and end is not None:
        rewrites.append((node.col_offset, end, attributes[0]))
        return None
    module = _QUALIFIER_MODULES.get(root.id)
    if module is not None and hasattr(module, attributes[0]):
        return None
    return ast.unparse(node)


def _is_literal_spelling(node: ast.expr) -> bool:
    """Returns True when *node* spells ``Literal`` or ``typing.Literal``."""
    if isinstance(node, ast.Name):
        return node.id == "Literal"
    return isinstance(node, ast.Attribute) and node.attr == "Literal"


def _rewrite_typing_spellings(signature: str, rewrites: list[tuple[int, int, str]]) -> str | None:
    """Returns *signature* with the recorded ``typing.X`` spans replaced.

    The recorded offsets are relative to ``"def _f" + signature``; they
    are trusted only for the single-line ASCII text ``inspect.signature``
    renders, and every span is checked to be the spelling it claims
    before the splice, so an offset that would cut the text is ``None``
    for the caller to degrade instead of a mangled signature.
    """
    if not signature.isascii() or "\n" in signature:  # pragma: no cover - one ASCII line
        return None
    prefix = len("def _f")
    text = signature
    for start, end, name in sorted(rewrites, reverse=True):
        low, high = start - prefix, end - prefix
        if signature[low:high] != f"typing.{name}":
            return None
        text = text[:low] + name + text[high:]
    return text


def _effective_occurs(model: Any, descriptor: Any) -> tuple[int, int | None]:
    """Returns the descriptor's effective ``(min, max)`` occurrence.

    ``model`` is the owning class's compiled ``_contentModel_``
    particle tree; the walk mirrors
    :func:`pyxsd.integrations._projection._edge_nullable`, folding every
    ancestor between the root and the descriptor's leaf particle: an
    ancestor with ``min_occurs == 0`` or ``kind == "choice"`` forces
    the effective minimum to 0, and an ancestor with ``max_occurs is
    None`` or ``> 1`` forces the effective maximum to ``None``
    (unbounded). A descriptor with no leaf particle — a class whose
    content model did not compile, or a member inherited from a class
    outside the model — falls back to the descriptor's own occurrence
    values. Never raises.
    """
    minimum, maximum = _declared_occurs(descriptor)
    if model is None:
        return minimum, maximum
    edge = _leaf_particle(model, descriptor)
    if edge is None:
        return minimum, maximum
    return _fold_occurs(model, edge, edge.min_occurs, edge.max_occurs)


def _fold_occurs(
    particle: Any, edge: Any, minimum: int, maximum: int | None
) -> tuple[int, int | None]:
    """Folds one ancestor particle's occurrence into the edge's."""
    if particle.min_occurs == 0 or particle.kind == "choice":
        minimum = 0
    if particle.max_occurs is None or particle.max_occurs > 1:
        maximum = None
    if particle is edge:
        return minimum, maximum
    for child in particle.children:
        if any(leaf is edge for leaf in particle_elements(child)):
            return _fold_occurs(child, edge, minimum, maximum)
    return minimum, maximum


def _leaf_particle(model: Any, descriptor: Any) -> Any | None:
    """Returns the element particle compiled for *descriptor*, or ``None``.

    A class-body descriptor is the declaration the class declares, but
    two compilation steps produce particles that carry *different*
    objects:

    - an element reference site compiles to a particle carrying the
      *referred* declaration, so the site is matched through
      ``referredElement``; and
    - a named group's expansion binds a per-use shallow copy into the
      class body while the particle tree carries the group's shared
      declarations, so the copy is matched through the shared
      ``xsdElement`` source node it was made from.

    When several leaves can carry the declaration (a group referenced
    twice, a reference site beside a group expansion), the descriptor's
    own occurrence identifies its leaf: a copy's occurrence is the
    product of the group references that contributed it, which is
    exactly :func:`_copy_signature`. A site's own occurrence is the
    particle's own (``_compile_element`` takes ``minOccurs``/``maxOccurs``
    from the site), the fallback tie-break.
    """
    leaves = [particle for particle in particle_elements(model) if particle.descriptor is not None]
    for leaf in leaves:
        if leaf.descriptor is descriptor:
            return leaf
    if getattr(descriptor, "isElementRef", False):
        referred = getattr(descriptor, "referredElement", None)
        if referred is None:
            return None
        candidates = [leaf for leaf in leaves if leaf.descriptor is referred]
        return _preferred_leaf(model, candidates, descriptor)
    return _preferred_leaf(model, _twin_leaves(leaves, descriptor), descriptor)


def _twin_leaves(leaves: list[Any], descriptor: Any) -> list[Any]:
    """Returns the leaves carrying the declaration *descriptor* was copied from.

    ``ComplexType._flattenGroupRef`` binds each group reference's own
    shallow copies of the group's declarations, so the class body and
    the particle tree hold distinct objects for one declaration. The
    copy shares the source ``xsdElement`` node with the shared
    declaration the particle carries, which is the exact link
    descriptor identity cannot see. A descriptor without a source node
    (a hand-written declaration) has no copy twin.
    """
    source = getattr(descriptor, "xsdElement", None)
    if source is None:
        return []
    return [leaf for leaf in leaves if getattr(leaf.descriptor, "xsdElement", None) is source]


def _preferred_leaf(model: Any, candidates: list[Any], descriptor: Any) -> Any | None:
    """Returns the leaf that carries *descriptor*'s occurrence, or ``None``.

    One candidate is the declaration's only particle. With several, the
    descriptor's own occurrence picks the leaf its declaration was
    compiled for: ``_copy_signature`` reproduces a group-expanded
    copy's occurrence exactly, and a direct (unwrapped) site's own
    occurrence is its particle's. The first candidate is the last
    resort, as before.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    minimum, maximum = _declared_occurs(descriptor)
    for leaf in candidates:
        if _copy_signature(model, leaf) == (minimum, maximum):
            return leaf
    for leaf in candidates:
        if leaf.min_occurs == minimum and leaf.max_occurs == maximum:
            return leaf
    return candidates[0]


def _copy_signature(model: Any, edge: Any) -> tuple[int, int | None]:
    """Returns the occurrence a group-expanded copy of *edge* carries.

    ``ComplexType._foldRefOccurrences`` multiplies a group reference
    site's occurrence into every element the reference contributes, and
    the compiled tree keeps that occurrence on the synthetic wrapper
    particle the reference compiles to (``_compile_group_ref``).
    Multiplying the synthetic wrappers and the element's own occurrence
    therefore reproduces the copy's own ``minOccurs``/``maxOccurs``
    exactly, normalized the way :func:`_declared_occurs` normalizes
    them.
    """
    return _signature(model, edge, 1, 1)


def _signature(
    particle: Any, edge: Any, minimum: int, maximum: int | None
) -> tuple[int, int | None]:
    """Multiplies the synthetic ancestors' occurrences into *edge*'s own."""
    if particle.synthetic:
        minimum, maximum = _multiplied(minimum, maximum, particle.min_occurs, particle.max_occurs)
    if particle is edge:
        minimum, maximum = _multiplied(minimum, maximum, edge.min_occurs, edge.max_occurs)
        return minimum, None if maximum is None or maximum >= _UNBOUNDED else maximum
    for child in particle.children:
        if any(leaf is edge for leaf in particle_elements(child)):
            return _signature(child, edge, minimum, maximum)
    return minimum, maximum


def _multiplied(
    minimum: int, maximum: int | None, factor_min: int, factor_max: int | None
) -> tuple[int, int | None]:
    """Multiplies one occurrence factor in, capping like the ref fold does."""
    minimum *= factor_min
    if maximum is None or factor_max is None:
        return minimum, None
    maximum *= factor_max
    if maximum >= _UNBOUNDED:
        return minimum, None
    return minimum, maximum


def _declared_occurs(descriptor: Any) -> tuple[int, int | None]:
    """Returns the descriptor's own occurrence, unbounded normalized."""
    try:
        minimum = int(descriptor.getMinOccurs())
    except Exception:
        minimum = 1
    try:
        maximum = descriptor.getMaxOccurs()
    except Exception:
        maximum = 1
    if maximum is None or maximum >= _UNBOUNDED:
        return minimum, None
    return minimum, int(maximum)


def _annotation_base(
    descriptor: Any,
    xml_name: str,
    names: dict[int, str],
    active: frozenset[int] = frozenset(),
) -> tuple[str, str | None]:
    """Returns the annotation's base type name and any degradation comment.

    The resolution order is the inventory's stub name, then the builtin
    data type's own class name. A construct the stub cannot present
    directly annotates as ``Any`` with a comment saying why, never as a
    guessed class name: an unresolved declaration, the untyped
    ``anyType`` fallback, a union simple type (a policy degradation:
    the parsed value is an instance of the union's own class, and the
    comment names the member types its value space draws from), and a
    class already open on *active* — the ids of the class declarations
    currently being resolved — which is a recursive back edge.
    """
    try:
        resolved = descriptor.getType()
    except Exception:
        return "Any", f"type of {xml_name!r} could not be resolved; annotated Any"
    if resolved is None:
        declared = getattr(descriptor, "type", None)
        return "Any", f"type {declared!r} is unresolved; annotated Any"
    if isinstance(resolved, type):
        return _resolved_base(resolved, names, active)
    return "Any", f"type {getattr(resolved, '__name__', resolved)!r} is not stubbed; annotated Any"


def _resolved_base(
    cls: type, names: dict[int, str], active: frozenset[int]
) -> tuple[str, str | None]:
    """Returns the annotation base and degradation comment for one class.

    A re-entered class (``cls``'s id is on *active*) is the back edge of
    a recursive type graph. A union reads ``Any`` by policy: a parsed
    union value is an instance of the union's own bookkeeping class, and
    the stub names the member types its value space draws from in the
    comment rather than presenting that class as the member's type;
    ``_unionMembers`` is read from the class's own namespace, mirroring
    derivation: a restriction of a union inherits the attribute through
    its MRO but is not itself a union.
    """
    from pyxsd.schema_base import SchemaBase

    if id(cls) in active:
        return "Any", "recursive type; annotated Any"
    union_members = vars(cls).get("_unionMembers")
    if union_members is not None:
        return "Any", f"union of: {_union_member_names(union_members, names)}; annotated Any"
    stub_name = names.get(id(cls))
    if stub_name is not None:
        return stub_name, None
    builtin_name = _BUILTIN_STUB_NAMES.get(cls)
    if builtin_name is not None:
        return builtin_name, None
    if cls is SchemaBase:
        return "Any", "no declared type; annotated Any"
    return "Any", f"type {getattr(cls, '__name__', cls)!r} is not stubbed; annotated Any"


def _union_member_names(members: Any, names: dict[int, str]) -> str:
    """Returns a union's member classes as annotation-style names.

    Each member resolves the way a member annotation does — the
    inventory's stub name, then the builtin data type's own class name —
    and falls back to the class's own ``__name__``. The walk never
    recurses into a member that is itself a union: naming it is all the
    comment needs, and a (malformed) circular union must not loop.
    """
    parts: list[str] = []
    for member in members or ():
        if not isinstance(member, type):
            parts.append(str(member))
            continue
        name = names.get(id(member))
        if name is None:
            name = _BUILTIN_STUB_NAMES.get(member)
        if name is None:
            name = str(getattr(member, "__name__", None) or repr(member))
        parts.append(name)
    return " | ".join(parts) if parts else "no resolvable members"


def _substitution_comment(descriptor: Any) -> str | None:
    """Returns the substitution-group comment for an element reference site.

    A local ``ref`` to a global element heading a substitution group
    admits every declaration that names it as their group: the runtime
    binds such a child's value through a member-named accessor, so the
    head's annotation models the head's own declaration only. A member
    occurrence satisfies the head's particle without filling the head
    accessor, so the caller adds the ``None`` arm; the comment names the
    members the annotation cannot say. ``None`` for anything that is not
    a resolution-proven head: a plain element, a declaration whose group
    cannot be established (the schema's element table is unreachable), or
    a head that blocks ``substitution``, where no member is admissible
    at all. Never raises.
    """
    if not getattr(descriptor, "isElementRef", False):
        return None
    try:
        schema_er = descriptor.getSchema()
    except Exception:
        return None
    table = getattr(schema_er, "substitutionGroups", None) if schema_er is not None else None
    if not table:
        return None
    referred = getattr(descriptor, "referredElement", None)
    if referred is None:
        referred = descriptor
    head = getattr(referred, "name", None)
    if not isinstance(head, str) or not head:
        return None
    members = None
    for key in (head, _attribute(referred, "expandedName")):
        if isinstance(key, str) and key in table:
            members = table[key]
            break
    if members is None or _blocks_substitution(referred):
        return None
    names = [
        member.name
        for member in members
        if isinstance(getattr(member, "name", None), str) and member.name
    ]
    if names:
        return f"substitution group: {head}; members: {', '.join(names)}"
    return f"substitution group: {head}"


def _blocks_substitution(referred: Any) -> bool:
    """Returns True when a head's effective ``block`` forbids substitutes."""
    block = _call(referred, "getBlock")
    if not isinstance(block, str) or not block:
        attributes = getattr(referred, "tagAttributes", None) or {}
        block = str(attributes.get("block") or "")
    return "substitution" in block.split() or "#all" in block.split()


def _usable_identifier(name: str) -> bool:
    """Returns True when *name* can bind a class-body member in source."""
    return name.isidentifier() and not keyword.iskeyword(name)


def _xml_name(descriptor: Any, key: str) -> str:
    """Returns the descriptor's schema name, falling back to its bound key."""
    name = getattr(descriptor, "name", None)
    return name if isinstance(name, str) and name else key


def _lexical(descriptor: Any, attribute: str, fallback: Any = None) -> Any:
    """Returns the descriptor's own raw lexical value for a facet.

    The ER keeps the schema XML attribute verbatim, while
    ``getDefault``/``getFixed`` return the value already converted to
    the declared type (``3`` for ``default="3"`` on ``xs:int``) and a
    reference site delegates to the referred declaration. ``None`` when
    the declaration carries no value of its own; *fallback* otherwise.
    """
    raw = getattr(descriptor, attribute, None)
    return raw if isinstance(raw, str) else fallback


def _call(descriptor: Any, method: str) -> Any:
    """Calls a descriptor accessor, returning ``None`` when it raises."""
    accessor = getattr(descriptor, method, None)
    if accessor is None:
        return None
    try:
        return accessor()
    except Exception:
        return None


def _attribute(instance: Any, name: str) -> Any:
    """Returns ``instance.name``, or ``None`` when reading it raises.

    A ``getattr`` default only covers a missing attribute; a property
    can raise anything at all, so the read is guarded like :func:`_call`
    guards a method call, keeping the module's never-raises discipline.
    """
    try:
        return getattr(instance, name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Class-level degradations
# ---------------------------------------------------------------------------


def _class_degradations(entry: _StubClass) -> list[str]:
    """Returns the class-level comment lines for content a stub cannot show.

    These are constructs whose effect reaches the parsed instance but
    that have no member to hang on: mixed content's character data, a
    wildcard's undeclared children, an attribute wildcard's undeclared
    attributes, and XSD 1.1 open content. Both renderers print the
    returned texts as comment lines in the class body, so the omission
    is never silent and the text is identical in both modes.
    """
    cls = entry.cls
    warnings: list[str] = []
    if (
        not getattr(cls, "_elementOnly_", True)
        and getattr(cls, "_simpleContentType_", None) is None
    ):
        warnings.append("mixed content: text not modeled")
    child_flags = _child_wildcard_flags(cls)
    if "wildcard" in child_flags:
        warnings.append("admits undeclared children (wildcard)")
    if "open content" in child_flags:
        warnings.append("admits undeclared children (open content)")
    if getattr(cls, "hasWildcardAttributes_", False):
        warnings.append("admits undeclared attributes (wildcard)")
    return warnings


def _child_wildcard_flags(cls: type) -> set[str]:
    """Returns which child wildcard kinds *cls*'s content model admits.

    The schema-phase particle tree (``_contentModel_``) carries the
    declared wildcards as ``any`` particles; the instance matcher's tree
    (``_instanceContentModel_``) additionally carries a type's effective
    XSD 1.1 open content as an ``any`` particle flagged
    ``open_content``. Both are walked, each model once. Never raises.
    """
    flags: set[str] = set()
    seen: set[int] = set()
    for attribute in ("_contentModel_", "_instanceContentModel_"):
        model = getattr(cls, attribute, None)
        if model is not None and id(model) not in seen:
            seen.add(id(model))
            _collect_particle_flags(model, flags)
    return flags


def _collect_particle_flags(particle: Any, flags: set[str]) -> None:
    """Adds the wildcard kinds reachable in *particle* to *flags*."""
    if particle is None:
        return
    if getattr(particle, "kind", None) == "any":
        flags.add("open content" if getattr(particle, "open_content", False) else "wildcard")
    for child in getattr(particle, "children", ()) or ():
        _collect_particle_flags(child, flags)


# ---------------------------------------------------------------------------
# Full-mode rendering
# ---------------------------------------------------------------------------


def emit_stub(
    schema: Schema,
    *,
    compact: bool = False,
    component_ids: tuple[str, ...] | None = None,
    depth: int | None = None,
) -> str:
    """Returns the stub text for the classes *schema* compiled.

    The text is generated from the live schema at call time (the class
    table, the declarations behind it, and the compiled particle
    model), so re-emitting after an overlay is loaded shows the
    overlay's members — nothing is cached at compile time.

    Full mode is an importable-quality ``.pyi`` document: a provenance
    and consumption header, imports, one class block per generated
    class, and a root-element alias section. Annotations name the real
    runtime classes, so a member typed ``Int`` reads back a
    ``pyxsd.xsd_data_types.Int``.

    ``compact`` selects the agent-oriented compact formatter, which
    shares this module's member model: a two-line header (provenance and
    an alias legend mapping the short spellings back to the pyxsd
    classes), one line per class and per member, facet-only simple types
    folded onto the members that use them, and a trailing comment line
    listing the root elements. The compact text is a description, not
    importable Python; every annotation spelling it uses is a rendered
    class name, a pyxsd builtin's own name, or a legend alias.
    Degradation comments are shared through the model and render
    verbatim in both modes (see :func:`_render_compact`).

    ``component_ids`` selects native declaration IDs; ``depth`` bounds
    outgoing declaration dependencies (None follows all reachable ones).
    An empty selection emits no classes or root aliases. Explicitly selected
    simple types are not folded away in compact mode. Unknown IDs raise
    KeyError; negative depths raise ValueError. Selection does not modify
    the schema's class table.

    Unfiltered rendering never raises on a schema that compiled: a construct a
    stub cannot type truthfully (an unresolvable type, a member with no
    Python accessor) degrades to ``Any`` with a comment, and an
    error-bearing schema still emits. This never calls
    ``schema.require_valid()``.
    """
    inventory = _inventory(schema)
    root_elements = None
    if depth is not None and depth < 0:
        raise ValueError("depth must be non-negative")
    if component_ids is not None:
        from pyxsd.introspection import _stub_selection

        class_ids, root_elements = _stub_selection(schema, component_ids, depth)
        inventory = [entry for entry in inventory if id(entry.cls) in class_ids]
    if compact:
        return _render_compact(schema, inventory, root_elements=root_elements)
    return _render_full(schema, inventory, root_elements=root_elements)


def _render_full(
    schema: Schema, inventory: list[_StubClass], *, root_elements: frozenset[int] | None = None
) -> str:
    """Assembles the full stub document for *schema*.

    Class blocks come in inventory order; the imports are derived from
    the rendered body, so only classes the document actually names are
    imported. Root aliases are checked against the class names and the
    import bindings, and one colliding with a qualifier module's import
    is re-rendered as a comment once the body decides that import. The
    result always ends in a newline.
    """
    names = {id(entry.cls): entry.name for entry in inventory}
    class_blocks = [_render_class(entry, names) for entry in inventory]
    class_names = {entry.name for entry in inventory}
    aliases = _root_aliases(
        schema,
        names,
        class_names=frozenset(class_names),
        imported_names=_IMPORTED_STUB_NAMES,
        element_ids=root_elements,
    )
    body_parts = list(class_blocks)
    if aliases:
        body_parts.append(_render_root_section(aliases))
    body = "\n\n".join(body_parts)
    module_imports = _module_imports(body)
    retracted = _retract_shadowed_module_aliases(aliases, module_imports)
    if retracted is not None:
        aliases = retracted
        body_parts = list(class_blocks)
        if aliases:
            body_parts.append(_render_root_section(aliases))
        body = "\n\n".join(body_parts)
        module_imports = _module_imports(body)
    sections = [
        "\n".join(_header_lines(schema, _example_class(inventory, aliases))),
        "\n".join(
            _import_lines(
                _used_builtin_names(body, class_names),
                _used_typing_names(body, class_names),
                module_imports,
            )
        ),
    ]
    if body:
        sections.append(body)
    return "\n\n".join(sections) + "\n"


def _header_lines(schema: Schema, example: str) -> list[str]:
    """Returns the provenance and consumption header comment block.

    ``example`` is the class the consumption idiom names; it is a real
    class of this stub when one exists, so the idiom is copy-pasteable
    for the schema at hand.
    """
    return [
        f"# Generated by pyxsd {__version__} from {_source_label(schema.source)}."
        " Do not edit by hand.",
        "#",
        "# This file is a generated typing surface for the classes a compiled",
        "# schema creates at runtime; there is no importable module (the classes",
        "# live in ``schema.classes``). Save this stub next to your code and",
        "# import it for type checking:",
        "#     if TYPE_CHECKING:",
        f"#         from schema_stubs import {example}",
        f"#     root = cast({example}, document.root)",
        "#",
        "# Semantics: annotations model valid parsed documents. Absent optional",
        "# members read as None (including absent repeated members); required",
        "# members of invalid documents read as None too. Repeated assignment",
        "# appends one item (obj.x = item); it does not take a list. The parser",
        "# binds a complex-typed child in the parsed tree only (obj.child reads",
        "# None), while a primitive-typed child is accessor-bound; the",
        "# annotations describe the descriptor and construction API. Constructs",
        "# a stub cannot type truthfully are Any with a comment.",
    ]


def _source_label(source: Any) -> str:
    """Returns a deterministic, human-readable name for a schema source.

    A path (or a file object's ``name``) is used verbatim; an anonymous
    stream has no name to show, so it reads as ``<stream>`` rather than
    an object repr that changes from run to run.
    """
    try:
        if isinstance(source, (str, os.PathLike)):
            return str(source)
        name = getattr(source, "name", None)
        if isinstance(name, str) and name:
            return name
    except Exception:
        pass
    return "<stream>"


def _import_lines(
    builtins: list[str], typing_names: list[str], module_imports: list[str]
) -> list[str]:
    """Returns the import block a full stub document starts with.

    ``typing_names`` are the typing generics beyond ``Any`` the body
    uses; ``module_imports`` are ready-made ``import`` lines for the
    qualifier modules the body uses (``import datetime``, ...).
    """
    lines: list[str] = list(module_imports)
    lines.append(f"from typing import {', '.join(('Any', *typing_names))}")
    lines.append("")
    lines.append("from pyxsd.schema_base import SchemaBase")
    if builtins:
        lines.append(f"from pyxsd.xsd_data_types import {', '.join(builtins)}")
    return lines


def _module_imports(body: str) -> list[str]:
    """Returns the ``import`` lines for the qualifier modules *body* uses.

    A validated signature keeps a ``datetime.``/``decimal.`` qualifier
    verbatim, so the module it names is imported exactly when the
    rendered text mentions it.
    """
    return [f"import {name}" for name in _QUALIFIER_MODULES if f"{name}." in body]


def _used_builtin_names(body: str, class_names: set[str]) -> list[str]:
    """Returns the builtin data type names *body* references, sorted.

    The scan is over the rendered text so extension signatures and
    degradation comments are covered too. A builtin whose name a
    rendered class also uses is skipped: the class shadows the import,
    so importing it would only create a duplicate name. A comment that
    happens to spell a builtin adds a harmless unused import.
    """
    return sorted(
        {
            name
            for name in set(_BUILTIN_STUB_NAMES.values())
            if name not in class_names and re.search(rf"\b{re.escape(name)}\b", body)
        }
    )


def _used_typing_names(body: str, class_names: set[str]) -> list[str]:
    """Returns the typing generics *body* references beyond ``Any``, sorted.

    Resolvable overlay signatures keep their bare spellings
    (:func:`_validated_signature`), so the scan over the rendered text
    is what decides the ``from typing import`` line. A generic whose
    name a rendered class also uses is skipped like a shadowed builtin;
    a comment that spells one adds a harmless unused import.
    """
    return sorted(
        name
        for name in _TYPING_NAMES - {"Any"}
        if name not in class_names and re.search(rf"\b{re.escape(name)}\b", body)
    )


def _example_class(inventory: list[_StubClass], aliases: list[_RootAlias]) -> str:
    """Returns the class name the consumption idiom should show.

    The first root element's class is the natural example for
    ``document.root``; a schema whose roots are all builtin-typed falls
    back to its first generated class, then to the base class.
    """
    class_names = {entry.name for entry in inventory}
    for alias in aliases:
        if alias.type_name in class_names:
            return alias.type_name
    if inventory:
        return inventory[0].name
    return "SchemaBase"


def _render_class(entry: _StubClass, names: dict[int, str]) -> str:
    """Returns one class block, without a trailing newline.

    A note renders as a comment above the class; a documentation
    string and the class-level degradations
    (:func:`_class_degradations`: mixed content, wildcards, open
    content) render inside it. Members come in :func:`_members` order
    (elements, attributes, overlay extensions); a class with no
    members renders ``...``. Simple types additionally carry their
    facets as comments, which is the only place a restriction's
    constraints are visible in the stub.
    """
    lines: list[str] = []
    if entry.note:
        lines.append(f"# {entry.note}")
    lines.append(f"class {entry.name}({_stub_base(entry, names)}):")
    lines.extend(f"    # {warning}" for warning in _class_degradations(entry))
    lines.extend(_docstring_lines(entry))
    members = _members(entry.cls, names, compact=False)
    member_lines = _member_lines(members)
    lines.extend(member_lines)
    # Comment-only members (unusable accessor names) are not
    # statements, so a block that has no statement line still needs
    # ``...`` to stay syntactically valid.
    if not any(not line.lstrip().startswith("#") for line in member_lines):
        lines.append("    ...")
    if _is_simple_type(entry.cls):
        lines.extend(f"    {comment}" for comment in _facet_comments(entry))
    return "\n".join(lines)


def _stub_base(entry: _StubClass, names: dict[int, str]) -> str:
    """Returns the base class the stub block for *entry* declares.

    Mirrors the runtime MRO: the nearest ancestor the inventory lists
    is the XSD base type's stub name (collisions and all), the nearest
    builtin data type is its own class name (a simple-type wrapper
    mirrors its runtime base), and anything else renders
    ``SchemaBase``, the class every generated class is rooted in.
    """
    from pyxsd.schema_base import SchemaBase

    for base in entry.cls.__mro__[1:]:
        if base is object:
            break
        stub_name = names.get(id(base))
        if stub_name is not None:
            return stub_name
        builtin_name = _BUILTIN_STUB_NAMES.get(base)
        if builtin_name is not None:
            return builtin_name
        if base is SchemaBase:
            break
    return "SchemaBase"


def _is_simple_type(cls: type) -> bool:
    """Returns True when *cls* is a schema simple type.

    A simple type's runtime class subclasses a builtin data type
    (``xsd_data_types.XsdDataType``); its value space is the facets the
    stub renders as comments.
    """
    try:
        return issubclass(cls, xsd_data_types.XsdDataType)
    except TypeError:
        return False


def _docstring_lines(entry: _StubClass) -> list[str]:
    """Returns the class docstring lines for a documented type, or ``[]``.

    The ``Documentation`` ER stamps ``xs:documentation`` text onto the
    containing type's ER as ``__doc__``; the annotation data alone
    would lose it. Quotes and backslashes are escaped so arbitrary
    documentation text stays syntactically valid.
    """
    er = entry.er
    if er is None:
        return []
    text = er.__dict__.get("__doc__")
    if not isinstance(text, str) or not text.strip():
        return []
    escaped = text.strip().replace("\\", "\\\\").replace('"', '\\"')
    parts = escaped.splitlines() or [""]
    if len(parts) == 1:
        return [f'    """{parts[0]}"""']
    return [f'    """{parts[0]}', *(f"    {part}" for part in parts[1:]), '    """']


def _facet_items(entry: _StubClass) -> list[str]:
    """Returns the simple type's facets as ``name: value`` text items.

    Facets live on the ``xs:restriction`` children of a simple type's
    declaration; every child carrying a ``value`` attribute is one, so
    the items show what the value space constrains (``pattern``,
    ``minLength``, ``enumeration``, ...) without a facet list to keep
    up to date. A declaration shape this does not recognize yields no
    items rather than an error.
    """
    xsd_element = getattr(entry.er, "xsdElement", None)
    items: list[str] = []
    if xsd_element is None:
        return items
    try:
        restrictions = xsd_element.findall(f".//{{{XSD_NS}}}restriction")
        for restriction in restrictions:
            for facet in restriction:
                value = facet.get("value")
                if value is not None:
                    items.append(f"{local_name(facet.tag)}: {value}")
    except Exception:
        return items
    return items


def _facet_comments(entry: _StubClass) -> list[str]:
    """Returns the simple type's facets as ``# name: value`` comment lines."""
    return [f"# {item}" for item in _facet_items(entry)]


def _member_lines(members: list[_Member]) -> list[str]:
    """Returns the class-body lines for *members*.

    A comment-only member (no usable accessor) renders as a bare
    comment, an overlay extension as a one-line ``def`` (a property as
    a ``@property`` line plus a parameterless ``def``), and a
    descriptor member as a class-level annotation with its comment
    trailing.
    """
    lines: list[str] = []
    for member in members:
        if not member.name:
            if member.comment:
                lines.append(f"    # {member.comment}")
            continue
        if member.kind == "extension":
            if member.is_property:
                lines.append("    @property")
            lines.append(_extension_line(member))
            continue
        line = f"    {member.name}: {member.annotation}"
        if member.comment:
            line += f"  # {member.comment}"
        lines.append(line)
    return lines


def _extension_line(member: _Member) -> str:
    """Returns the ``def`` line for one overlay function or property.

    ``member.annotation`` carries the signature text after the member
    name (``(self) -> str``); the leading parameter list and the rest
    are split apart so the line reads as ordinary stub source. A
    property drops its parameter list — ``obj.name`` is a value, not a
    call — and keeps only the getter's return type. A signature that
    cannot be split degrades to ``(self) -> Any`` with the member's
    comment, never a syntax-breaking line.
    """
    params, returns = _split_signature(member.annotation)
    if params is None or returns is None:
        reason = member.comment or "signature could not be split; annotated Any"
        return f"    def {member.name}(self) -> Any: ...  # {reason}"
    if member.is_property:
        line = f"    def {member.name}(self) -> {returns}: ..."
    else:
        line = f"    def {member.name}{params} -> {returns}: ..."
    if member.comment:
        line += f"  # {member.comment}"
    return line


def _split_signature(signature: str) -> tuple[str | None, str | None]:
    """Splits ``(params) -> ret`` into its parameter list and return type.

    The parameter list ends at the ``)`` matching the leading ``(``, so
    annotations carrying nested parentheses (``Callable[[int], str]``)
    survive intact. Anything that is not a ``(params) -> ret`` shape
    returns ``(None, None)`` for the caller to degrade.
    """
    if not signature.startswith("("):
        return None, None
    depth = 0
    for index, char in enumerate(signature):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                params = signature[: index + 1]
                rest = signature[index + 1 :].strip()
                if not rest.startswith("->"):
                    return None, None
                returns = rest[2:].strip()
                return (params, returns) if returns else (None, None)
    return None, None


@dataclass(frozen=True)
class _RootAlias:
    """One global element's module-level alias.

    - ``name`` - the element's XML name (the alias target).
    - ``type_name`` - the stub name of the element's type class, or
      ``None`` when the alias cannot be typed (see ``comment``).
    - ``comment`` - why a ``name`` has no ``type_name`` (unresolvable
      type, a name that is not a Python identifier, a repeat, a name
      the emitted module already binds).
    """

    name: str
    type_name: str | None = None
    comment: str | None = None


def _root_aliases(
    schema: Schema,
    names: dict[int, str],
    *,
    class_names: frozenset[str] = frozenset(),
    imported_names: frozenset[str] = frozenset(),
    element_ids: frozenset[int] | None = None,
) -> list[_RootAlias]:
    """Returns the aliases for *schema*'s global element declarations.

    Global elements are the valid document roots, so the stub lists
    them after the classes. A root whose type resolves annotates as
    ``type[TheClass]``; one that cannot be typed truthfully keeps its
    name in a comment instead. The first declaration of a name wins;
    a repeat is commented rather than emitted twice. *class_names* and
    *imported_names* are the identifiers the full document already
    binds; an alias duplicating one would be a duplicate definition,
    so it is commented too (the compact renderer passes neither: its
    root line is a comment, not a binding).
    """
    aliases: list[_RootAlias] = []
    seen_elements: set[int] = set()
    used_names: set[str] = set()
    declarations: Iterable[Any]
    if element_ids is None:
        declarations = (er for group in schema.components.values() for er in group)
    else:
        from pyxsd.introspection import _index

        declarations = _index(schema).elements.values()
    for er in declarations:
        if id(er) in seen_elements or not _is_global_element(er):
            continue
        if element_ids is not None and id(er) not in element_ids:
            continue
        seen_elements.add(id(er))
        aliases.append(
            _root_alias(
                er,
                names,
                used_names,
                class_names=class_names,
                imported_names=imported_names,
            )
        )
    return aliases


def _is_global_element(er: Any) -> bool:
    """Returns True for a global ``Element`` representative."""
    if type(er).__name__ != "Element":
        return False
    try:
        return bool(er.isGlobalDeclaration())
    except Exception:
        return False


def _bound_name_comment(name: str, binding: str) -> str:
    """Returns the comment for a root name the emitted module already binds."""
    return f"root element {name!r}: name already used by {binding}"


def _root_alias(
    er: Any,
    names: dict[int, str],
    used_names: set[str],
    *,
    class_names: frozenset[str] = frozenset(),
    imported_names: frozenset[str] = frozenset(),
) -> _RootAlias:
    """Returns the alias, or the comment explaining its absence, for *er*.

    A name the emitted module already binds cannot become an alias: a
    rendered class or an import binding would be a duplicate definition
    (mypy: ``name already defined``), so the element keeps its name in
    a comment instead. Qualifier-module names are not on *imported_names*:
    their import depends on the rendered body, and
    :func:`_retract_shadowed_module_aliases` re-checks them once the
    body exists.
    """
    name = getattr(er, "name", None)
    if not isinstance(name, str) or not name:
        name = _called_name(er)
    if not name or not name.isidentifier() or keyword.iskeyword(name):
        return _RootAlias(name or "?", comment=f"root element {name!r} is not a Python identifier")
    if name in class_names:
        return _RootAlias(name, comment=_bound_name_comment(name, f"class {name!r}"))
    if name in imported_names:
        return _RootAlias(name, comment=_bound_name_comment(name, "an import"))
    if name in used_names:
        return _RootAlias(name, comment=f"root element {name!r} repeats an earlier alias")
    type_name, problem = _annotation_base(er, name, names)
    if problem is not None:
        return _RootAlias(name, comment=f"root element {name!r}: {problem}")
    used_names.add(name)
    return _RootAlias(name, type_name=type_name)


def _called_name(er: Any) -> str | None:
    """Returns ``er.getName()``, or ``None`` when it raises or is unusable."""
    try:
        name = er.getName()
    except Exception:
        return None
    return name if isinstance(name, str) and name else None


def _render_root_section(aliases: list[_RootAlias]) -> str:
    """Returns the trailing root-element section as one text block."""
    lines = ["# Root elements:"]
    for alias in aliases:
        if alias.type_name is not None:
            lines.append(f"{alias.name}: type[{alias.type_name}]")
        else:
            lines.append(f"# {alias.comment}")
    return "\n".join(lines)


def _retract_shadowed_module_aliases(
    aliases: list[_RootAlias], module_imports: list[str]
) -> list[_RootAlias] | None:
    """Returns *aliases* with module-shadowed names re-commented, or ``None``.

    A root alias named after a qualifier module the body uses
    (``datetime``, ``decimal``) would duplicate the ``import`` line the
    imports derive from the body, so it becomes the comment form. The
    check runs after the body exists because the alias line itself
    never carries the qualifier dot, so retracting it cannot change the
    body's import decision. ``None`` when nothing is shadowed.
    """
    imported = {line.removeprefix("import ") for line in module_imports}
    shadowed = {
        alias.name for alias in aliases if alias.type_name is not None and alias.name in imported
    }
    if not shadowed:
        return None
    return [
        replace(alias, type_name=None, comment=_bound_name_comment(alias.name, "an import"))
        if alias.name in shadowed
        else alias
        for alias in aliases
    ]


# ---------------------------------------------------------------------------
# Compact-mode rendering
# ---------------------------------------------------------------------------

#: The builtin data types that render under a short Python spelling. A
#: type named here aliases to the family's spelling; anything else keeps
#: its stub name (``Duration``, ``AnyType``, ...). The alias renames the
#: spelling of the same runtime type — ``str`` is a ``String`` subclass
#: instance, ``int`` an ``Integer`` one — so the legend stays truthful.
_STRING_FAMILY_NAMES = (
    "String",
    "NormalizedString",
    "Token",
    "Language",
    "Name",
    "NCName",
    "NMTOKEN",
    "ID",
    "IDREF",
    "ENTITY",
    "IDREFS",
    "ENTITIES",
    "NMTOKENS",
    "AnyURI",
    "QName",
    "NOTATION",
)
_INTEGER_FAMILY_NAMES = (
    "Integer",
    "NonPositiveInteger",
    "NegativeInteger",
    "NonNegativeInteger",
    "PositiveInteger",
    "Long",
    "Int",
    "Short",
    "Byte",
    "UnsignedLong",
    "UnsignedInt",
    "UnsignedShort",
    "UnsignedByte",
)
_TEMPORAL_ALIASES = {"Date": "date", "DateTime": "datetime", "Time": "time"}


def _compact_alias_name(stub_name: str) -> str:
    """Returns the compact spelling for one builtin data type's name."""
    if stub_name in _STRING_FAMILY_NAMES:
        return "str"
    if stub_name in _INTEGER_FAMILY_NAMES:
        return "int"
    if stub_name == "Boolean":
        return "bool"
    if stub_name in ("Double", "Float"):
        return "float"
    if stub_name == "Decimal":
        return "Decimal"
    return _TEMPORAL_ALIASES.get(stub_name, stub_name)


#: Every builtin data type's compact spelling, keyed by class identity:
#: ``String`` family → ``str``, ``Integer`` family → ``int``,
#: ``Boolean`` → ``bool``, ``Decimal`` → ``Decimal``, ``Double``/
#: ``Float`` → ``float``, ``Date``/``DateTime``/``Time`` → ``date``/
#: ``datetime``/``time``; every other class keeps its stub name.
_COMPACT_ALIASES: dict[type, str] = {
    cls: _compact_alias_name(name) for cls, name in _BUILTIN_STUB_NAMES.items()
}


@dataclass(frozen=True)
class _Spelling:
    """One compact annotation spelling and what it stands for.

    - ``text`` - the short spelling a member annotation uses.
    - ``source`` - the pyxsd class name the spelling denotes. For a
      dropped simple type this is the builtin data type the class
      derives from; for a builtin it is the class's own name. Only a
      spelling that differs from its source joins the legend (an
      unchanged name is already the pyxsd class's own spelling).
    """

    text: str
    source: str


def _render_compact(
    schema: Schema, inventory: list[_StubClass], *, root_elements: frozenset[int] | None = None
) -> str:
    """Assembles the compact agent view for *schema*.

    The document is a description, not importable Python: a two-line
    header (provenance and alias legend), one line per class and per
    member, plus the class-level degradation comment lines
    (:func:`_class_degradations`), and a trailing comment line listing
    the root elements. Facet-only simple types are dropped and their
    facets render on each member that uses them; every annotation
    spelling is a rendered class name, a pyxsd builtin's own name, or a
    legend alias, so the view cannot drift from the model. Degradation
    comments are shared text and render verbatim — the same words as
    the full mode — so a comment may name a class the compact view
    folded, which the aliased annotations around it cannot.
    """
    names = {id(entry.cls): entry.name for entry in inventory}
    entries_by_id = {id(entry.cls): entry for entry in inventory}
    aliases = _root_aliases(schema, names, element_ids=root_elements)
    root_names = {alias.type_name for alias in aliases if alias.type_name is not None}
    rendered: list[tuple[_StubClass, list[_Member]]] = []
    skipped: list[_StubClass] = []
    for entry in inventory:
        members = _members(entry.cls, names, compact=True)
        if (
            root_elements is None
            and entry.name not in root_names
            and not members
            and _foldable_simple_type(entry.cls)
        ):
            skipped.append(entry)
        else:
            rendered.append((entry, members))
    skipped_ids = {id(entry.cls) for entry in skipped}
    spellings = _compact_spellings(inventory, skipped)
    pattern = _spelling_pattern(spellings)
    facets_by_name = {
        entry.name: _compact_facets(entry, entries_by_id, skipped_ids) for entry in skipped
    }
    used: dict[str, list[str]] = {}

    def spell(text: str) -> str:
        return _spell_text(text, pattern, spellings, facets_by_name, used)[0]

    blocks: list[str] = []
    for entry, members in rendered:
        member_lines = _compact_member_lines(members, pattern, spellings, facets_by_name, used)
        facets = _compact_facets(entry, entries_by_id, skipped_ids)
        warnings = _class_degradations(entry)
        blocks.append(
            _compact_class_block(
                entry, spell(_stub_base(entry, names)), member_lines, facets, warnings
            )
        )
    roots_line = _compact_roots(aliases, spell)
    legend = ", ".join(f"{text}={'+'.join(sources)}" for text, sources in used.items())
    sections = [
        "\n".join(
            [
                f"# Generated by pyxsd {__version__} from {_source_label(schema.source)};"
                " compact view, not Python source.",
                f"# aliases: {legend or 'none'}",
            ]
        )
    ]
    if blocks:
        sections.append("\n\n".join(blocks))
    if roots_line is not None:
        sections.append(roots_line)
    return "\n\n".join(sections) + "\n"


def _foldable_simple_type(cls: type) -> bool:
    """Returns True when the compact view folds *cls* onto its users.

    A simple type whose content is only facet comments renders no line
    of its own: its facets inline on each member that uses it, and its
    spelling resolves to the builtin data type it derives from. A list
    type is excluded — its content is the item type, not facets — and
    an unknown base (an ``xs:union``, whose runtime class derives from
    ``XsdDataType`` directly) keeps its own line so the name it renders
    under stays resolvable.
    """
    if not _is_simple_type(cls) or issubclass(cls, xsd_data_types.TypeList):
        return False
    return _nearest_builtin(cls) is not None


def _nearest_builtin(cls: type) -> type | None:
    """Returns the nearest builtin data type *cls* derives from, or ``None``.

    The compact alias a dropped simple type spells as is its nearest
    builtin ancestor's: a type restricted from ``xs:string`` reads as
    ``String``'s alias, and a restriction chain resolves the same way
    the runtime classes do.
    """
    for klass in cls.__mro__:
        if klass is not cls and klass in _BUILTIN_STUB_NAMES:
            return klass
    return None


def _compact_spellings(
    inventory: list[_StubClass], skipped: list[_StubClass]
) -> dict[str, _Spelling]:
    """Returns the annotation-name replacements the compact view applies.

    A dropped simple type spells as the compact alias of the builtin
    data type it derives (``SKU`` reads ``str``); the facet comment on
    the use site carries what the class added. Every remaining builtin
    data type's stub name maps to its alias. A schema class that
    shadows a builtin's *name* (a type literally named ``Int``) keeps
    its own name — the class wins, as in the full renderer's import
    scan — and a class that shadows an *alias text* (a type literally
    named ``str``) suppresses that alias family: the builtin keeps its
    long spelling, and a dropped simple type in the family spells as
    that long name, so no annotation reads ``str`` for both.
    """
    spellings: dict[str, _Spelling] = {}
    class_names = {entry.name for entry in inventory}
    for entry in skipped:
        base = _nearest_builtin(entry.cls)
        if base is None:  # unreachable for a foldable class; never guess
            continue
        spellings[entry.name] = _compact_spelling(entry.name, base, class_names)
    for cls, alias in _COMPACT_ALIASES.items():
        name = _BUILTIN_STUB_NAMES[cls]
        if name in class_names or alias in class_names:
            continue
        spellings[name] = _Spelling(alias, name)
    return spellings


def _compact_spelling(folded: str, base: type, class_names: set[str]) -> _Spelling:
    """Returns the spelling for a dropped simple type, alias collisions kept out.

    The alias text is the builtin's short spelling, which a class
    literally named after it takes precedence over; the spelling then
    falls back to the builtin's own stub name (and, when even that is
    taken, to the folded class's name, which at least stays unique).
    """
    alias = _COMPACT_ALIASES.get(base, base.__name__)
    if alias in class_names:
        long = _BUILTIN_STUB_NAMES.get(base, base.__name__)
        alias = long if long not in class_names else folded
    return _Spelling(alias, base.__name__)


def _spelling_pattern(spellings: dict[str, _Spelling]) -> re.Pattern[str] | None:
    """Returns the word-boundary pattern matching every spelled name."""
    if not spellings:
        return None
    names = "|".join(re.escape(name) for name in sorted(spellings, key=len, reverse=True))
    return re.compile(rf"\b(?:{names})\b")


def _spell_text(
    text: str,
    pattern: re.Pattern[str] | None,
    spellings: dict[str, _Spelling],
    facets_by_name: dict[str, list[str]],
    used: dict[str, list[str]],
) -> tuple[str, list[str]]:
    """Rewrites *text* into compact spellings; returns it with the facets it inlines.

    Only whole identifiers are rewritten, so ``list[SKU]`` becomes
    ``list[str]``. A spelling that differs from the pyxsd class it
    stands for is recorded in *used*, in first-use order, for the
    header's legend; one alias may fold several pyxsd classes, and the
    legend names each of them. The facets of a dropped simple type
    found in the text are returned for the member's comment.
    """
    if pattern is None:
        return text, []
    inlined: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(0)
        spelling = spellings[name]
        if spelling.text != spelling.source:
            sources = used.setdefault(spelling.text, [])
            if spelling.source not in sources:
                sources.append(spelling.source)
        for item in facets_by_name.get(name, ()):
            if item not in inlined:
                inlined.append(item)
        return spelling.text

    return pattern.sub(replace, text), inlined


def _compact_facets(
    entry: _StubClass, entries_by_id: dict[int, _StubClass], skipped_ids: set[int]
) -> list[str]:
    """Returns the facet items *entry*'s compact rendering inlines.

    The class's own facets come first; a skipped simple-type ancestor (a
    restriction chain whose parent class the compact view dropped)
    contributes its facets too, because no line of its own renders them.
    """
    items = _facet_items(entry) if _is_simple_type(entry.cls) else []
    seen = set(items)
    for klass in entry.cls.__mro__:
        ancestor = entries_by_id.get(id(klass))
        if ancestor is None or ancestor is entry or id(ancestor.cls) not in skipped_ids:
            continue
        for item in _facet_items(ancestor):
            if item not in seen:
                seen.add(item)
                items.append(item)
    return items


def _compact_class_block(
    entry: _StubClass,
    base: str,
    member_lines: list[str],
    facets: list[str],
    warnings: list[str],
) -> str:
    """Returns one compact class block: a header line, then one line per member.

    A class deriving only from ``SchemaBase`` shows no base; its
    class-level degradations (:func:`_class_degradations`) follow the
    header as comment lines, so the compact view carries the same
    truthfulness notes as the full one. A simple type's facets ride on
    the header line, and a class with no members, warnings and facets
    renders ``...`` so the header is not a dangling colon.
    """
    lines: list[str] = []
    if entry.note:
        lines.append(f"# {entry.note}")
    header = f"class {entry.name}"
    if base != "SchemaBase":
        header += f"({base})"
    facet_comment = f"  # {'; '.join(facets)}" if facets else ""
    if member_lines or warnings:
        lines.append(f"{header}:{facet_comment}")
        lines.extend(f"  # {warning}" for warning in warnings)
        lines.extend(member_lines)
    elif facets:
        lines.append(f"{header}:{facet_comment}")
    else:
        lines.append(f"{header}: ...")
    return "\n".join(lines)


def _compact_member_lines(
    members: list[_Member],
    pattern: re.Pattern[str] | None,
    spellings: dict[str, _Spelling],
    facets_by_name: dict[str, list[str]],
    used: dict[str, list[str]],
) -> list[str]:
    """Returns one compact line per member.

    A comment-only member stays a comment, an element or attribute a
    ``name: annotation`` line, and an overlay member a one-line
    signature (a property reads as a value). A folded simple type's
    facets append to the member's comment; the annotation's spellings
    are rewritten through the compact alias table.
    """
    lines: list[str] = []
    for member in members:
        if not member.name:
            if member.comment:
                lines.append(f"  # {member.comment}")
            continue
        annotation, inlined = _spell_text(
            member.annotation, pattern, spellings, facets_by_name, used
        )
        parts = [member.comment] if member.comment else []
        parts.extend(inlined)
        comment = f"  # {'; '.join(parts)}" if parts else ""
        if member.kind == "extension":
            lines.append(_compact_extension_line(member, annotation, comment))
            continue
        lines.append(f"  {member.name}: {annotation}{comment}")
    return lines


def _compact_extension_line(member: _Member, annotation: str, comment: str) -> str:
    """Returns one compact line for an overlay function or property.

    A property reads as a value (``label: str``), a function as a
    one-line signature (``def double(self, count: int) -> str: ...``);
    a signature that cannot be split degrades to ``Any`` with the
    member's comment, never a broken line.
    """
    params, returns = _split_signature(annotation)
    if member.is_property:
        line = f"  {member.name}: {returns if returns is not None else 'Any'}"
    elif params is None or returns is None:
        line = f"  def {member.name}(self) -> Any: ..."
    else:
        line = f"  def {member.name}{params} -> {returns}: ..."
    return line + comment


def _compact_roots(aliases: list[_RootAlias], spell: Callable[[str], str]) -> str | None:
    """Returns the trailing root-element comment line, or ``None``.

    Every global element is listed once as ``name: Type``; a root whose
    type could not be resolved keeps its name with ``?`` in place of a
    type rather than naming a class the document does not contain.
    """
    if not aliases:
        return None
    parts: list[str] = []
    for alias in aliases:
        type_text = spell(alias.type_name) if alias.type_name is not None else "?"
        parts.append(f"{alias.name}: {type_text}")
    return "# roots: " + ", ".join(parts)
