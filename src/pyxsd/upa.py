"""Unique Particle Attribution over compiled content models (cos-nonambig).

The schema component constraint is checked on the *effective* content
model of a complex type — the base type's compiled model composed with
an extension's suffix included — because ambiguity can arise across a
derivation boundary (particlesZ022's inherited wildcard and the
extension's own overlapping wildcard). The sweep is a port of the
reference implementation's path analysis (``distinguishable_paths``):
every element or wildcard particle is reached through the chain of
compositors that contain it, and two overlapping particles are a
violation when no deterministic separation exists between their paths.
``upa_violations`` returns the first violation reason, or an empty list
for a deterministic model.

Deliberately faithful to the corpus and the reference implementation:

* Two occurrences of one expanded name are only ambiguous when the
  earlier particle can match more than once (``min occurs != max
  occurs``) or the paths cross a repetition with no required separator
  (particlesZ037, particlesZ033_c, particlesZ022); a univocal pair
  (``sequence(e1, e1)``, mgQ002) is deterministic.
* A choice (or ``all``) whose alternatives overlap is ambiguous for any
  overlapping pair (mgS002-005, mgQ003, mgQ021).
* An element particle and a wildcard that admits it are never reported:
  the element declaration takes precedence when both match an item, so
  the attribution stays unique.
* A substitution-group member overlaps its (transitive) head: two
  members of one group, or a member beside its head, are ambiguous
  (particlesZ033_e/f, wildI014's reading).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyxsd.content_model import Particle

HeadLookup = Callable[[Any], Any]

_COMPOSITORS = ("sequence", "choice", "all")


def upa_violations(
    model: Particle | None,
    head_lookup: HeadLookup | None = None,
) -> list[str]:
    """The Unique Particle Attribution violations of a content model.

    ``model`` is a compiled particle tree (the type's effective model).
    ``head_lookup`` maps an element declaration to the declaration of
    its substitution-group head, or ``None`` when the schema cannot
    supply one (exact expanded-name equality is used then).
    """
    if model is None:
        return []
    model = _fold_synthetic(model)
    seen: list[tuple[Particle, list[Particle]]] = []
    for leaf, path in _leaves(model, []):
        for previous, previous_path in seen:
            if not _overlap(previous, leaf, head_lookup):
                continue
            reason = _pair_violation(previous, previous_path, leaf, path)
            if reason:
                return [reason]
        seen.append((leaf, path))
    return []


def _fold_synthetic(particle: Particle) -> Particle:
    """Replaces group-reference wrappers with their folded content.

    A group reference is transparent in the component model: the
    referenced compositor's particles are the model's particles at the
    reference site's occurrence. The compiler models it as a synthetic
    singleton ``sequence``, so the wrapper is replaced by its content
    with the two occurrence ranges multiplied (mirrors
    ``_fold_synthetic`` in ``particle_derivation``).
    """
    if particle.kind in _COMPOSITORS:
        children = [_fold_synthetic(child) for child in particle.children]
        particle = Particle(
            particle.kind,
            particle.min_occurs,
            particle.max_occurs,
            children,
            particle.name,
            particle.spec,
            particle.descriptor,
            particle.synthetic,
            particle.siblings,
        )
    if particle.kind == "sequence" and particle.synthetic and len(particle.children) == 1:
        inner = particle.children[0]
        maximum = (
            None
            if particle.max_occurs is None or inner.max_occurs is None
            else particle.max_occurs * inner.max_occurs
        )
        return Particle(
            inner.kind,
            particle.min_occurs * inner.min_occurs,
            maximum,
            inner.children,
            inner.name,
            inner.spec,
            inner.descriptor,
            inner.synthetic,
            inner.siblings,
        )
    return particle


def _leaves(particle: Particle, path: list[Particle]) -> list[tuple[Particle, list[Particle]]]:
    """The ``(leaf, containing-compositor chain)`` pairs of a model.

    The chain holds every compositor from the root down to the leaf's
    parent, in order; the leaf itself is appended by the caller when a
    path is compared (mirrors the reference implementation's
    ``current_path + [e]``).
    """
    if particle.kind not in _COMPOSITORS:
        return [(particle, path)]
    found: list[tuple[Particle, list[Particle]]] = []
    for child in particle.children:
        found.extend(_leaves(child, [*path, particle]))
    return found


def _is_univocal(particle: Particle) -> bool:
    """Whether ``min occurs == max occurs`` (a fixed repeat count)."""
    return particle.min_occurs == particle.max_occurs


def _emptiable(particle: Particle) -> bool:
    """The reference implementation's ``is_emptiable`` for the paths.

    A particle is emptiable when its own ``min occurs`` is 0; a
    compositor is additionally emptiable when its member structure can
    match zero (a choice with any emptiable branch, a sequence or
    ``all`` with every member emptiable).
    """
    if particle.min_occurs == 0:
        return True
    if particle.kind not in _COMPOSITORS:
        return False
    if not particle.children:
        return True
    if particle.kind == "choice":
        return any(_emptiable(child) for child in particle.children)
    return all(_emptiable(child) for child in particle.children)


def _element_namespace(particle: Particle) -> str | None:
    descriptor = particle.descriptor
    if descriptor is not None:
        getter = getattr(descriptor, "getNamespace", None)
        if getter is not None:
            try:
                return getter()
            except Exception:
                return None
    return None


def _same_expanded_name(first: Particle, second: Particle) -> bool:
    return first.name == second.name and _element_namespace(first) == _element_namespace(second)


def _head_chain(declaration: Any, head_lookup: HeadLookup | None) -> set[int]:
    """The ids of a declaration's transitive substitution-group heads."""
    heads: set[int] = set()
    current = declaration
    while head_lookup is not None and current is not None:
        head = head_lookup(current)
        if head is None or id(head) in heads:
            return heads
        heads.add(id(head))
        current = head
    return heads


def _substitution_overlap(
    first: Particle, second: Particle, head_lookup: HeadLookup | None
) -> bool:
    """Whether either declaration substitutes for the other, transitively."""
    if head_lookup is None:
        return False
    first_decl = first.descriptor
    second_decl = second.descriptor
    if first_decl is None or second_decl is None:
        return False
    if id(second_decl) in _head_chain(first_decl, head_lookup):
        return True
    return id(first_decl) in _head_chain(second_decl, head_lookup)


def _wildcard_admits_element(wildcard: Particle, element: Particle) -> bool:
    """Whether a wildcard's namespace constraint admits an element.

    Wildcards carrying ``notQName`` exclusions are treated as
    non-overlapping: whether the name is excluded needs context a
    schema-phase sweep does not have, and reporting the overlap could
    reject a schema the corpus pins valid.
    """
    spec = wildcard.spec
    if spec is None or spec.not_qname:
        return False
    return spec.admits_namespace(_element_namespace(element), None)


def _overlap(first: Particle, second: Particle, head_lookup: HeadLookup | None) -> bool:
    if first.kind == "element" and second.kind == "element":
        if _same_expanded_name(first, second):
            return True
        return _substitution_overlap(first, second, head_lookup)
    if first.kind == "any" and second.kind == "any":
        if first.spec is None or second.spec is None:
            return False
        from pyxsd.wildcards import wildcard_specs_overlap

        return wildcard_specs_overlap(first.spec, second.spec)
    # Element and wildcard: the element declaration takes precedence, so
    # the attribution stays unique (the reference implementation adds a
    # wildcard precedence instead of a violation).
    return False


def _index_of(children: list[Particle], node: Particle) -> int:
    for index, child in enumerate(children):
        if child is node:
            return index
    return -1


def _distinguishable(path1: list[Particle], path2: list[Particle]) -> bool:
    """The reference implementation's path separation analysis.

    Returns ``True`` when the two leaf paths can be told apart without
    lookahead. Both paths run from the shared root to a leaf, the leaf
    last.
    """
    depth = 0
    for index, node in enumerate(path1):
        if not any(node is other for other in path2):
            if not index:
                return True
            depth = index - 1
            break
    if path1[depth].max_occurs == 0:
        return True

    univocal1 = univocal2 = True
    if path1[depth].kind == "sequence":
        index1 = _index_of(path1[depth].children, path1[depth + 1])
        index2 = _index_of(path2[depth].children, path2[depth + 1])
        before1 = any(not _emptiable(child) for child in path1[depth].children[:index1])
        after1 = before2 = any(
            not _emptiable(child) for child in path1[depth].children[index1 + 1 : index2]
        )
        after2 = any(not _emptiable(child) for child in path2[depth].children[index2 + 1 :])
    else:
        before1 = after1 = before2 = after2 = False

    for index in range(depth + 1, len(path1) - 1):
        univocal1 &= _is_univocal(path1[index])
        position = _index_of(path1[index].children, path1[index + 1])
        if path1[index].kind == "sequence":
            before1 |= any(not _emptiable(child) for child in path1[index].children[:position])
            after1 |= any(not _emptiable(child) for child in path1[index].children[position + 1 :])
        elif any(
            _emptiable(child)
            for child in path1[index].children
            if child is not path1[index].children[position]
        ):
            univocal1 = False

    for index in range(depth + 1, len(path2) - 1):
        univocal2 &= _is_univocal(path2[index])
        position = _index_of(path2[index].children, path2[index + 1])
        if path2[index].kind == "sequence":
            before2 |= any(not _emptiable(child) for child in path2[index].children[:position])
            after2 |= any(not _emptiable(child) for child in path2[index].children[position + 1 :])
        elif any(
            _emptiable(child)
            for child in path2[index].children
            if child is not path2[index].children[position]
        ):
            univocal2 = False

    if path1[depth].kind != "sequence":
        if before1 and before2:
            return True
        if before1:
            return (univocal1 and _is_univocal(path1[-1])) or after1 or path1[depth].max_occurs == 1
        if before2:
            return (univocal2 and _is_univocal(path2[-1])) or after2 or path2[depth].max_occurs == 1
        return False
    if path1[depth].max_occurs == 1:
        return before2 or ((before1 or univocal1) and (_is_univocal(path1[-1]) or after1))
    return (before2 or ((before1 or univocal1) and (_is_univocal(path1[-1]) or after1))) and (
        before1 or ((before2 or univocal2) and (_is_univocal(path2[-1]) or after2))
    )


def _pair_violation(
    first: Particle,
    first_path: list[Particle],
    second: Particle,
    second_path: list[Particle],
) -> str | None:
    """The violation reason for two overlapping leaves, or ``None``."""
    same_parent = bool(first_path) and bool(second_path) and first_path[-1] is second_path[-1]
    if same_parent:
        parent = first_path[-1]
        if parent.kind in ("choice", "all"):
            return _reason(first, second)
        if _is_univocal(first):
            return None
    if _distinguishable([*first_path, first], [*second_path, second]):
        return None
    return _reason(first, second)


def _reason(first: Particle, second: Particle) -> str:
    first_label = f"'{first.name}'" if first.kind == "element" else "a wildcard"
    second_label = f"'{second.name}'" if second.kind == "element" else "a wildcard"
    return (
        f"content model is ambiguous: {first_label} and {second_label} "
        "overlap and cannot be attributed uniquely (cos-nonambig)"
    )
