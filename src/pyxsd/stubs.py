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
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyxsd.namespaces import clark

if TYPE_CHECKING:
    from pyxsd.schema import Schema

#: The ER class names that build instance classes. Every other kind in
#: the component table (elements, groups, facets, ...) is not a class.
_TYPE_ER_NAMES = ("SimpleType", "ComplexType")

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
