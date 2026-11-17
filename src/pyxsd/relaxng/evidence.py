"""Parse-local packed proofs of complete accepting RELAX NG event runs.

Each event maps semantic residuals to proof nodes, not sets of origins.
Equal residuals share a map entry whose OR edge keeps alternative incoming
proofs. Advancing a run adds an AND edge requiring its previous prefix and
the local event proof. Lists use the same construction at token boundaries:
no complete document derivations or list assignments are enumerated.

Edges point from a conclusion (``predecessors``, one node) to its premises
(``successors``). AND requires every premise; OR requires any premise. A
zero-premise AND is a successful local fact. Only viable premises reachable
from final accepting conclusions survive. Origins are optional provenance
views of matched semantic patterns, never premises of an acceptance proof.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from pyxsd.relaxng.datatypes import Datatype, DatatypeContext
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.origins import OriginTable
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Data,
    DataExcept,
    Element,
    Empty,
    Group,
    Interleave,
    List,
    NotAllowed,
    OneOrMore,
    Pattern,
    Ref,
    Text,
    Value,
    choice,
    group,
    interleave,
    one_or_more,
)
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.validator import (
    TokenSpan,
    _After,
    _after,
    _apply_after,
    _event_derivation_children,
    _instance_events,
    _nullable,
    _schema_datatype_context,
    _whitespace,
    expanded_name,
    validate_element,
    xml_whitespace_tokens,
)

__all__ = [
    "EvidenceEdge",
    "EvidenceGraph",
    "EvidenceNode",
    "EvidenceOccurrence",
    "ScalarWitness",
    "build_binding_evidence",
]


@dataclass(frozen=True, eq=False)
class EvidenceOccurrence:
    """Opaque site identity; equal lexical values never identify occurrences.

    Token sites point to their enclosing scalar site and retain exact spans.
    Event sites identify attributes by name and character nodes by content index.
    """

    element: RelaxNGElement
    kind: str
    name: str | None = None
    content_index: int | None = None
    token_index: int | None = None
    parent: EvidenceOccurrence | None = None
    span: TokenSpan | None = None


@dataclass(frozen=True)
class ScalarWitness:
    """Raw accepting scalar contract, lexical value, and occurrence context.

    List witnesses describe the whole list; token witnesses remain separate
    premises in the graph. Consumers must not multiply their marginal views
    into an alleged jointly accepting assignment.
    """

    occurrence: EvidenceOccurrence
    pattern: Pattern
    lexical: str
    context: DatatypeContext
    token_spans: tuple[TokenSpan, ...] = ()

    @property
    def datatype(self) -> Datatype | None:
        """The compiled datatype, or None for text and whole-list witnesses."""
        return cast(Datatype | None, getattr(self.pattern, "datatype", None))


@dataclass(frozen=True, eq=False)
class EvidenceNode:
    """One proof conclusion at a site, with an opaque semantic residual key.

    Residual keys are parse-local integers: private validator continuations
    cannot escape through the graph. Sharing a key does not share node identity.
    """

    occurrence: EvidenceOccurrence
    residual: int
    origins: OriginTable


@dataclass(frozen=True)
class EvidenceEdge:
    """A conjunctive or alternative dependency, optionally carrying witnesses."""

    kind: Literal["and", "or"]
    predecessors: tuple[EvidenceNode, ...]
    successors: tuple[EvidenceNode, ...]
    witnesses: tuple[ScalarWitness, ...] = ()


@dataclass(frozen=True)
class EvidenceGraph:
    """Immutable accepting proof snapshot and deterministic marginal views."""

    accepting_roots: tuple[EvidenceNode, ...]
    retained_nodes: tuple[EvidenceNode, ...]
    retained_edges: tuple[EvidenceEdge, ...]
    occurrences: tuple[EvidenceOccurrence, ...]

    def origins_at(self, occurrence: EvidenceOccurrence) -> OriginTable:
        """Authored-origin unions of retained matched semantic patterns only."""
        return OriginTable().union(
            *(node.origins for node in self.retained_nodes if node.occurrence is occurrence)
        )

    def scalar_witnesses_at(self, occurrence: EvidenceOccurrence) -> tuple[ScalarWitness, ...]:
        """Return raw surviving witnesses, deduplicated in construction order."""
        return tuple(
            dict.fromkeys(
                witness
                for edge in self.retained_edges
                for witness in edge.witnesses
                if witness.occurrence is occurrence
            )
        )


def _prune(
    nodes: list[EvidenceNode],
    edges: list[EvidenceEdge],
    roots: tuple[EvidenceNode, ...],
) -> tuple[tuple[EvidenceNode, ...], tuple[EvidenceEdge, ...]]:
    """Least-fixed-point viability, then accepting-root forward reachability.

    An unfounded cycle is not a proof. An AND with one dead premise is dead;
    an OR can retain its other premises. Indexing dependencies makes this
    linear in graph size, rather than rescanning every edge per event.
    """
    dependents: dict[EvidenceNode, list[int]] = defaultdict(list)
    outgoing: dict[EvidenceNode, list[int]] = defaultdict(list)
    remaining = []
    ready: deque[int] = deque()
    for index, edge in enumerate(edges):
        remaining.append(len(edge.successors))
        outgoing[edge.predecessors[0]].append(index)
        for child in edge.successors:
            dependents[child].append(index)
        if not edge.successors and edge.kind == "and":
            ready.append(index)
    viable: set[EvidenceNode] = set()
    while ready:
        edge = edges[ready.popleft()]
        parent = edge.predecessors[0]
        if parent in viable:
            continue
        viable.add(parent)
        for index in dependents[parent]:
            remaining[index] -= 1
            if edges[index].kind == "or" or remaining[index] == 0:
                ready.append(index)
    reachable: set[EvidenceNode] = set()
    retained: dict[int, EvidenceEdge] = {}
    pending = list(roots)
    while pending:
        node = pending.pop()
        if node in reachable or node not in viable:
            continue
        reachable.add(node)
        for index in outgoing[node]:
            edge = edges[index]
            children = tuple(child for child in edge.successors if child in viable)
            if edge.kind == "and" and len(children) != len(edge.successors):
                continue
            if edge.kind == "or" and not children:
                continue
            retained[index] = EvidenceEdge(edge.kind, edge.predecessors, children, edge.witnesses)
            pending.extend(children)
    return (
        tuple(node for node in nodes if node in reachable),
        tuple(retained[index] for index in sorted(retained)),
    )


_Results = dict[Pattern, EvidenceNode]


class _Builder:
    """Second-pass event derivatives whose values are packed local proofs."""

    def __init__(
        self,
        limits: RelaxNGLimits | None,
        origins: Callable[[Pattern], OriginTable] | None,
    ) -> None:
        self.counter = BudgetCounter("evidence", limits)
        self.origins = origins
        self.nodes: list[EvidenceNode] = []
        self.edges: list[EvidenceEdge] = []
        self.occurrences: list[EvidenceOccurrence] = []
        self.residuals: dict[Pattern, int] = {}
        self.witnesses: dict[ScalarWitness, ScalarWitness] = {}
        self.tokens: dict[tuple[EvidenceOccurrence, int], EvidenceOccurrence] = {}

    def node(
        self,
        site: EvidenceOccurrence,
        residual: Pattern,
        children: tuple[EvidenceNode, ...] = (),
        *,
        kind: Literal["and", "or"] = "and",
        matched: Pattern | None = None,
        witnesses: tuple[ScalarWitness, ...] = (),
    ) -> EvidenceNode:
        self.counter.consume("max_evidence_states")
        key = self.residuals.setdefault(residual, len(self.residuals))
        origins = (
            self.origins(matched)
            if matched is not None and self.origins is not None
            else OriginTable()
        )
        result = EvidenceNode(site, key, origins)
        self.nodes.append(result)
        self.counter.consume("max_evidence_edges")
        self.edges.append(EvidenceEdge(kind, (result,), children, witnesses))
        return result

    def merge(
        self, site: EvidenceOccurrence, alternatives: list[tuple[Pattern, EvidenceNode]]
    ) -> _Results:
        grouped: dict[Pattern, list[EvidenceNode]] = defaultdict(list)
        for residual, proof in alternatives:
            grouped[residual].append(proof)
        return {
            residual: self.node(site, residual, tuple(proofs), kind="or")
            for residual, proofs in grouped.items()
        }

    def scalar(
        self, pattern: Pattern, lexical: str, site: EvidenceOccurrence, cx: DatatypeContext
    ) -> ScalarWitness:
        witness = ScalarWitness(
            site,
            pattern,
            lexical,
            cx,
            xml_whitespace_tokens(lexical) if isinstance(pattern, List) else (),
        )
        if witness not in self.witnesses:
            self.counter.consume("max_scalar_witnesses")
            self.witnesses[witness] = witness
        return self.witnesses[witness]

    def sequence(
        self,
        states: _Results,
        site: EvidenceOccurrence,
        event: str,
        name: str,
        lexical: str,
        cx: DatatypeContext,
    ) -> _Results:
        alternatives = []
        for previous, prefix in states.items():
            for residual, local in self.derive(previous, site, event, name, lexical, cx).items():
                alternatives.append((residual, self.node(site, residual, (prefix, local))))
            if event == "weak_text":
                alternatives.append((previous, self.node(site, previous, (prefix,))))
        return self.merge(site, alternatives)

    def derive(
        self,
        pattern: Pattern,
        site: EvidenceOccurrence,
        event: str,
        name: str,
        lexical: str,
        cx: DatatypeContext,
    ) -> _Results:
        """Evaluate local rules iteratively; never recurse on pattern depth."""
        results: dict[Pattern, _Results] = {}
        active: set[Pattern] = set()
        pending = [(pattern, False)]
        while pending:
            current, ready = pending.pop()
            if current in results:
                continue
            if not ready:
                active.add(current)
                pending.append((current, True))
                if event in ("text", "weak_text"):
                    children = self.text_children(current, lexical, cx)
                else:
                    children = _event_derivation_children(event, current)
                for child in reversed(children):
                    if child in active:
                        raise RuntimeError("cyclic RELAX NG evidence has no event boundary")
                    pending.append((child, False))
                continue
            active.remove(current)
            results[current] = self.combine(current, results, site, event, name, lexical, cx)
        return results[pattern]

    @staticmethod
    def text_children(pattern: Pattern, lexical: str, cx: DatatypeContext) -> tuple[Pattern, ...]:
        if isinstance(pattern, _After):
            return (pattern.first,)
        if isinstance(pattern, Ref):
            return (pattern.target,)
        if isinstance(pattern, (Choice, Interleave)):
            return (pattern.first, pattern.second)
        if isinstance(pattern, Group):
            return (pattern.first, pattern.second) if _nullable(pattern.first) else (pattern.first,)
        if isinstance(pattern, OneOrMore):
            return (pattern.pattern,)
        if isinstance(pattern, DataExcept) and cast(Datatype, pattern.datatype).allows(lexical, cx):
            return (pattern.excluded,)
        return ()

    def combine(
        self,
        pattern: Pattern,
        results: dict[Pattern, _Results],
        site: EvidenceOccurrence,
        event: str,
        name: str,
        lexical: str,
        cx: DatatypeContext,
    ) -> _Results:
        alternatives: list[tuple[Pattern, EvidenceNode]] = []

        def add(
            residual: Pattern,
            proofs: tuple[EvidenceNode, ...] = (),
            *,
            matched: Pattern | None = None,
            scalar: bool = False,
        ) -> None:
            if not isinstance(residual, NotAllowed):
                witnesses = (self.scalar(pattern, lexical, site, cx),) if scalar else ()
                alternatives.append(
                    (
                        residual,
                        self.node(site, residual, proofs, matched=matched, witnesses=witnesses),
                    )
                )

        def transform(child: Pattern, function: Callable[[Pattern], Pattern]) -> None:
            for residual, proof in results[child].items():
                add(function(residual), (proof,))

        is_text = event in ("text", "weak_text")
        if isinstance(pattern, Ref):
            transform(pattern.target, lambda residual: residual)
        elif isinstance(pattern, Choice):
            transform(pattern.first, lambda residual: residual)
            transform(pattern.second, lambda residual: residual)
        elif event == "end":
            if isinstance(pattern, _After) and _nullable(pattern.first):
                add(pattern.second)
        elif isinstance(pattern, _After):
            transform(
                pattern.first,
                lambda residual: (
                    _apply_after(lambda rest: _after(rest, pattern.second), residual)
                    if event == "open"
                    else _after(residual, pattern.second)
                ),
            )
        elif event == "close":
            if isinstance(pattern, (Group, Interleave)):
                constructor = group if isinstance(pattern, Group) else interleave
                for left, lp in results[pattern.first].items():
                    for right, rp in results[pattern.second].items():
                        add(constructor(left, right), (lp, rp))
            elif isinstance(pattern, OneOrMore):
                transform(pattern.pattern, one_or_more)
            elif not isinstance(pattern, Attribute):
                add(pattern)
        elif isinstance(pattern, (Group, Interleave)):
            first, second = pattern.first, pattern.second
            constructor = group if isinstance(pattern, Group) else interleave
            if event == "open":
                transform(
                    first,
                    lambda residual: _apply_after(lambda rest: constructor(rest, second), residual),
                )
                if isinstance(pattern, Interleave):
                    transform(
                        second,
                        lambda residual: _apply_after(
                            lambda rest: interleave(first, rest), residual
                        ),
                    )
                elif _nullable(first):
                    transform(second, lambda residual: residual)
            else:
                transform(first, lambda residual: constructor(residual, second))
                if isinstance(pattern, Interleave) or event == "attribute":
                    transform(second, lambda residual: constructor(first, residual))
                elif _nullable(first):
                    transform(second, lambda residual: residual)
        elif isinstance(pattern, OneOrMore):
            rest = choice(one_or_more(pattern.pattern), Empty())
            transform(
                pattern.pattern,
                lambda residual: (
                    _apply_after(lambda tail: group(tail, rest), residual)
                    if event == "open"
                    else group(residual, rest)
                ),
            )
        elif event == "open" and isinstance(pattern, Element):
            if pattern.name_class.contains(*expanded_name(name)):
                add(_after(pattern.pattern, Empty()), matched=pattern)
        elif event == "attribute" and isinstance(pattern, Attribute):
            if pattern.name_class.contains(*expanded_name(name)):
                if _nullable(pattern.pattern) and _whitespace(lexical):
                    add(Empty(), matched=pattern)
                for residual, proof in self.derive(
                    pattern.pattern, site, "text", "", lexical, cx
                ).items():
                    if _nullable(residual):
                        add(Empty(), (proof,), matched=pattern)
        elif is_text:
            if isinstance(pattern, Text):
                add(pattern, matched=pattern, scalar=True)
            elif isinstance(pattern, List):
                states = {pattern.pattern: self.node(site, pattern.pattern)}
                for index, span in enumerate(xml_whitespace_tokens(lexical)):
                    key = (site, index)
                    if key not in self.tokens:
                        token = EvidenceOccurrence(
                            site.element,
                            "token",
                            content_index=site.content_index,
                            token_index=index,
                            parent=site,
                            span=span,
                        )
                        self.tokens[key] = token
                        self.occurrences.append(token)
                    states = self.sequence(states, self.tokens[key], "text", "", span.text, cx)
                for residual, proof in states.items():
                    if _nullable(residual):
                        add(Empty(), (proof,), matched=pattern, scalar=True)
            elif isinstance(pattern, Data):
                if cast(Datatype, pattern.datatype).allows(lexical, cx):
                    add(Empty(), matched=pattern, scalar=True)
            elif isinstance(pattern, DataExcept):
                if cast(Datatype, pattern.datatype).allows(lexical, cx) and not any(
                    _nullable(residual) for residual in results[pattern.excluded]
                ):
                    add(Empty(), matched=pattern, scalar=True)
            elif isinstance(pattern, Value):
                datatype = cast(Datatype, pattern.datatype)
                if datatype.equal(pattern.lexical, _schema_datatype_context(datatype), lexical, cx):
                    add(Empty(), matched=pattern, scalar=True)
        return self.merge(site, alternatives)


def build_binding_evidence(
    root: RelaxNGElement,
    start: Pattern,
    *,
    datatype_context: DatatypeContext,
    limits: RelaxNGLimits | None,
    origins: Callable[[Pattern], OriginTable] | None = None,
) -> EvidenceGraph:
    """Build a complete proof for a valid lexical tree, or raise an internal error.

    Call only after a valid Phase 2 verdict. The precondition is rechecked
    without recovery; invalid trees cannot produce authoritative empty graphs.
    The optional origin lookup is consulted only for provenance materialization.
    Root fallback context never overrides any child's recorded context.
    """
    if not validate_element(root, start, limits=limits).valid:
        raise RuntimeError("binding evidence requires a complete accepting derivation")
    builder = _Builder(limits, origins)
    initial = EvidenceOccurrence(root, "document")
    builder.occurrences.append(initial)
    states = {start: builder.node(initial, start)}
    events = {"start_tag_open": "open", "start_tag_close": "close", "end_tag": "end"}
    for event in _instance_events([root]):
        owner = cast(RelaxNGElement, event.element)
        cx = DatatypeContext(owner.base_uri, owner.namespaces or {})
        if owner is root:
            cx = DatatypeContext(
                owner.base_uri if owner.base_uri is not None else datatype_context.base_uri,
                owner.namespaces if owner.namespaces is not None else datatype_context.namespaces,
            )
        site = EvidenceOccurrence(
            owner,
            "text" if event.kind == "weak_text" else event.kind,
            event.name,
            event.content_index,
        )
        builder.occurrences.append(site)
        states = builder.sequence(
            states,
            site,
            events.get(event.kind, event.kind),
            event.name or "",
            event.lexical or "",
            cx,
        )
    roots = tuple(proof for residual, proof in states.items() if _nullable(residual))
    if not roots:
        raise RuntimeError("binding evidence requires a complete accepting derivation")
    nodes, edges = _prune(builder.nodes, builder.edges, roots)
    retained_sites = {node.occurrence for node in nodes}
    return EvidenceGraph(
        roots,
        nodes,
        edges,
        tuple(site for site in builder.occurrences if site in retained_sites),
    )
