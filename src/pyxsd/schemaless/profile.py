"""Bounded, immutable profiles of one XML or native JSON document revision.

A profile walks the document's current authoritative storage once and records
per-path evidence: occurrence cardinality, null, empty, and mixed-content
counts, observed native JSON kinds, bounded value lists, ranked scalar
candidates, and the source of the current interpretation. Profiles are evidence
only: they never replace the scalar policy, edit lexical or native storage, or
add validation-derived XDM annotations, and a returned snapshot never changes
when the document does.

Every budget in :class:`ProfileLimits` is deterministic and follows document
order. Exceeding one sets ``complete=False`` (and the affected row's
``truncated``) instead of silently dropping evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from pyxsd.locations import DataLocation, IssueLocation, PathStep
from pyxsd.schemaless.json_values import NativeJsonValue
from pyxsd.schemaless.json_xdm import _scalar_text, escape_pointer_segment
from pyxsd.schemaless.tree import SchemalessElement, SchemalessRevision, _current_path
from pyxsd.schemaless.types import ScalarAdapter, TypePolicy, _ranked_candidates

if TYPE_CHECKING:
    from pyxsd.schemaless.document import SchemalessDocument
    from pyxsd.schemaless.json_document import JsonDocument

_Infer = Callable[[tuple[PathStep, ...], str | None], ScalarAdapter | None]

_ELEMENT = "element"
_ATTRIBUTE = "attribute"
_VALUE = "value"
_ARRAY_ITEMS = "array-items"

#: Deterministic report order for the observed native JSON storage kinds.
_NATIVE_KINDS = (
    "boolean",
    "integer",
    "decimal",
    "double",
    "string",
    "array",
    "object",
    "null",
)


@dataclass(frozen=True, slots=True)
class ProfileLimits:
    """Optional budgets for one profile walk.

    Every bound is inclusive and positive. ``max_paths`` bounds the number of
    reported path rows, ``max_distinct`` the distinct-value list of each row,
    ``max_samples`` the sample list of each row, and ``max_value_bytes`` the
    UTF-8 length of each stored value. ``None`` leaves that budget unset, which
    profiles the whole source; supply a bound whenever the source is not
    trusted. Exceeding a budget during a walk marks the profile incomplete.
    """

    max_paths: int | None = None
    max_distinct: int | None = None
    max_samples: int | None = None
    max_value_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_paths", "max_distinct", "max_samples", "max_value_bytes"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class TypeCandidate:
    """One ranked scalar type evaluated against every value at a path.

    ``first_failure`` is the first rejecting occurrence in document order, in
    the same location family the document uses for diagnostics: an XML
    :class:`~pyxsd.locations.IssueLocation` or a JSON
    :class:`~pyxsd.locations.DataLocation`. ``from_schema_annotation`` is
    true when at least one applied JSON Schema annotation suggested the type.
    """

    name: str
    passed: int
    failed: int
    first_failure: IssueLocation | None = None
    from_schema_annotation: bool = False


@dataclass(frozen=True, slots=True)
class PathProfile:
    """Bounded evidence for one structural XML path or exact JSON pointer.

    ``kind`` is ``"element"`` or ``"attribute"`` for XML, ``"value"`` for one
    exact JSON address, and ``"array-items"`` for the aggregate member evidence
    of a JSON array at that same pointer. An XML path spells expanded names
    without same-name occurrence indexes, exactly as a path-wide pin does.
    ``occurrences`` counts every value at the path; ``parents`` counts the
    containing occurrences (one document context for a root row) and
    ``min_occurs``/``max_occurs`` describe how many occurrences each parent
    holds; ``missing`` counts parents holding none. ``empties`` counts empty
    elements, empty attributes, empty strings, and empty JSON containers;
    ``nulls`` counts JSON nulls; ``mixed`` counts XML occurrences that mix
    character data with element children.

    ``distinct_values`` and ``samples`` hold bounded lexical text in document
    order (samples may repeat), ``native_kinds`` holds the observed native JSON
    kinds, and ``candidates`` holds the ranked built-in scalar types followed
    by any extra schema-suggested types, with their pass/fail evidence.
    A row without scalar evidence reports zero candidate
    counts: an XML occurrence with element children contributes character data
    only through ``mixed``, and a JSON array aggregate evaluates its scalar
    members while nulls and containers contribute only kinds and counts.
    Candidate counts always evaluate the whole lexical value; only the stored
    value lists are bounded. ``interpretations`` lists the distinct sources of
    the current interpretation in first-seen order.
    """

    path: str
    kind: str
    occurrences: int
    parents: int
    min_occurs: int
    max_occurs: int
    missing: int = 0
    nulls: int = 0
    empties: int = 0
    mixed: int = 0
    native_kinds: tuple[str, ...] = ()
    distinct_values: tuple[str, ...] = ()
    samples: tuple[str, ...] = ()
    candidates: tuple[TypeCandidate, ...] = ()
    interpretations: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def candidate_names(self) -> tuple[str, ...]:
        """The ranked candidate names in evaluation order."""
        return tuple(candidate.name for candidate in self.candidates)

    def candidate(self, name: str) -> TypeCandidate:
        """Return the candidate named *name*, or raise ``KeyError``."""
        for candidate in self.candidates:
            if candidate.name == name:
                return candidate
        raise KeyError(f"the profile at {self.path!r} has no {name!r} candidate")

    @property
    def interpretation(self) -> str:
        """The source of the current interpretation, or ``"mixed"``.

        A row whose occurrences are interpreted by different sources reports
        ``"mixed"``. An aggregate row with no scalar members has no source of
        its own and reports ``"native"``.
        """
        if not self.interpretations:
            return "native"
        if len(self.interpretations) == 1:
            return self.interpretations[0]
        return "mixed"


@dataclass(frozen=True, slots=True)
class DocumentProfile:
    """One immutable profile snapshot of a document's current revision.

    ``revision_id`` is the XML revision identifier and ``revision_dirty`` its
    edited state at snapshot time; a native JSON document has no revision
    counter, so it records ``None`` and ``False``. ``rows`` follows document
    order, and ``complete`` is false whenever a limit truncated evidence.
    """

    revision_id: int | None
    revision_dirty: bool
    complete: bool
    rows: tuple[PathProfile, ...]

    def at(self, path: str, kind: str | None = None) -> PathProfile:
        """Return the row for *path*, preferring its own row over an aggregate.

        ``kind`` selects one row when a path owns more than one, as a JSON
        array does with ``"value"`` and ``"array-items"``. Without ``kind`` the
        first row at the path wins, which is the exact address's own row
        because an aggregate row always follows the row it aggregates.
        """
        for row in self.rows:
            if row.path == path and (kind is None or row.kind == kind):
                return row
        requested = f"{path!r}" if kind is None else f"{path!r} with kind {kind!r}"
        raise KeyError(f"the profile has no row for {requested}")


@dataclass(slots=True)
class _CandidateCount:
    adapter: ScalarAdapter
    passed: int = 0
    failed: int = 0
    first_failure: IssueLocation | None = None
    from_schema_annotation: bool = False


@dataclass(slots=True)
class _Row:
    path: str
    kind: str
    parent: tuple[str, str] | None
    candidates: dict[str, _CandidateCount]
    occurrences: int = 0
    per_parent: dict[tuple[PathStep, ...] | None, int] = field(default_factory=dict)
    nulls: int = 0
    empties: int = 0
    mixed: int = 0
    kinds: set[str] = field(default_factory=set)
    distinct: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    samples: list[str] = field(default_factory=list)
    interpretations: list[str] = field(default_factory=list)
    truncated: bool = False

    def add_interpretation(self, source: str) -> None:
        """Record one occurrence's interpretation source in first-seen order."""
        if source not in self.interpretations:
            self.interpretations.append(source)


class _Builder:
    """Collect row evidence under one budget and freeze it into models."""

    __slots__ = ("_candidates", "_infer", "_inferred", "limits", "rows", "truncated")

    def __init__(
        self,
        limits: ProfileLimits,
        infer: _Infer | None = None,
    ) -> None:
        self.limits = limits
        self.rows: dict[tuple[str, str], _Row] = {}
        self.truncated = False
        self._candidates = _ranked_candidates()
        self._infer = infer
        self._inferred: dict[tuple[str, str], ScalarAdapter | None] = {}

    def row(self, path: str, kind: str, parent: tuple[str, str] | None) -> _Row | None:
        """Return the row for this address, or ``None`` once the path budget ends."""
        key = (path, kind)
        existing = self.rows.get(key)
        if existing is not None:
            return existing
        if self.limits.max_paths is not None and len(self.rows) >= self.limits.max_paths:
            self.truncated = True
            return None
        row = _Row(
            path,
            kind,
            parent,
            {name: _CandidateCount(adapter) for name, adapter in self._candidates},
        )
        self.rows[key] = row
        return row

    def inferred(
        self, key: tuple[str, str], steps: tuple[PathStep, ...], attribute: str | None
    ) -> ScalarAdapter | None:
        """Return the row's path-wide inferred adapter, computing it once."""
        try:
            return self._inferred[key]
        except KeyError:
            adapter = None if self._infer is None else self._infer(steps, attribute)
            self._inferred[key] = adapter
            return adapter

    def observe_text(self, row: _Row, text: str, location: IssueLocation) -> None:
        """Record one lexical value's bounded evidence and candidate results."""
        stored, cut = _bounded_text(text, self.limits.max_value_bytes)
        if cut:
            self.truncate(row)
        if self.limits.max_samples is None or len(row.samples) < self.limits.max_samples:
            row.samples.append(stored)
        else:
            self.truncate(row)
        if stored not in row.seen:
            if self.limits.max_distinct is None or len(row.seen) < self.limits.max_distinct:
                row.seen.add(stored)
                row.distinct.append(stored)
            else:
                self.truncate(row)
        for counts in row.candidates.values():
            if counts.adapter.test(text):
                counts.passed += 1
            else:
                counts.failed += 1
                if counts.first_failure is None:
                    counts.first_failure = location

    def suggest(self, row: _Row, names: tuple[str, ...]) -> None:
        """Register extra schema candidates before examining a row's values."""
        for name in names:
            if name not in row.candidates:
                row.candidates[name] = _CandidateCount(TypePolicy.resolve(name))
            row.candidates[name].from_schema_annotation = True

    def truncate(self, row: _Row) -> None:
        """Mark one row and the whole snapshot as truncated."""
        row.truncated = True
        self.truncated = True

    def finish(self, revision_id: int | None, revision_dirty: bool) -> DocumentProfile:
        """Freeze every collected row into the immutable result model."""
        return DocumentProfile(
            revision_id=revision_id,
            revision_dirty=revision_dirty,
            complete=not self.truncated,
            rows=tuple(self._freeze_row(row) for row in self.rows.values()),
        )

    def _freeze_row(self, row: _Row) -> PathProfile:
        if row.parent is None:
            parents = 1
            minimum = maximum = row.occurrences
            missing = 1 if row.occurrences == 0 else 0
        else:
            parents = self.rows[row.parent].occurrences
            counts = list(row.per_parent.values())
            maximum = max(counts, default=0)
            minimum = min(counts, default=0) if len(counts) == parents else 0
            missing = parents - len(counts)
        return PathProfile(
            path=row.path,
            kind=row.kind,
            occurrences=row.occurrences,
            parents=parents,
            min_occurs=minimum,
            max_occurs=maximum,
            missing=missing,
            nulls=row.nulls,
            empties=row.empties,
            mixed=row.mixed,
            native_kinds=tuple(kind for kind in _NATIVE_KINDS if kind in row.kinds),
            distinct_values=tuple(row.distinct),
            samples=tuple(row.samples),
            candidates=tuple(
                TypeCandidate(
                    name,
                    counts.passed,
                    counts.failed,
                    counts.first_failure,
                    counts.from_schema_annotation,
                )
                for name, counts in row.candidates.items()
            ),
            interpretations=tuple(row.interpretations),
            truncated=row.truncated,
        )


class _XmlWalk:
    """One document-order walk of a schemaless XML revision."""

    __slots__ = ("_builder", "_path_root", "_revision", "_root", "_root_steps")

    def __init__(self, document: SchemalessDocument, limits: ProfileLimits) -> None:
        self._revision: SchemalessRevision = document.root.revision
        self._root = document.root
        self._path_root, self._root_steps = _current_path(self._root)
        infer = self._infer if self._revision.types.mode == "infer" else None
        self._builder = _Builder(limits, infer)

    def run(self) -> DocumentProfile:
        pending: list[
            tuple[SchemalessElement, tuple[PathStep, ...], tuple[PathStep, ...] | None]
        ] = [(self._root, self._root_steps, None)]
        while pending:
            node, steps, parent_steps = pending.pop()
            parent = None if parent_steps is None else (_steps_path(parent_steps), _ELEMENT)
            row = self._builder.row(_steps_path(steps), _ELEMENT, parent)
            if row is None:
                # The path budget only bounds distinct rows: this unadmitted
                # element path is new, so every descendant path is new too.
                # Skip the subtree but keep the walk for admitted rows.
                continue
            self._observe_element(row, node, steps, parent_steps)
            for name, lexical in node.attributes.items():
                attribute = self._builder.row(
                    f"{row.path}/@{name}", _ATTRIBUTE, (row.path, _ELEMENT)
                )
                if attribute is None:
                    # Skip this unadmitted attribute only; an admitted child
                    # row may still occur under this element elsewhere.
                    continue
                self._observe_attribute(attribute, node, name, lexical, steps)
            pending.extend(reversed(_child_occurrences(node, steps)))
        return self._builder.finish(self._revision.revision_id, self._revision.dirty)

    def _infer(self, steps: tuple[PathStep, ...], attribute: str | None) -> ScalarAdapter | None:
        return self._revision._inferred_adapter(self._path_root, steps, attribute)

    def _observe_element(
        self,
        row: _Row,
        node: SchemalessElement,
        steps: tuple[PathStep, ...],
        parent_steps: tuple[PathStep, ...] | None,
    ) -> None:
        row.occurrences += 1
        row.per_parent[parent_steps] = row.per_parent.get(parent_steps, 0) + 1
        children = node.children
        text = node.text_value()
        if children:
            if text is not None or any(child.tail_value() is not None for child in children):
                row.mixed += 1
        else:
            if text is None or text == "":
                row.empties += 1
            self._builder.observe_text(row, text or "", _xml_location(node, steps, None))
        row.add_interpretation(self._source(row, steps, None, text))

    def _observe_attribute(
        self,
        row: _Row,
        node: SchemalessElement,
        name: str,
        lexical: str,
        steps: tuple[PathStep, ...],
    ) -> None:
        row.occurrences += 1
        row.per_parent[steps] = row.per_parent.get(steps, 0) + 1
        if lexical == "":
            row.empties += 1
        self._builder.observe_text(row, lexical, _xml_location(node, steps, name))
        row.add_interpretation(self._source(row, steps, name, lexical))

    def _source(
        self,
        row: _Row,
        steps: tuple[PathStep, ...],
        attribute: str | None,
        lexical: str | None,
    ) -> str:
        if self._revision._pin_for(self._path_root, steps, attribute) is not None:
            return "pinned"
        if lexical is not None:
            inferred = self._builder.inferred((row.path, row.kind), steps, attribute)
            if inferred is not None:
                return "inferred"
        return "lexical"


class _JsonWalk:
    """One document-order walk of a native JSON value."""

    __slots__ = ("_builder", "_document", "_value")

    def __init__(self, document: JsonDocument, limits: ProfileLimits) -> None:
        self._document = document
        self._value = document._value
        self._builder = _Builder(limits)

    def run(self) -> DocumentProfile:
        pending: list[tuple[NativeJsonValue, str]] = [(self._value, "")]
        while pending:
            value, pointer = pending.pop()
            row = self._builder.row(pointer, _VALUE, None)
            if row is None:
                break
            row.occurrences = 1
            self._observe_native(row, pointer, value)
            if isinstance(value, dict):
                pending.extend(
                    reversed(
                        [
                            (member, f"{pointer}/{escape_pointer_segment(key)}")
                            for key, member in value.items()
                        ]
                    )
                )
            elif isinstance(value, list):
                aggregate = self._builder.row(pointer, _ARRAY_ITEMS, None)
                if aggregate is None:
                    break
                aggregate.occurrences = len(value)
                for index, member in enumerate(value):
                    self._suggest(aggregate, f"{pointer}/{index}", member)
                for index, member in enumerate(value):
                    self._observe_native(aggregate, f"{pointer}/{index}", member)
                pending.extend(
                    reversed([(member, f"{pointer}/{index}") for index, member in enumerate(value)])
                )
        return self._builder.finish(None, False)

    def _observe_native(self, row: _Row, pointer: str, value: NativeJsonValue) -> None:
        row.kinds.add(_native_kind(value))
        if value is None:
            row.nulls += 1
            row.add_interpretation("native")
        elif isinstance(value, (dict, list)):
            if not value:
                row.empties += 1
            row.add_interpretation("native")
        else:
            if isinstance(value, str) and value == "":
                row.empties += 1
            self._suggest(row, pointer, value)
            row.add_interpretation(self._document._resolution_at(pointer, value)[1])
            self._builder.observe_text(
                row, _scalar_text(value), _json_location(self._document, pointer)
            )

    def _suggest(self, row: _Row, pointer: str, value: NativeJsonValue) -> None:
        if isinstance(value, str):
            self._builder.suggest(row, self._document._schema_candidates(pointer))


def _steps_path(steps: tuple[PathStep, ...]) -> str:
    """Format occurrence steps as a path without same-name indexes."""
    return "/" + "/".join(step.name for step in steps)


def _child_occurrences(
    node: SchemalessElement, steps: tuple[PathStep, ...]
) -> list[tuple[SchemalessElement, tuple[PathStep, ...], tuple[PathStep, ...]]]:
    """Return the element children with their absolute occurrence steps."""
    counts: dict[str, int] = {}
    occurrences: list[tuple[SchemalessElement, tuple[PathStep, ...], tuple[PathStep, ...]]] = []
    for child in node.children:
        index = counts.get(child._name_, 0) + 1
        counts[child._name_] = index
        occurrences.append((child, (*steps, PathStep(child._name_, index)), steps))
    return occurrences


def _xml_location(
    node: SchemalessElement, steps: tuple[PathStep, ...], attribute: str | None
) -> IssueLocation:
    """Name one occurrence in the XML diagnostic location family."""
    source = node.source_location
    return IssueLocation(source.source if source is not None else None, steps, attribute)


def _json_location(document: JsonDocument, pointer: str) -> DataLocation:
    """Name one occurrence in the JSON diagnostic location family."""
    return DataLocation(document.source, pointer)


def _native_kind(value: NativeJsonValue) -> str:
    """Name one native JSON storage kind for profile reporting."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _bounded_text(text: str, max_bytes: int | None) -> tuple[str, bool]:
    """Truncate one stored value to a UTF-8 byte budget.

    The stored text is a prefix that stays valid UTF-8, so a character split
    by the budget is dropped. The returned flag reports whether *text* changed.
    """
    if max_bytes is None:
        return text, False
    raw = text.encode("utf-8", "surrogatepass")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", "ignore"), True


def _xml_profile(document: SchemalessDocument, limits: ProfileLimits | None) -> DocumentProfile:
    """Profile one schemaless XML document revision."""
    return _XmlWalk(document, limits or ProfileLimits()).run()


def _json_profile(document: JsonDocument, limits: ProfileLimits | None) -> DocumentProfile:
    """Profile one native JSON document value."""
    return _JsonWalk(document, limits or ProfileLimits()).run()


__all__ = [
    "DocumentProfile",
    "PathProfile",
    "ProfileLimits",
    "TypeCandidate",
]
