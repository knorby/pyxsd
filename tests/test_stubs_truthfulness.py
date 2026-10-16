"""Truthfulness harness: rendered annotations against runtime reads.

The stub renderers (Tasks 2-6) derive member annotations from the compiled
schema; this module is the suite's guard against a stub that lies. It
interprets each rendered annotation string with a small recursive matcher
(a union of arms; ``list[T]`` requires a list of matching items; ``None``
is an absence allowance; ``Any`` is exempt from value matching), then
cross-checks every member against *actual runtime behavior*:

- **Parsed-value checks** (``test_fixture_tree_annotations_match_runtime``):
  every fixture's instance document is parsed and the bound object graph is
  walked. Each member's parsed value -- the bound child instance(s) for an
  element, the descriptor read for an attribute -- must satisfy the
  member's annotation. The descriptor accessor is cross-checked too: when
  it returns a value it must agree with the parsed value.
- **Absence probes** (``test_fixture_member_probes``): the member is
  pruned out of the fixture's serialized document, the document is parsed
  again, and the member's read must match what its annotation and comment
  claim: ``| None`` members read ``None``; a declaration-local ``default``
  reads back exactly the raw lexical ``str``; an attribute ``fixed`` value
  is supplied by the binder; an element ``fixed`` value reads ``None``
  when absent. A branch that accepts its read additionally checks that
  read against the member's own annotation, so a probe whose read the
  annotation does not cover fails loudly instead of passing as an
  unexamined exemption. Members that cannot be probed are recorded with a
  reason (never skipped silently).

Runtime facts the harness is built around, each pinned by a test below:

- Parse-time binding stores a *simple-typed* child through the member's
  accessor, but a *complex-typed* child only in the parent's
  ``_children_`` list (``SchemaBase._addChildInstance``). A parsed complex
  child is therefore checked through ``_children_``; the accessor reading
  ``None`` for one is a known divergence, recorded as such and reported as
  a concern rather than silently excused.
- A substitution-group member satisfies the head's particle without
  filling the head accessor -- the member binds under its own name -- so
  the head reads ``None`` in a valid document, and a substitution-degraded
  annotation must carry the absence arm.
- ``Element.__get__`` falls back to the descriptor's own raw ``default``
  string, so a declaration-local element default reads back a plain
  ``str`` when absent; a ``fixed`` declaration has no such fallback.
- Attribute ``default``/``fixed`` values are applied by the binder through
  the descriptor, so they read back typed (a ``String`` subclass for
  string-typed declarations), not as the raw lexical text.

Nothing here calls ``schema.require_valid()``: the probes re-parse reduced
documents and lean on the lax binder, exactly like a user inspecting an
imperfect document would.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from functools import cache
from typing import Any

import pytest

from conftest import ALL_FIXTURES, run_parser
from pyxsd import xsd_data_types
from pyxsd.schema import Schema
from pyxsd.stubs import _inventory, _members

# ---------------------------------------------------------------------------
# Annotation interpretation
# ---------------------------------------------------------------------------

#: The scalar arms that are not class names.
_LITERAL_ARMS = ("None", "Any", "str")


def _builtin_names() -> dict[str, type]:
    """Returns the annotate-able builtin name -> class map.

    The renderer annotates a builtin data type with the class's own
    ``__name__`` (``Int`` for ``xsd_data_types.Int``), so the harness
    rebuilds that mapping from the module rather than trusting the
    renderer's private table. A schema class that shadows a builtin name
    takes precedence in :func:`_annotation_names`, matching the full
    renderer's import scan (the class name wins).
    """
    return {
        cls.__name__: cls
        for cls in vars(xsd_data_types).values()
        if isinstance(cls, type) and issubclass(cls, xsd_data_types.XsdDataType)
    }


def _annotation_names(inventory: list[Any]) -> dict[str, type]:
    """Returns the annotation text -> runtime class map for one schema."""
    names = _builtin_names()
    names.update({entry.name: entry.cls for entry in inventory})
    return names


def _split_arms(annotation: str, context: str) -> list[str]:
    """Splits a union annotation on top-level ``|`` into its arms.

    The split is bracket-aware, so ``list[T] | None`` yields
    ``["list[T]", "None"]`` -- not the brief's original ``list`` slice,
    which mangled that union. A malformed text (unbalanced brackets, an
    empty arm) fails loudly: the harness must never guess at an annotation
    shape it does not understand.
    """
    arms: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(annotation):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise AssertionError(f"{context}: unbalanced ']' in {annotation!r}")
        elif char == "|" and depth == 0:
            arms.append(annotation[start:index].strip())
            start = index + 1
    if depth:
        raise AssertionError(f"{context}: unbalanced '[' in {annotation!r}")
    arms.append(annotation[start:].strip())
    if any(not arm for arm in arms):
        raise AssertionError(f"{context}: empty union arm in {annotation!r}")
    return arms


def _validate_arms(annotation: str, names: dict[str, type], context: str) -> None:
    """Fails loudly when an annotation uses a shape the harness cannot read.

    Every arm must be a literal arm, a ``list[inner]`` whose inner arm is
    itself a literal or a resolvable class name, or a resolvable class
    name. This is the loud-failure guard: an unknown shape never becomes a
    silent pass.
    """
    for arm in _split_arms(annotation, context):
        if arm.startswith("list["):
            if not arm.endswith("]"):
                raise AssertionError(f"{context}: malformed list arm {arm!r} in {annotation!r}")
            inner = arm[len("list[") : -1]
            if inner.startswith("list[") or "|" in inner:
                raise AssertionError(f"{context}: unsupported nested list arm {arm!r}")
            arm = inner
        if arm not in _LITERAL_ARMS and arm not in names:
            raise AssertionError(
                f"{context}: annotation arm {arm!r} does not resolve to a runtime class"
            )


def check_annotation(value: Any, annotation: str, names: dict[str, type], context: str) -> None:
    """Asserts runtime *value* is consistent with stub *annotation*.

    ``None`` is accepted only when a ``None`` arm is present; a list value
    is accepted by a ``list[T]`` arm whose items each satisfy ``T``; any
    arm may be satisfied by a matching value (a union is an allow-list).
    An ``Any`` arm accepts every value -- degraded constructs are truthful
    but not precise. The annotation's shape is validated first, so an
    annotation the harness cannot interpret fails loudly even when the
    value would have satisfied some arm.
    """
    _validate_arms(annotation, names, context)
    arms = _split_arms(annotation, context)
    if value is None:
        assert "None" in arms, f"{context}: None not allowed by {annotation!r}"
        return
    if any(arm == "Any" for arm in arms):
        return
    for arm in arms:
        if arm in ("None", "Any"):
            continue
        if arm.startswith("list["):
            if not isinstance(value, list):
                continue
            inner = arm[len("list[") : -1]
            for item in value:
                check_annotation(item, inner, names, f"{context}[{item!r}]")
            return
        if arm == "str":
            if isinstance(value, str):
                return
            continue
        if isinstance(value, names[arm]):
            return
    raise AssertionError(
        f"{context}: {value!r} ({type(value).__name__}) is not covered by {annotation!r}"
    )


# ---------------------------------------------------------------------------
# Fixture plumbing
# ---------------------------------------------------------------------------


@cache
def _fixture_document(fixture: str) -> Any:
    """Returns the parsed fixture document, compiled once per session."""
    return run_parser(fixture)


def _class_names(schema: Schema) -> tuple[dict[int, str], dict[str, type]]:
    """Returns the renderer's ``id -> stub name`` map and the text map."""
    inventory = _inventory(schema)
    return {id(entry.cls): entry.name for entry in inventory}, _annotation_names(inventory)


def _member_descriptor(cls: type, member_name: str) -> Any:
    """Returns the class-body descriptor bound as *member_name*."""
    descriptor = getattr(cls, member_name, None)
    if descriptor is None or not hasattr(descriptor, "name"):
        raise AssertionError(f"{cls.__name__}.{member_name}: member has no class-body descriptor")
    return descriptor


def _child_belongs(child: Any, descriptor: Any) -> bool:
    """Returns True when a bound child was bound for *descriptor*.

    Identity is exact for most declarations; a group-expanded copy binds a
    per-use descriptor while the particle carries the shared declaration,
    so the child's document name is the fallback link.
    """
    if getattr(child, "_descriptor_", None) is descriptor:
        return True
    return getattr(child, "_name_", None) == descriptor.name


def _bound_children(node: Any, descriptor: Any) -> list[Any]:
    """Returns the parsed children belonging to *descriptor*."""
    return [child for child in (node._children_ or ()) if _child_belongs(child, descriptor)]


def _has_list_arm(annotation: str, context: str) -> bool:
    """Returns True when the annotation has a ``list[...]`` arm."""
    return any(arm.startswith("list[") for arm in _split_arms(annotation, context))


def _walk(node: Any, path: tuple[tuple[str, int], ...] = ()) -> Any:
    """Yields ``(node, path)`` pre-order over the bound graph.

    ``path`` is the ``(document name, index among same-named siblings)``
    chain from the root; the probe builder replays it over the serialized
    XML, so no parent pointers are needed.
    """
    yield node, path
    counts: dict[str, int] = {}
    for child in getattr(node, "_children_", ()) or ():
        if child is None:
            continue
        name = getattr(child, "_name_", None)
        if not isinstance(name, str):
            continue
        index = counts.get(name, 0)
        counts[name] = index + 1
        yield from _walk(child, (*path, (name, index)))


def _local(tag: str) -> str:
    """Returns an ElementTree tag's local name."""
    return tag.rsplit("}", 1)[-1]


def _follow(node: Any, path: tuple[tuple[str, int], ...]) -> Any:
    """Replays *path* over a freshly parsed bound tree."""
    for name, index in path:
        found = [
            child
            for child in (getattr(node, "_children_", None) or ())
            if child is not None and getattr(child, "_name_", None) == name
        ]
        if index >= len(found):
            return None
        node = found[index]
    return node


def _follow_xml(element: Any, path: tuple[tuple[str, int], ...]) -> Any:
    """Replays *path* over a serialized XML tree."""
    for name, index in path:
        found = [child for child in element if _local(child.tag) == name]
        if index >= len(found):
            return None
        element = found[index]
    return element


def _check_member_read(
    node: Any,
    member: Any,
    descriptor: Any,
    names: dict[str, type],
    context: str,
) -> None:
    """Checks one member's parsed value and accessor channel.

    The parsed value is the bound child instance(s) for an element and the
    descriptor read for an attribute. When the accessor returns a value it
    must agree with the parsed value. When the accessor returns ``None``
    while a parsed child exists, that is a descriptor-channel divergence
    that only the complex-child binding path explains (the runtime stores
    those in ``_children_`` only; see
    ``test_complex_child_values_live_in_children_only_known_divergence``);
    any other such read is asserted loudly.
    """
    if member.kind == "attribute":
        check_annotation(getattr(node, member.name), member.annotation, names, context)
        return
    children = _bound_children(node, descriptor)
    list_annotated = _has_list_arm(member.annotation, context)
    if len(children) > 1 and not list_annotated:
        raise AssertionError(
            f"{context}: {len(children)} bound children for a non-list annotation "
            f"{member.annotation!r}"
        )
    if children:
        value: Any = children if list_annotated else children[0]
    else:
        value = getattr(node, member.name)
    check_annotation(value, member.annotation, names, context)
    if not children:
        return
    accessor = getattr(node, member.name)
    if accessor is None:
        assert not all(isinstance(child, xsd_data_types.XsdDataType) for child in children), (
            f"{context}: accessor read None while a simple-typed parsed child exists"
        )
        return
    if list_annotated:
        assert isinstance(accessor, list) and len(accessor) == len(children), (
            f"{context}: accessor {accessor!r} does not agree with the bound children"
        )
        assert all(seen is bound for seen, bound in zip(accessor, children, strict=True)), (
            f"{context}: accessor list does not hold the bound children"
        )
    else:
        assert accessor is children[0], (
            f"{context}: accessor {accessor!r} is not the bound child {children[0]!r}"
        )


# ---------------------------------------------------------------------------
# Probes: absence, declaration-local default, fixed
# ---------------------------------------------------------------------------

_DEFAULT_READ = re.compile(r"default '(?P<value>[^']*)' reads as str when absent")
_REFERRED_DEFAULT = re.compile(r"default '(?P<value>[^']*)' from the referenced declaration")
_FIXED = re.compile(r"fixed '(?P<value>[^']*)'")


@dataclass(frozen=True)
class _Probe:
    """One member probe and its outcome.

    ``expectation`` is one of:

    - ``none`` - the absent read must be ``None`` (an optional member, a
      fixed element, or a reference site delegating to a referred
      declaration's default, which supplies no site-local fallback);
    - ``default-lexical`` - the declaration carries a local ``default``,
      so the absent read must be exactly the raw lexical string;
    - ``fixed-lexical`` - an attribute's ``fixed`` value is applied by the
      binder, so the absent read must carry that value;
    - ``fixed-element`` - an element's ``fixed`` value has no read
      fallback, so the absent read must be ``None`` (covered by the
      union's ``None`` arm);
    - ``none-in-valid-doc`` - the annotation allows no ``None``; a
      ``None`` read only counts when the reduced document stays valid.

    ``executed`` is False for a recorded skip, with ``reason`` saying why
    (never silent); an executed probe with a non-``None`` ``reason`` is a
    violation for the caller to judge (the known-defect registry absorbs
    the pinned ones).
    """

    fixture: str
    cls_name: str
    member: str
    expectation: str | None
    executed: bool
    reason: str | None = None
    detail: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.fixture, self.cls_name, self.member)

    @property
    def violated(self) -> bool:
        return self.executed and self.reason is not None


def _probe_expectation(member: Any) -> str:
    """Classifies a member's annotation and comment into a probe kind."""
    comment = member.comment or ""
    if _DEFAULT_READ.search(comment):
        return "default-lexical"
    if _FIXED.search(comment):
        return "fixed-lexical" if member.kind == "attribute" else "fixed-element"
    if _REFERRED_DEFAULT.search(comment):
        return "none"
    if "None" in _split_arms(member.annotation, "probe"):
        return "none"
    return "none-in-valid-doc"


def _reduced_document(
    document: Any, path: tuple[tuple[str, int], ...], member: Any, descriptor: Any
) -> tuple[Any, Any]:
    """Returns the fixture document with *member* pruned, and its new tree.

    Pruning removes every direct child element with the member's document
    name (all occurrences of a repeated member), or the attribute itself.
    Returns ``(reduced_doc, reduced_node)``; ``reduced_doc`` is ``None``
    when the node cannot be located in the serialized document, and
    ``reduced_node`` is ``None`` when the reduced document no longer binds
    the node.
    """
    root_element = ET.fromstring(document.to_string())
    element = _follow_xml(root_element, path)
    if element is None:
        return None, None
    name = descriptor.name
    if member.kind == "attribute":
        for key in [key for key in element.attrib if _local(key) == name]:
            del element.attrib[key]
    else:
        for child in [child for child in list(element) if _local(child.tag) == name]:
            element.remove(child)
    reduced_doc = document.schema.parse(io.StringIO(ET.tostring(root_element, encoding="unicode")))
    return reduced_doc, _follow(reduced_doc.root, path)


def _run_probe(
    fixture: str,
    node: Any,
    path: tuple[tuple[str, int], ...],
    member: Any,
    descriptor: Any,
    document: Any,
    names: dict[str, type],
) -> _Probe:
    """Runs one member's absence/default probe, returning its outcome.

    A branch that accepts its read shape (``None``, the lexical default,
    the fixed value) additionally checks the read against the member's
    own annotation: an annotation missing the arm its probe demonstrates
    fails loudly here instead of passing as an unexamined exemption.
    """
    expectation = _probe_expectation(member)
    cls_name = type(node).__name__
    reduced_doc, reduced_node = _reduced_document(document, path, member, descriptor)
    if reduced_doc is None or reduced_node is None:
        reason = (
            f"the node at path {path!r} could not be located in the serialized document"
            if reduced_doc is None
            else "the reduced document no longer binds this node"
        )
        return _Probe(fixture, cls_name, member.name, expectation, executed=False, reason=reason)
    value = getattr(reduced_node, member.name)
    detail = f"read {value!r} ({type(value).__name__})"
    context = f"{fixture}:{cls_name}.{member.name} probe ({expectation})"
    if expectation in ("none", "fixed-element"):
        if value is None:
            check_annotation(value, member.annotation, names, context)
            return _Probe(fixture, cls_name, member.name, expectation, True, detail=detail)
        return _Probe(
            fixture,
            cls_name,
            member.name,
            expectation,
            executed=True,
            detail=detail,
            reason=f"{(member.comment or 'optional absence')!r} claims an absent None read",
        )
    if expectation == "default-lexical":
        lexical = _DEFAULT_READ.search(member.comment or "").group("value")
        if member.kind == "element":
            ok = type(value) is str and value == lexical
        else:
            ok = isinstance(value, str) and value == lexical
        if ok:
            check_annotation(value, member.annotation, names, context)
            return _Probe(fixture, cls_name, member.name, expectation, True, detail=detail)
        return _Probe(
            fixture,
            cls_name,
            member.name,
            expectation,
            executed=True,
            detail=detail,
            reason=f"claims the absent read is the lexical default {lexical!r}",
        )
    if expectation == "fixed-lexical":
        lexical = _FIXED.search(member.comment or "").group("value")
        if str(value) == lexical:
            check_annotation(value, member.annotation, names, context)
            return _Probe(fixture, cls_name, member.name, expectation, True, detail=detail)
        return _Probe(
            fixture,
            cls_name,
            member.name,
            expectation,
            executed=True,
            detail=detail,
            reason=f"claims the absent read carries the fixed value {lexical!r}",
        )
    # none-in-valid-doc: the annotation claims no absence, so a None read
    # is a defect only when the reduced document is valid.
    if value is None and reduced_doc.is_valid:
        return _Probe(
            fixture,
            cls_name,
            member.name,
            expectation,
            executed=True,
            detail=f"{detail}; the reduced document is valid",
            reason="absent read is None although the annotation has no None arm",
        )
    if value is None:
        return _Probe(
            fixture,
            cls_name,
            member.name,
            expectation,
            executed=False,
            reason="required member; absence only appears in an invalid document",
        )
    return _Probe(
        fixture,
        cls_name,
        member.name,
        expectation,
        executed=False,
        reason="member still reads a value after pruning; absence not demonstrated",
    )


def _iter_member_probes(fixture: str) -> list[_Probe]:
    """Runs every member probe for one fixture.

    Members of classes the fixture's instance document never instantiates
    get no probe and are recorded as skips with that reason, so the ledger
    covers every inventory member the parsed graph could not reach.
    """
    document = _fixture_document(fixture)
    assert document.root is not None, f"{fixture}: fixture instance did not bind"
    id_names, names = _class_names(document.schema)
    probes: list[_Probe] = []
    reached: set[int] = set()
    for node, path in _walk(document.root):
        cls = type(node)
        if id(cls) not in id_names:
            continue
        reached.add(id(cls))
        for member in _members(cls, id_names, compact=False):
            if not member.name:
                continue
            if member.kind == "extension":
                probes.append(
                    _Probe(
                        fixture,
                        cls.__name__,
                        member.name,
                        expectation=None,
                        executed=False,
                        reason="overlay extension member; not a descriptor read",
                    )
                )
                continue
            descriptor = _member_descriptor(cls, member.name)
            probes.append(_run_probe(fixture, node, path, member, descriptor, document, names))
    for entry in _inventory(document.schema):
        if id(entry.cls) in reached:
            continue
        for member in _members(entry.cls, id_names, compact=False):
            if not member.name:
                continue
            probes.append(
                _Probe(
                    fixture,
                    entry.name,
                    member.name,
                    expectation=None,
                    executed=False,
                    reason="class not instantiated by the fixture instance document",
                )
            )
    return probes


_PROBE_CACHE: dict[str, list[_Probe]] = {}


def _probes(fixture: str) -> list[_Probe]:
    """Returns (and caches) the probe outcomes for one fixture."""
    if fixture not in _PROBE_CACHE:
        _PROBE_CACHE[fixture] = _iter_member_probes(fixture)
    return _PROBE_CACHE[fixture]


#: Annotation gaps this harness discovered and that are pinned, with the
#: current (wrong) stub text, by the dedicated tests below instead of
#: failing every fixture run. A probe for a key here is expected to
#: *fail* its expectation; a key whose probe passes is stale and
#: ``test_known_defects_are_still_real`` fails loudly. Currently empty:
#: the substitution-head gap was fixed in ``pyxsd.stubs`` (the degraded
#: annotation now carries the absence arm its probe demonstrates), and
#: the corpus walk enforces the corrected annotation without a pin.
KNOWN_DEFECTS: dict[tuple[str, str, str], str] = {}


# ---------------------------------------------------------------------------
# Tests: the annotation matcher itself
# ---------------------------------------------------------------------------


def test_matcher_accepts_union_and_list_shapes():
    names = {"Int": xsd_data_types.Int, "String": xsd_data_types.String}
    check_annotation(xsd_data_types.Int("3"), "Int | str | None", names, "t")
    check_annotation(None, "Int | str | None", names, "t")
    check_annotation([xsd_data_types.Int("1"), xsd_data_types.Int("2")], "list[Int]", names, "t")
    # The R2 fix: the trailing absence arm belongs to the union, not to
    # the list's item type.
    check_annotation([xsd_data_types.Int("1")], "list[Int] | None", names, "t")
    check_annotation(None, "list[Int] | None", names, "t")
    check_annotation("plain", "String | str | None", names, "t")


def test_matcher_rejects_a_list_under_a_scalar_arm_and_the_reverse():
    names = {"Int": xsd_data_types.Int}
    with pytest.raises(AssertionError, match="None not allowed"):
        check_annotation(None, "list[Int]", names, "t")
    with pytest.raises(AssertionError, match="not covered"):
        check_annotation([xsd_data_types.Int("1")], "Int | None", names, "t")
    with pytest.raises(AssertionError, match="not covered"):
        check_annotation(xsd_data_types.Int("1"), "list[Int] | None", names, "t")
    # A value matching no non-None arm still fails: the checked union is an
    # allow-list, not a membership test for the annotation's text.
    with pytest.raises(AssertionError, match="not covered"):
        check_annotation(xsd_data_types.String("x"), "Int | None", names, "t")


def test_matcher_fails_loudly_on_unknown_or_malformed_annotations():
    names = {"Int": xsd_data_types.Int}
    with pytest.raises(AssertionError, match="does not resolve"):
        check_annotation(1, "NoSuchClass", names, "t")
    with pytest.raises(AssertionError, match="does not resolve"):
        check_annotation([1], "list[NoSuchClass]", names, "t")
    with pytest.raises(AssertionError, match="unbalanced"):
        check_annotation(1, "list[Int", names, "t")
    with pytest.raises(AssertionError, match="empty union arm"):
        check_annotation(1, "Int | ", names, "t")


# ---------------------------------------------------------------------------
# Tests: corpus-wide truthfulness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_fixture_tree_annotations_match_runtime(fixture):
    """Every member's parsed value and accessor read satisfy its annotation."""
    document = _fixture_document(fixture)
    id_names, names = _class_names(document.schema)
    assert document.root is not None, f"{fixture}: fixture instance did not bind"
    checked = 0
    for node, _path in _walk(document.root):
        cls = type(node)
        if id(cls) not in id_names:
            continue
        for member in _members(cls, id_names, compact=False):
            if not member.name or member.kind == "extension":
                continue
            descriptor = _member_descriptor(cls, member.name)
            context = f"{fixture}:{cls.__name__}.{member.name} ({member.annotation})"
            _check_member_read(node, member, descriptor, names, context)
            checked += 1
    assert checked > 0, f"{fixture}: no members were reachable in the parsed tree"


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_fixture_annotations_are_wellformed(fixture):
    """Every rendered annotation parses into arms the harness understands.

    The check runs over the whole inventory, not only the classes the
    instance document reached, so a class no fixture instance visits
    cannot hide an annotation shape the harness would misread.
    """
    document = _fixture_document(fixture)
    id_names, names = _class_names(document.schema)
    checked = 0
    for entry in _inventory(document.schema):
        for member in _members(entry.cls, id_names, compact=False):
            if not member.name:
                # A comment-only member (prohibited use, unusable accessor)
                # must carry the comment that is its only stub presence.
                assert member.comment, f"{fixture}:{entry.name}: comment-only member with none"
                continue
            if member.kind == "extension":
                continue
            _validate_arms(member.annotation, names, f"{fixture}:{entry.name}.{member.name}")
            checked += 1
    assert checked > 0, f"{fixture}: no annotations to check"


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_fixture_emit_stub_renders_the_member_model(fixture):
    """Both renderers emit a line for every named member of the model.

    The truthfulness checks above interpret the shared ``_members`` model;
    this test ties the emitted text to that model inside each class block,
    so a renderer that silently drops a member or renders it under another
    class is caught here as well. Compact annotations are alias-rewritten
    by design, so only the full mode's annotation text must appear
    verbatim.
    """
    from pyxsd.stubs import _foldable_simple_type, emit_stub

    document = _fixture_document(fixture)
    schema = document.schema
    inventory = _inventory(schema)
    id_names = {id(entry.cls): entry.name for entry in inventory}
    full_blocks = _class_blocks(emit_stub(schema))
    compact_blocks = _class_blocks(emit_stub(schema, compact=True))
    for entry in inventory:
        assert entry.name in full_blocks, f"{fixture}: full stub omits class {entry.name}"
        members = _members(entry.cls, id_names, compact=False)
        if _foldable_simple_type(entry.cls):
            # The compact view folds a facet-only simple type onto its use
            # sites and renders no class line of its own (Task 5 rule).
            assert not [member for member in members if member.name]
            continue
        assert entry.name in compact_blocks, f"{fixture}: compact stub omits class {entry.name}"
        block = full_blocks[entry.name]
        compact_block = compact_blocks[entry.name]
        for member in members:
            if not member.name:
                assert member.comment in block and member.comment in compact_block
                continue
            if member.kind == "extension":
                assert f"def {member.name}" in block and f"def {member.name}" in compact_block
                continue
            assert f"    {member.name}: {member.annotation}" in block, (
                f"{fixture}: full stub omits {entry.name}.{member.name} ({member.annotation})"
            )
            assert f"  {member.name}: " in compact_block, (
                f"{fixture}: compact stub omits {entry.name}.{member.name}"
            )


def _class_blocks(stub_text: str) -> dict[str, str]:
    """Returns the rendered text per class block, keyed by class name.

    A class name is taken up to the first ``(``/``:``; the member lines an
    inventory class renders are its own block's (a base class's members
    are that base's block), so this keeps the renderer check scoped.
    """
    blocks: dict[str, str] = {}
    name_lines: list[str] = []
    for line in stub_text.splitlines():
        if line.startswith("class "):
            if name_lines:
                blocks[name_lines[0]] = "\n".join(name_lines[1:])
            header = line[len("class ") :]
            name = header.split("(", 1)[0].split(":", 1)[0]
            name_lines = [name, line]
        elif name_lines:
            name_lines.append(line)
    if name_lines:
        blocks[name_lines[0]] = "\n".join(name_lines[1:])
    return blocks


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_fixture_member_probes(fixture):
    """Absence and default probes hold for every probeable member."""
    failures: list[str] = []
    for probe in _probes(fixture):
        if not probe.violated:
            continue
        if probe.key in KNOWN_DEFECTS:
            continue
        failures.append(
            f"{fixture}:{probe.cls_name}.{probe.member} probe ({probe.expectation}): "
            f"{probe.reason}; {probe.detail}"
        )
    assert not failures, "probe expectation(s) violated:\n" + "\n".join(failures)


def test_every_probe_skip_is_recorded_with_a_reason():
    """No member is silently skipped: skips carry a reason, and the corpus
    actually exercised each probe kind."""
    executed = 0
    skipped = 0
    kinds: set[str] = set()
    for fixture in ALL_FIXTURES:
        for probe in _probes(fixture):
            if probe.executed:
                executed += 1
                if probe.reason is None:
                    kinds.add(probe.expectation or "")
            else:
                skipped += 1
                assert probe.reason, f"{probe.key}: skip without a reason"
    assert executed > 0, "no probe was executed at all"
    assert skipped > 0, "the corpus recorded no skips; the skip ledger is not exercised"
    assert "default-lexical" in kinds, "the declaration-local default probe never ran"
    assert {"fixed-element", "fixed-lexical"} & kinds, "the fixed probe never ran"
    assert "none" in kinds, "the absence probe never ran"


def test_fixture_defaults_read_back_lexical_values():
    """The brief's fixture: an absent ``default`` reads raw ``str``, an
    absent ``fixed`` element reads ``None``, and absent defaulted/fixed
    attributes carry their constraining value."""
    document = _fixture_document("defaults")
    id_names, _ = _class_names(document.schema)
    root = document.schema.parse(io.StringIO("<settings/>")).root

    members = {member.name: member for member in _members(type(root), id_names, compact=False)}
    assert members["volume"].annotation == "Integer | str | None"
    assert type(root.volume) is str and root.volume == "5"
    assert members["label"].annotation == "String | str | None"
    assert root.label is None
    assert members["mode"].annotation == "String | str | None"
    assert isinstance(root.mode, str) and root.mode == "standard"
    assert members["key"].annotation == "String | str | None"
    assert str(root.key) == "k-1"


def test_probe_pass_requires_the_annotation_to_cover_the_read():
    """A probe pass cross-checks its read against the annotation.

    The probe branches accept a read *shape* alone -- a ``None`` read, the
    lexical default, the fixed value -- so a member whose annotation
    misses the arm its own probe demonstrates (``Integer | None`` with a
    "default '5' reads as str when absent" comment) would otherwise pass
    silently. The true annotations pass; the doctored ones fail loudly.
    """
    document = _fixture_document("defaults")
    id_names, names = _class_names(document.schema)
    root = document.root
    volume = next(
        member
        for member in _members(type(root), id_names, compact=False)
        if member.name == "volume"
    )
    volume_descriptor = _member_descriptor(type(root), "volume")
    passing = _run_probe("defaults", root, (), volume, volume_descriptor, document, names)
    assert passing.executed and not passing.violated

    lying_default = replace(volume, annotation="Integer | None")
    with pytest.raises(AssertionError, match="not covered"):
        _run_probe("defaults", root, (), lying_default, volume_descriptor, document, names)

    label = next(
        member for member in _members(type(root), id_names, compact=False) if member.name == "label"
    )
    lying_absence = replace(label, annotation="String | str")
    with pytest.raises(AssertionError, match="None not allowed"):
        _run_probe(
            "defaults",
            root,
            (),
            lying_absence,
            _member_descriptor(type(root), "label"),
            document,
            names,
        )


def test_known_defects_are_still_real():
    """Every entry in KNOWN_DEFECTS must still be a failing probe.

    A passing probe for a pinned key means the defect was fixed (or the
    probe stopped reaching it): this test then fails, forcing the pin --
    and the Task 7 report's concern -- to be revisited rather than quietly
    keeping a stale stain on the suite. The registry is currently empty
    (the substitution-head gap was fixed in the renderer);
    ``test_known_defects_registry_still_flags_a_stale_pin`` proves the
    guard itself still fails when a pin goes stale.
    """
    stale = []
    for key, reason in KNOWN_DEFECTS.items():
        probe = next((probe for probe in _probes(key[0]) if probe.key == key), None)
        assert probe is not None, f"{key}: no such probe; stale KNOWN_DEFECTS entry"
        if not probe.violated:
            stale.append(f"{key}: probe no longer fails ({probe.reason}); concern says: {reason}")
    assert not stale, "\n".join(stale)


def test_known_defects_registry_still_flags_a_stale_pin(monkeypatch):
    """The registry's stale-pin guard works with no live entries.

    ``KNOWN_DEFECTS`` is empty because the corpus is clean, not because
    the guard stopped checking: pinning a probe that passes must make
    ``test_known_defects_are_still_real`` fail loudly.
    """
    passing = next(probe for probe in _probes("defaults") if probe.executed and not probe.violated)
    monkeypatch.setitem(KNOWN_DEFECTS, passing.key, "stale pin exercise")
    with pytest.raises(AssertionError, match="no longer fails"):
        test_known_defects_are_still_real()


# ---------------------------------------------------------------------------
# Tests: pinned runtime facts behind the harness's shape
# ---------------------------------------------------------------------------


def test_simple_child_values_are_stored_through_the_accessor():
    """A simple-typed child is readable through its descriptor after parse."""
    document = _fixture_document("inventory")
    first_item = document.root._children_[0]
    children = _bound_children(first_item, _member_descriptor(type(first_item), "name"))
    assert len(children) == 1
    assert first_item.name is children[0]


def test_complex_child_values_live_in_children_only_known_divergence():
    """Known runtime divergence, reported as a concern.

    Parse-time binding stores a complex-typed child only in the parent's
    ``_children_`` list (``SchemaBase._addChildInstance`` stores through
    the accessor for primitives only), so ``root.item`` reads None even
    though the parsed values exist. The stubs' semantics preamble reads as
    if present members read their value; the harness therefore checks
    complex children through ``_children_`` and records this divergence
    instead of pretending the accessor answers. If the binding runtime
    starts populating complex accessors, this pin fails and both the
    divergence allowance and the concern must be revisited.
    """
    document = _fixture_document("inventory")
    id_names, _ = _class_names(document.schema)
    item_member = next(
        member
        for member in _members(type(document.root), id_names, compact=False)
        if member.name == "item"
    )
    assert item_member.annotation == "list[itemType]"
    bound = _bound_children(document.root, _member_descriptor(type(document.root), "item"))
    assert bound and all(not isinstance(child, xsd_data_types.XsdDataType) for child in bound)
    assert document.root.item is None


def test_element_fixed_str_arm_is_unreachable_known_deferred():
    """Known deferred over-approximation (Task 3 report, deferred minor).

    An element with ``fixed`` carries a ``str`` union arm that no read
    path produces: the descriptor falls back only to a ``default``
    attribute, never to ``fixed``, so an absent fixed element reads None
    and a present one reads the typed value. The arm is an allow-list
    over-approximation, not a wrong type for any actual read; this test
    pins both the arm and the reachability gap.
    """
    document = _fixture_document("defaults")
    id_names, _ = _class_names(document.schema)
    cls = type(document.root)
    label = next(
        member for member in _members(cls, id_names, compact=False) if member.name == "label"
    )
    assert "str" in _split_arms(label.annotation, "label")
    assert document.schema.parse(io.StringIO("<settings/>")).root.label is None
    assert cls().label is None


def test_reference_site_default_str_arm_is_unreachable_known_deferred():
    """Known deferred over-approximation (Task 3 report, deferred minor).

    A reference site whose *referred* declaration carries a default has no
    site-local fallback, so its absent read is None (pinned by
    ``test_stubs_annotations.test_reference_site_with_referred_default_is_absent_as_none``);
    the ``str`` arm the renderer adds covers no read on the site. This
    harness pin keeps the gap visible.
    """
    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="item" type="xs:int" default="9"/>
      <xs:complexType name="bag"><xs:sequence>
        <xs:element ref="item" minOccurs="0"/>
      </xs:sequence></xs:complexType>
      <xs:element name="root" type="bag"/>
    </xs:schema>"""
    schema = Schema.compile(io.StringIO(xsd))
    id_names = {id(entry.cls): entry.name for entry in _inventory(schema)}
    bag = schema.classes["bag"]
    member = next(m for m in _members(bag, id_names, compact=False) if m.name == "item")
    assert member.annotation == "Int | str | None"
    assert "referenced declaration" in (member.comment or "")
    assert schema.parse(io.StringIO("<root/>")).root.item is None
    assert bag().item is None


def test_substitution_head_reads_none_and_the_annotation_has_the_absence_arm():
    """A substitution member satisfies the head's particle without filling
    the head accessor, so the head reads ``None`` in a valid document.

    ``<shape><dot>3</dot></shape>`` is valid (``dot`` substitutes for the
    head ``marker``) and binds under its own name, while ``shape.marker``
    stays None. Task 7's Finding 2 was that the stub rendered
    ``marker: list[AnySimpleType]`` with no absence arm; the renderer now
    adds it, the probe for ``substitution:shape|complexType.marker``
    enforces the read corpus-wide, and this test pins the annotation,
    comment and read together.
    """
    document = _fixture_document("substitution")
    valid = document.schema.parse(io.StringIO("<shape><dot>3</dot></shape>"))
    assert valid.is_valid
    id_names, names = _class_names(document.schema)
    marker = next(
        member
        for member in _members(type(valid.root), id_names, compact=False)
        if member.name == "marker"
    )
    assert valid.root.marker is None
    assert valid.root.dot == 3  # the substitute binds under its own name
    assert marker.annotation == "list[AnySimpleType] | None"
    assert "substitution group: marker" in (marker.comment or "")
    assert "members: dot, peg" in (marker.comment or "")
    check_annotation(valid.root.marker, marker.annotation, names, "substitution:marker")
