"""Parse-scoped RELAX NG pattern annotations and independent capture budgets.

Annotations are an optional view over the same accepting evidence that supports
typed scalar access.  They retain surviving authored origins, the named grammar
definitions those origins belong to, and the scalar alternatives at each site.
They never choose a branch, prove acceptance, or change mandatory evidence.

Capture is deliberately truncatable.  A capture budget counts annotated lexical
nodes in preorder; retained records are sound subsets, and a dropped record
marks both the table and every queried node incomplete instead of implying that
no match exists.  Typed values and candidate identities come from the complete
evidence graph and are independent of this budget.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, TypeVar

from pyxsd.relaxng.contracts import ScalarContract, scalar_contract
from pyxsd.relaxng.evidence import EvidenceGraph, EvidenceOccurrence, ScalarWitness
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
from pyxsd.relaxng.tree import LexicalRevision, RelaxNGElement
from pyxsd.relaxng.values import (
    AlternativeValue,
    ListInterpretation,
    alternative_value_from_witnesses,
    list_interpretation,
    list_value,
)

__all__ = [
    "AnnotationTable",
    "NodeAnnotations",
    "ScalarAnnotation",
]


def _origin_key(origin: SourceOrigin) -> tuple[object, ...]:
    """Return a process-independent authored-origin order.

    Source identifiers and their exact locations decide the order; object
    identity, hash iteration, and mapping insertion never participate.
    """
    location = origin.location
    if location is None:
        location_key: tuple[object, ...] = (1, "", (), "")
    else:
        location_key = (
            0,
            location.source or "",
            tuple((step.name, step.index) for step in location.path),
            location.attribute or "",
        )
    return (origin.source_id, location_key)


def _scalar_key(annotation: ScalarAnnotation) -> tuple[object, ...]:
    """Order character data before attributes, then by stable site identity."""
    if annotation.kind == "text":
        return (0, annotation.content_index)
    return (1, annotation.name or "")


_T = TypeVar("_T")


def _typed_items(values: Iterable[object], expected: type[_T], label: str) -> tuple[_T, ...]:
    """Copy one public tuple field and reject values of the wrong type."""
    items: list[_T] = []
    for item in values:
        if not isinstance(item, expected):
            raise TypeError(f"{label} must contain {expected.__name__} values")
        items.append(item)
    return tuple(items)


@dataclass(frozen=True)
class ScalarAnnotation:
    """One typed scalar position on a lexical element.

    ``kind="text"`` identifies the element's leading character data and uses
    ``content_index``; ``kind="attribute"`` identifies one present attribute by
    its expanded name.  ``value`` retains every surviving candidate; the
    annotation never selects a preferred branch.
    """

    kind: Literal["text", "attribute"]
    name: str | None
    content_index: int | None
    value: AlternativeValue

    def __post_init__(self) -> None:
        if self.kind == "text":
            if self.name is not None:
                raise ValueError("a text scalar annotation has no attribute name")
            if (
                isinstance(self.content_index, bool)
                or not isinstance(self.content_index, int)
                or self.content_index < 0
            ):
                raise ValueError("a text scalar annotation needs a non-negative content_index")
        elif self.kind == "attribute":
            if self.content_index is not None:
                raise ValueError("an attribute scalar annotation has no content_index")
            if not isinstance(self.name, str) or not self.name:
                raise ValueError("an attribute scalar annotation needs an expanded name")
        else:
            raise ValueError("scalar annotation kind must be 'text' or 'attribute'")
        if not isinstance(self.value, AlternativeValue):
            raise TypeError("a scalar annotation value must be an AlternativeValue")


@dataclass(frozen=True)
class NodeAnnotations:
    """Immutable annotation view for one lexical element occurrence.

    ``patterns`` is the sorted union of surviving authored origins at every
    scalar and structural site of this element.  ``definitions`` names the
    compiled grammar definitions whose origins intersect those surviving
    origins.  ``scalar`` retains the deterministic typed scalar sites.  There is
    deliberately no singular matched pattern or type.
    """

    schema_id: str
    revision_id: int
    patterns: tuple[SourceOrigin, ...]
    definitions: tuple[str, ...]
    scalar: tuple[ScalarAnnotation, ...]
    complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.schema_id, str) or not self.schema_id:
            raise ValueError("schema_id must be a nonempty string")
        if isinstance(self.revision_id, bool) or not isinstance(self.revision_id, int):
            raise ValueError("revision_id must be an integer")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a bool")
        patterns = _typed_items(self.patterns, SourceOrigin, "patterns")
        definitions = _typed_items(self.definitions, str, "definitions")
        scalar = _typed_items(self.scalar, ScalarAnnotation, "scalar")
        object.__setattr__(
            self,
            "patterns",
            tuple(sorted(set(patterns), key=_origin_key)),
        )
        object.__setattr__(self, "definitions", tuple(sorted(set(definitions))))
        object.__setattr__(
            self,
            "scalar",
            tuple(sorted(scalar, key=_scalar_key)),
        )


@dataclass(frozen=True, eq=False)
class AnnotationTable:
    """One parse revision's immutable annotation records.

    ``complete`` is false when the capture budget dropped at least one
    annotated node.  A retained record in a truncated table is a sound subset
    and also reports ``complete=False``.  An unretained node returns empty
    tuples with the table's completeness instead of a fabricated match.
    """

    schema_id: str
    revision_id: int
    complete: bool
    _revision: LexicalRevision = field(repr=False, compare=False)
    _entries: Mapping[RelaxNGElement, NodeAnnotations] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_entries", MappingProxyType(dict(self._entries)))

    def for_node(self, node: RelaxNGElement) -> NodeAnnotations:
        """Return the annotation view for one element of this parse revision.

        A node from another revision or a non-element value is rejected rather
        than silently treated as unannotated.
        """
        if not isinstance(node, RelaxNGElement) or node.revision is not self._revision:
            raise ValueError("the node does not belong to this RELAX NG parse revision")
        entry = self._entries.get(node)
        if entry is not None:
            return entry
        return NodeAnnotations(
            self.schema_id,
            self.revision_id,
            (),
            (),
            (),
            self.complete,
        )


def _site_interpretations(
    witnesses: tuple[ScalarWitness, ...],
    evidence: EvidenceGraph,
    *,
    grammar_fingerprint: str,
    revision: int,
) -> tuple[ListInterpretation, ...]:
    """Build one correlated interpretation per surviving list contract."""
    interpretations: dict[str, ListInterpretation] = {}
    for witness in witnesses:
        if scalar_contract(witness.pattern).kind != "list":
            continue
        interpretation = list_interpretation(
            witness,
            evidence,
            grammar_fingerprint=grammar_fingerprint,
            revision=revision,
        )
        interpretations.setdefault(interpretation.candidate_id, interpretation)
    return tuple(interpretations.values())


def typed_value_at_site(
    evidence: EvidenceGraph,
    occurrence: EvidenceOccurrence,
    *,
    grammar_fingerprint: str,
    revision: int,
    contracts: tuple[ScalarContract, ...] | None = None,
) -> object | None:
    """Return one scalar site's typed value using the Task 4 selection rules.

    This is the single implementation shared by generic typed access and
    annotation materialization.  Wrapper shape and candidate identities follow
    the complete schema-static contract set, never the surviving branch that
    happens to be easiest for this lexical value.
    """
    witnesses = evidence.scalar_witnesses_at(occurrence)
    if not witnesses:
        return None
    schema_contracts = evidence.schema_contracts_at(occurrence) if contracts is None else contracts
    if len(schema_contracts) == 1:
        contract = schema_contracts[0]
        if contract.kind == "list":
            return list_value(
                witnesses,
                evidence,
                schema_contracts=schema_contracts,
                grammar_fingerprint=grammar_fingerprint,
                revision=revision,
            )
        value = alternative_value_from_witnesses(
            witnesses,
            grammar_fingerprint=grammar_fingerprint,
            revision=revision,
        )
        if (
            contract.kind == "atomic"
            and len(value.candidates) == 1
            and value.candidates[0].decodable
        ):
            return value.candidates[0].decoded
        return value
    return alternative_value_from_witnesses(
        witnesses,
        grammar_fingerprint=grammar_fingerprint,
        revision=revision,
        list_interpretations=_site_interpretations(
            witnesses,
            evidence,
            grammar_fingerprint=grammar_fingerprint,
            revision=revision,
        ),
    )


def _alternative_value_at_site(
    witnesses: tuple[ScalarWitness, ...],
    evidence: EvidenceGraph,
    *,
    grammar_fingerprint: str,
    revision: int,
) -> AlternativeValue | None:
    """Return the complete wrapper for one annotation scalar site."""
    if not witnesses:
        return None
    return alternative_value_from_witnesses(
        witnesses,
        grammar_fingerprint=grammar_fingerprint,
        revision=revision,
        list_interpretations=_site_interpretations(
            witnesses,
            evidence,
            grammar_fingerprint=grammar_fingerprint,
            revision=revision,
        ),
    )


def _scalar_annotations(
    node: RelaxNGElement,
    *,
    evidence: EvidenceGraph,
    text_sites: Mapping[RelaxNGElement, EvidenceOccurrence],
    attribute_sites: Mapping[tuple[RelaxNGElement, str], EvidenceOccurrence],
    witnesses: Mapping[EvidenceOccurrence, tuple[ScalarWitness, ...]],
    grammar_fingerprint: str,
    revision: int,
) -> tuple[ScalarAnnotation, ...]:
    """Build the deterministic text-then-attributes scalar view of one node."""
    scalar: list[ScalarAnnotation] = []
    text_site = text_sites.get(node)
    if text_site is not None and node.text_value() is not None:
        value = _alternative_value_at_site(
            witnesses.get(text_site, ()),
            evidence,
            grammar_fingerprint=grammar_fingerprint,
            revision=revision,
        )
        if value is not None:
            scalar.append(ScalarAnnotation("text", None, text_site.content_index, value))
    for name in sorted(node.attributes):
        occurrence = attribute_sites.get((node, name))
        if occurrence is None:
            continue
        value = _alternative_value_at_site(
            witnesses.get(occurrence, ()),
            evidence,
            grammar_fingerprint=grammar_fingerprint,
            revision=revision,
        )
        if value is not None:
            scalar.append(ScalarAnnotation("attribute", name, None, value))
    return tuple(scalar)


def build_annotation_table(
    root: RelaxNGElement,
    evidence: EvidenceGraph,
    *,
    schema_id: str,
    revision_id: int,
    definition_origins: Mapping[str, OriginTable],
    max_capture_entries: int | None,
) -> AnnotationTable:
    """Materialize one optional annotation table from accepting evidence.

    Annotated nodes are visited in lexical preorder.  The first
    ``max_capture_entries`` records are retained; observing another annotated
    node marks the table incomplete and stops capture without raising.  ``None``
    retains every record.
    """
    origins_by_element: dict[RelaxNGElement, set[SourceOrigin]] = {}
    for evidence_node in evidence.retained_nodes:
        origins_by_element.setdefault(evidence_node.occurrence.element, set()).update(
            evidence_node.origins
        )

    text_sites: dict[RelaxNGElement, EvidenceOccurrence] = {}
    attribute_sites: dict[tuple[RelaxNGElement, str], EvidenceOccurrence] = {}
    for occurrence in evidence.occurrences:
        if occurrence.kind == "text" and occurrence.content_index == 0:
            text_sites.setdefault(occurrence.element, occurrence)
        elif occurrence.kind == "attribute" and occurrence.name is not None:
            attribute_sites.setdefault((occurrence.element, occurrence.name), occurrence)

    found_witnesses: dict[EvidenceOccurrence, dict[ScalarWitness, ScalarWitness]] = {}
    for edge in evidence.retained_edges:
        for witness in edge.witnesses:
            found_witnesses.setdefault(witness.occurrence, {})[witness] = witness
    witnesses = {occurrence: tuple(found.values()) for occurrence, found in found_witnesses.items()}

    definitions_by_origin: dict[SourceOrigin, set[str]] = {}
    for name, origins in definition_origins.items():
        for origin in origins:
            definitions_by_origin.setdefault(origin, set()).add(name)

    retained: dict[
        RelaxNGElement,
        tuple[tuple[SourceOrigin, ...], tuple[str, ...], tuple[ScalarAnnotation, ...]],
    ] = {}
    complete = True
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.children))
        patterns = tuple(sorted(origins_by_element.get(node, ()), key=_origin_key))
        scalar = _scalar_annotations(
            node,
            evidence=evidence,
            text_sites=text_sites,
            attribute_sites=attribute_sites,
            witnesses=witnesses,
            grammar_fingerprint=schema_id,
            revision=revision_id,
        )
        if not patterns and not scalar:
            continue
        if max_capture_entries is not None and len(retained) >= max_capture_entries:
            complete = False
            break
        definitions = tuple(
            sorted({name for origin in patterns for name in definitions_by_origin.get(origin, ())})
        )
        retained[node] = (patterns, definitions, scalar)

    entries = {
        node: NodeAnnotations(
            schema_id,
            revision_id,
            patterns,
            definitions,
            scalar,
            complete,
        )
        for node, (patterns, definitions, scalar) in retained.items()
    }
    return AnnotationTable(schema_id, revision_id, complete, root.revision, entries)
