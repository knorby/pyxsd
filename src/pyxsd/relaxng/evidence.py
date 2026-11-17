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
The graph also retains the scalar patterns each site reached before pruning,
so schema-static wrapper selection never depends on a surviving witness.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast

from pyxsd.relaxng.contracts import ScalarContract, scalar_contract
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
    start_tag_close_derivative,
    validate_element,
    xml_whitespace_tokens,
)

__all__ = [
    "EvidenceEdge",
    "EvidenceGraph",
    "EvidenceNode",
    "EvidenceOccurrence",
    "ScalarWitness",
    "TokenConstraintGraph",
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


@dataclass(frozen=True, eq=False)
class TokenConstraintGraph:
    """Compact token-assignment constraints for one whole-list interpretation.

    The graph is the part of the retained accepting proof that belongs to one
    whole-list witness: its derivation roots, the token-site states they reach,
    and the AND/OR edges among them.  :meth:`accepts` re-runs the same
    least-fixed-point viability used by whole-document pruning after removing
    witness edges that a caller rejects.  It never enumerates assignments, so
    the work per query is linear in the retained list subgraph, not exponential
    in the number of tokens.
    """

    site: EvidenceOccurrence
    roots: tuple[EvidenceNode, ...]
    nodes: tuple[EvidenceNode, ...]
    edges: tuple[EvidenceEdge, ...]
    token_occurrences: tuple[EvidenceOccurrence, ...]

    def __post_init__(self) -> None:
        if not self.roots:
            raise ValueError("a token constraint graph requires accepting roots")

    def accepts(self, matches: Callable[[ScalarWitness], bool]) -> bool:
        """Whether a complete token derivation survives the witness filter.

        *matches* decides whether one scalar witness may participate.  Edges
        carrying no witness are structural and remain.  A root is accepted when
        the filtered graph has a complete proof for it.
        """
        if not callable(matches):
            raise TypeError("token constraint matching must be callable")
        filtered = tuple(
            edge for edge in self.edges if all(matches(witness) for witness in edge.witnesses)
        )
        viable = _viability(filtered)
        return any(root in viable for root in self.roots)


def _contract_order(contract: ScalarContract) -> tuple[object, ...]:
    """Return a deterministic presentation order for schema scalar contracts."""
    return (
        contract.kind,
        contract.type_name or "",
        contract.library_uri or "",
        contract.pattern_identity,
        tuple((parameter.name, parameter.value) for parameter in contract.parameters),
        tuple(
            (restriction.kind, restriction.pattern_identity)
            for restriction in contract.restrictions
        ),
    )


@dataclass(frozen=True)
class EvidenceGraph:
    """Immutable accepting proof snapshot and deterministic marginal views.

    The private pre-pruning pattern table retains the scalar patterns each
    recorded occurrence could reach, so consumers can choose a schema-static
    wrapper shape without probing the instance lexical value.  It is
    deliberately independent of the retained witnesses.
    """

    accepting_roots: tuple[EvidenceNode, ...]
    retained_nodes: tuple[EvidenceNode, ...]
    retained_edges: tuple[EvidenceEdge, ...]
    occurrences: tuple[EvidenceOccurrence, ...]
    _schema_patterns: Mapping[EvidenceOccurrence, tuple[Pattern, ...]] = field(
        default_factory=dict, repr=False, hash=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "_schema_patterns", MappingProxyType(dict(self._schema_patterns)))

    def schema_contracts_at(self, occurrence: EvidenceOccurrence) -> tuple[ScalarContract, ...]:
        """Return the complete schema-possible scalar contracts at one site.

        Contracts are derived only from pre-pruning pattern reachability and
        deduplicated by their normalized semantic identity.  The table is
        recorded before accepting-viability pruning, so a pattern from a
        branch that later fails is still listed; it never depends on whether
        the instance lexical value satisfied a datatype.  These contracts are
        the stable input to schema-static wrapper selection.
        """
        return tuple(
            sorted(
                dict.fromkeys(
                    scalar_contract(pattern)
                    for pattern in self._schema_patterns.get(occurrence, ())
                ),
                key=_contract_order,
            )
        )

    def token_constraints(self, witness: ScalarWitness) -> TokenConstraintGraph:
        """Return the token subgraph belonging to one whole-list witness.

        The witness must be a retained whole-list witness in this graph.  Its
        proof nodes become the constraint roots; every retained premise they
        reach is retained, so another list contract at the same lexical site
        cannot contribute token candidates to this interpretation.  Empty lists
        yield the same structure with no token occurrences.
        """
        if not isinstance(witness, ScalarWitness):
            raise TypeError("token constraints require a ScalarWitness")
        if not isinstance(witness.pattern, List):
            raise TypeError("token constraints require a whole-list witness")
        site = witness.occurrence
        outgoing: dict[EvidenceNode, list[int]] = defaultdict(list)
        for index, edge in enumerate(self.retained_edges):
            outgoing[edge.predecessors[0]].append(index)
        roots = tuple(
            dict.fromkeys(
                edge.predecessors[0] for edge in self.retained_edges if witness in edge.witnesses
            )
        )
        if not roots:
            raise ValueError("the witness is not a retained whole-list proof")
        included_nodes: set[EvidenceNode] = set(roots)
        included_edges: set[int] = set()
        pending = list(roots)
        while pending:
            node = pending.pop()
            for index in outgoing.get(node, ()):
                included_edges.add(index)
                for child in self.retained_edges[index].successors:
                    if child not in included_nodes:
                        included_nodes.add(child)
                        pending.append(child)
        token_occurrences = tuple(
            sorted(
                (
                    occurrence
                    for occurrence in self.occurrences
                    if occurrence.kind == "token" and occurrence.parent is site
                ),
                key=lambda occurrence: cast(int, occurrence.token_index),
            )
        )
        return TokenConstraintGraph(
            site,
            roots,
            tuple(node for node in self.retained_nodes if node in included_nodes),
            tuple(self.retained_edges[index] for index in sorted(included_edges)),
            token_occurrences,
        )

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


def _viability(edges: Sequence[EvidenceEdge]) -> set[EvidenceNode]:
    """Least-fixed-point viable nodes over the given AND/OR edges.

    Zero-premise AND edges are local facts.  An AND needs every premise; an OR
    needs one.  Cycles without a founded fact stay dead.
    """
    dependents: dict[EvidenceNode, list[int]] = defaultdict(list)
    remaining = []
    ready: deque[int] = deque()
    for index, edge in enumerate(edges):
        remaining.append(len(edge.successors))
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
    return viable


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
    viable = _viability(edges)
    outgoing: dict[EvidenceNode, list[int]] = defaultdict(list)
    retained: dict[int, EvidenceEdge] = {}
    for index, edge in enumerate(edges):
        outgoing[edge.predecessors[0]].append(index)
    reachable: set[EvidenceNode] = set()
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
        self.schema_patterns: dict[EvidenceOccurrence, dict[int, Pattern]] = {}

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

    def record_schema_scalar(self, site: EvidenceOccurrence, pattern: Pattern) -> None:
        """Record one scalar pattern reachable at *site* before pruning.

        The record is grammar-side reachability, not a local datatype success:
        a ``data`` branch that rejects this lexical value is retained exactly
        like one that accepts it, so wrapper shape cannot depend on the value.
        Patterns nested in a ``data except`` exclusion are not scalar
        alternatives of the position and are skipped by the caller.
        """
        if isinstance(pattern, (Text, List, Data, DataExcept, Value)):
            self.schema_patterns.setdefault(site, {}).setdefault(id(pattern), pattern)

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
        if event == "close":
            # Close only filters still-required attributes. It does not choose
            # a content branch or produce scalar witnesses. Keep independent
            # choices factored in the semantic residual; consuming events will
            # introduce their OR proofs beneath the run's prefix conjunctions.
            # Distributing here also incorrectly turns (a|b)+ into a+|b+.
            residual = start_tag_close_derivative(pattern)
            return {} if isinstance(residual, NotAllowed) else {residual: self.node(site, residual)}
        results: dict[Pattern, _Results] = {}
        active: set[Pattern] = set()
        pending = [(pattern, False, False)]
        while pending:
            current, ready, excluded = pending.pop()
            if not excluded:
                self.record_schema_scalar(site, current)
            if current in results:
                continue
            if not ready:
                active.add(current)
                pending.append((current, True, excluded))
                if event in ("text", "weak_text"):
                    children = self.text_children(current, lexical, cx)
                    child_excluded = excluded or isinstance(current, DataExcept)
                else:
                    children = _event_derivation_children(event, current)
                    child_excluded = excluded
                for child in reversed(children):
                    if child in active:
                        raise RuntimeError("cyclic RELAX NG evidence has no event boundary")
                    pending.append((child, False, child_excluded))
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
    Every site uses the same recorded context as Phase 2. Present root
    metadata overrides ``datatype_context``. If a root namespace frame or base
    URI is absent, a nonempty fallback for that component raises ValueError:
    Phase 2 cannot receive that fallback, so applying it only here could
    manufacture witnesses the validation pass rejected. Empty fallbacks are
    harmless; no fallback is inherited by children. The caller tree is not
    mutated.
    """
    if (root.namespaces is None and datatype_context.namespaces) or (
        root.base_uri is None and datatype_context.base_uri is not None
    ):
        raise ValueError("nonempty datatype_context fallback is inconsistent with Phase 2 context")
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
    schema_patterns = {
        site: tuple(patterns.values())
        for site, patterns in builder.schema_patterns.items()
        if site in retained_sites
    }
    return EvidenceGraph(
        roots,
        nodes,
        edges,
        tuple(site for site in builder.occurrences if site in retained_sites),
        _schema_patterns=schema_patterns,
    )
