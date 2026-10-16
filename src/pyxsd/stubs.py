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
with a comment rather than naming a type the stub cannot import.
``compact`` selects the compact formatter's alias spellings; the member
model itself is identical in both modes.

:func:`emit_stub` renders the model as the full ``.pyi`` document: a
provenance and consumption header, imports, one class block per
inventory entry (mirroring the XSD derivation the runtime mirrors, with
simple types as their datatype subclasses and facets as comments), and
the global elements as module-level aliases. Rendering is total: a
schema that compiled always emits, and every construct the stub cannot
type truthfully becomes ``Any`` with a comment instead of an exception.
"""

from __future__ import annotations

import inspect
import keyword
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyxsd import __version__, xsd_data_types
from pyxsd.element_representatives.attribute import Attribute
from pyxsd.element_representatives.element import Element
from pyxsd.integrations._shape import particle_elements
from pyxsd.namespaces import XSD_NS, clark, local_name
from pyxsd.schema_base import SchemaBase

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
    """

    name: str
    annotation: str
    kind: Literal["element", "attribute", "extension"]
    comment: str | None = None


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
    skipped: an unresolvable type annotates as ``Any`` with a comment,
    a prohibited attribute use and a name with no usable Python
    accessor become comment-only members, and a member accessor that
    raises is treated as absent rather than propagating. ``compact``
    selects the compact formatter's alias spellings; the annotations
    this function returns are the same in both modes.

    Nothing here calls ``schema.require_valid()``: an error-bearing
    schema still compiles classes, and a stub may still be requested
    for it.
    """
    model = getattr(cls, "_contentModel_", None)
    inventory = set(names)
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
        for order, key in enumerate(klass.__dict__.get("_elementNames_", ())):
            descriptor = klass.__dict__.get(key)
            if not isinstance(descriptor, Element):
                continue  # shadowed by a later binding in the same body
            member = _element_member(key, descriptor, names, model)
            candidates.setdefault(key, (0, -depth, order, member))
        for order, key in enumerate(klass.__dict__.get("_attributeNames_", ())):
            descriptor = klass.__dict__.get(key)
            if not isinstance(descriptor, Attribute):
                continue
            member = _attribute_member(key, descriptor, names)
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
        candidates[key] = (2, 0, order, _extension_member(key, value))
    return [member for _, _, _, member in sorted(candidates.values(), key=lambda item: item[:3])]


def _element_member(key: str, descriptor: Any, names: dict[int, str], model: Any) -> _Member:
    """Returns the ``_Member`` for one element descriptor bound as *key*.

    Occurs are folded through *model* (the owning class's
    ``_contentModel_``), so an optional or repeated ancestor group
    reaches the annotation. A ``default`` or ``fixed`` value appends
    ``str`` and ``None`` to the union. The comment distinguishes where
    the value constraint lives: a default on the declaration itself is
    what makes its absent read the raw lexical string, while a
    reference site delegates the constraint to the referred declaration
    and still reads ``None`` when absent (the site carries no fallback
    of its own). ``nillable`` is a comment, not a union arm: the value
    class itself is unchanged.
    """
    xml_name = _xml_name(descriptor, key)
    if not _usable_identifier(key):
        return _Member(
            name="",
            annotation="",
            kind="element",
            comment=f"element {xml_name!r} has no Python accessor (bound as {key!r})",
        )
    parts: list[str] = []
    base, problem = _annotation_base(descriptor, xml_name, names)
    if problem is not None:
        parts.append(problem)
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


def _attribute_member(key: str, descriptor: Any, names: dict[int, str]) -> _Member:
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
            comment=f"attribute {xml_name!r} has no Python accessor (bound as {key!r})",
        )
    parts: list[str] = []
    base, problem = _annotation_base(descriptor, xml_name, names)
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


def _extension_member(key: str, value: Any) -> _Member:
    """Returns the ``_Member`` for an overlay's function or property.

    The annotation is the ``def`` text after the member name, read from
    ``inspect.signature`` (a property is introspected through its
    getter; its setter's asymmetry is out of a stub's reach). An
    unannotated return becomes ``Any``; a signature that cannot be read
    degrades to a variadic ``Any`` signature with a comment.
    """
    target = value.fget if isinstance(value, property) else value
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
        )
    if " -> " not in signature:
        signature += " -> Any"
    return _Member(name=key, annotation=signature, kind="extension", comment=None)


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
    descriptor: Any, xml_name: str, names: dict[int, str]
) -> tuple[str, str | None]:
    """Returns the annotation's base type name and any degradation comment.

    The resolution order is the inventory's stub name, then the builtin
    data type's own class name. Anything else — an unresolved
    declaration, the untyped ``anyType`` fallback — annotates as
    ``Any`` with a comment saying why, never as a guessed class name.
    """
    try:
        resolved = descriptor.getType()
    except Exception:
        return "Any", f"type of {xml_name!r} could not be resolved; annotated Any"
    if resolved is None:
        declared = getattr(descriptor, "type", None)
        return "Any", f"type {declared!r} is unresolved; annotated Any"
    if isinstance(resolved, type):
        stub_name = names.get(id(resolved))
        if stub_name is not None:
            return stub_name, None
        builtin_name = _BUILTIN_STUB_NAMES.get(resolved)
        if builtin_name is not None:
            return builtin_name, None
        if resolved is SchemaBase:
            return "Any", "no declared type; annotated Any"
    return "Any", f"type {getattr(resolved, '__name__', resolved)!r} is not stubbed; annotated Any"


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


# ---------------------------------------------------------------------------
# Full-mode rendering
# ---------------------------------------------------------------------------


def emit_stub(schema: Schema, *, compact: bool = False) -> str:
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

    Rendering never raises on a schema that compiled: a construct a
    stub cannot type truthfully (an unresolvable type, a member with no
    Python accessor) degrades to ``Any`` with a comment, and an
    error-bearing schema still emits. This never calls
    ``schema.require_valid()``.

    ``compact`` selects the agent-oriented compact formatter, which
    shares this module's member model; the compact view is a follow-up
    change, so both modes currently render the full document.
    """
    return _render_full(schema, compact=compact)


def _render_full(schema: Schema, *, compact: bool) -> str:
    """Assembles the full stub document for *schema*.

    Class blocks come in inventory order; the imports are derived from
    the rendered body, so only classes the document actually names are
    imported. The result always ends in a newline.
    """
    inventory = _inventory(schema)
    names = {id(entry.cls): entry.name for entry in inventory}
    class_blocks = [_render_class(entry, names, compact=compact) for entry in inventory]
    aliases = _root_aliases(schema, names)
    body_parts = list(class_blocks)
    if aliases:
        body_parts.append(_render_root_section(aliases))
    body = "\n\n".join(body_parts)
    class_names = {entry.name for entry in inventory}
    sections = [
        "\n".join(_header_lines(schema, _example_class(inventory, aliases))),
        "\n".join(
            _import_lines(
                _used_builtin_names(body, class_names),
                needs_datetime="datetime." in body,
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
        "# appends one item (obj.x = item); it does not take a list. Constructs",
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


def _import_lines(builtins: list[str], *, needs_datetime: bool) -> list[str]:
    """Returns the import block a full stub document starts with."""
    lines: list[str] = []
    if needs_datetime:
        lines.append("import datetime")
    lines.append("from typing import Any")
    lines.append("")
    lines.append("from pyxsd.schema_base import SchemaBase")
    if builtins:
        lines.append(f"from pyxsd.xsd_data_types import {', '.join(builtins)}")
    return lines


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


def _render_class(entry: _StubClass, names: dict[int, str], *, compact: bool) -> str:
    """Returns one class block, without a trailing newline.

    A note renders as a comment above the class; a documentation
    string renders inside it. Members come in :func:`_members` order
    (elements, attributes, overlay extensions); a class with no
    members renders ``...``. Simple types additionally carry their
    facets as comments, which is the only place a restriction's
    constraints are visible in the stub.
    """
    lines: list[str] = []
    if entry.note:
        lines.append(f"# {entry.note}")
    lines.append(f"class {entry.name}({_stub_base(entry, names)}):")
    lines.extend(_docstring_lines(entry))
    members = _members(entry.cls, names, compact=compact)
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


def _facet_comments(entry: _StubClass) -> list[str]:
    """Returns the simple type's facets as comment lines.

    Facets live on the ``xs:restriction`` children of a simple type's
    declaration; every child carrying a ``value`` attribute is one, so
    the comments show what the value space constrains (``pattern``,
    ``minLength``, ``enumeration``, ...) without a facet list to keep
    up to date. A declaration shape this does not recognize yields no
    comments rather than an error.
    """
    xsd_element = getattr(entry.er, "xsdElement", None)
    comments: list[str] = []
    if xsd_element is None:
        return comments
    try:
        restrictions = xsd_element.findall(f".//{{{XSD_NS}}}restriction")
        for restriction in restrictions:
            for facet in restriction:
                value = facet.get("value")
                if value is not None:
                    comments.append(f"# {local_name(facet.tag)}: {value}")
    except Exception:
        return comments
    return comments


def _member_lines(members: list[_Member]) -> list[str]:
    """Returns the class-body lines for *members*.

    A comment-only member (no usable accessor) renders as a bare
    comment, an overlay extension as a one-line ``def``, and a
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
    signature that cannot be split degrades to ``(self) -> Any`` with
    the member's comment, never a syntax-breaking line.
    """
    params, returns = _split_signature(member.annotation)
    if params is None or returns is None:
        reason = member.comment or "signature could not be split; annotated Any"
        return f"    def {member.name}(self) -> Any: ...  # {reason}"
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
      type, a name that is not a Python identifier, a repeat).
    """

    name: str
    type_name: str | None = None
    comment: str | None = None


def _root_aliases(schema: Schema, names: dict[int, str]) -> list[_RootAlias]:
    """Returns the aliases for *schema*'s global element declarations.

    Global elements are the valid document roots, so the stub lists
    them after the classes. A root whose type resolves annotates as
    ``type[TheClass]``; one that cannot be typed truthfully keeps its
    name in a comment instead. The first declaration of a name wins;
    a repeat is commented rather than emitted twice.
    """
    aliases: list[_RootAlias] = []
    seen_elements: set[int] = set()
    used_names: set[str] = set()
    for representatives in schema.components.values():
        for er in representatives:
            if id(er) in seen_elements or not _is_global_element(er):
                continue
            seen_elements.add(id(er))
            aliases.append(_root_alias(er, names, used_names))
    return aliases


def _is_global_element(er: Any) -> bool:
    """Returns True for a global ``Element`` representative."""
    if type(er).__name__ != "Element":
        return False
    try:
        return bool(er.isGlobalDeclaration())
    except Exception:
        return False


def _root_alias(er: Any, names: dict[int, str], used_names: set[str]) -> _RootAlias:
    """Returns the alias, or the comment explaining its absence, for *er*."""
    name = getattr(er, "name", None)
    if not isinstance(name, str) or not name:
        name = _called_name(er)
    if not name or not name.isidentifier() or keyword.iskeyword(name):
        return _RootAlias(name or "?", comment=f"root element {name!r} is not a Python identifier")
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
