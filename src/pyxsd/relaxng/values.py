"""Immutable scalar and list alternatives built only from accepting evidence.

Candidates identify schema-static contracts, not values or whole derivations.
An alternative snapshot retains the original lexical characters, the surviving
candidate interpretations, the occurrence's immutable datatype context, the
compiled grammar fingerprint, and the parse revision.  It is deliberately
unhashable and has no implicit equality to a raw Python scalar: a lexical
``"12"`` is not the integer ``12`` merely because a datatype could decode it.

A whole list keeps token spans, per-token marginal candidates, and a compact
constraint graph.  Selecting one datatype per token is checked against a
complete retained list derivation; the marginal views are never multiplied into
an alleged cross-product.  Equal Python values from distinct contracts remain
distinct candidates and interpretations.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType, NotImplementedType
from typing import Any, Literal, cast

from pyxsd.relaxng.contracts import (
    ListShape,
    ScalarContract,
    candidate_id,
    list_items_decodable,
    list_shape,
    scalar_contract,
)
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    DatatypeSelector,
    UnsupportedDatatypeOperation,
    datatype_supports_decode,
    resolve_datatype_selector,
)
from pyxsd.relaxng.errors import AmbiguousValueError
from pyxsd.relaxng.evidence import EvidenceGraph, ScalarWitness, TokenConstraintGraph
from pyxsd.relaxng.patterns import (
    Choice,
    Data,
    DataExcept,
    Empty,
    Group,
    List,
    OneOrMore,
    Pattern,
    Ref,
    Value,
)
from pyxsd.relaxng.validator import TokenSpan

__all__ = [
    "AlternativeValue",
    "Candidate",
    "ListInterpretation",
    "alternative_value_from_witnesses",
    "candidate_for_witness",
    "encode_scalar",
    "list_interpretation",
    "list_value",
]


@dataclass(frozen=True, eq=False, repr=False)
class Candidate:
    """One surviving scalar contract, its optional decoded value, and its ID.

    ``decodable`` is the discriminator.  A decodable candidate carries its
    decoder's result in ``decoded``; that result may be any Python object,
    including ``None``.  A nondecodable candidate has ``decoded is None`` and
    never carries a fabricated value.  A missing decoder therefore never falls
    back to a string value or lexical equality.  Candidates order lexically by
    :attr:`candidate_id`; decoded values may be unhashable, so candidates are
    not hashable either.
    """

    candidate_id: str
    contract: ScalarContract
    decoded: Any
    decodable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise ValueError("a candidate ID must be a nonempty string")
        if not isinstance(self.contract, ScalarContract):
            raise TypeError("a candidate contract must be a ScalarContract")
        if not isinstance(self.decodable, bool):
            raise TypeError("candidate decodability must be a bool")
        if not self.decodable and self.decoded is not None:
            raise ValueError("a nondecodable candidate cannot carry a fabricated value")
        if self.contract.kind == "text" and self.decodable:
            raise ValueError("text is a lexical contract and cannot be decoded")

    def __repr__(self) -> str:
        """Represent stable contract identity without a decoded object's repr."""
        return (
            f"Candidate(candidate_id={self.candidate_id!r}, "
            f"contract={self.contract!r}, decodable={self.decodable!r})"
        )

    def __eq__(self, other: object) -> bool | NotImplementedType:
        if not isinstance(other, Candidate):
            return NotImplemented
        return (
            self.candidate_id == other.candidate_id
            and self.contract == other.contract
            and self.decoded == other.decoded
            and self.decodable == other.decodable
        )

    def __lt__(self, other: object) -> bool | NotImplementedType:
        if not isinstance(other, Candidate):
            return NotImplemented
        return self.candidate_id < other.candidate_id

    __hash__ = None  # type: ignore[assignment]


@dataclass(frozen=True, eq=False, repr=False)
class ListInterpretation:
    """One whole-list contract with correlated token assignment evidence.

    ``token_candidates`` is a per-token marginal view, not a product: choosing
    one candidate at each position is only legal when the retained list
    derivation graph contains a complete assignment using them.  ``as_types``
    performs that whole-assignment check.  ``as_python`` returns a value only
    when exactly one complete decodable interpretation survives; equal Python
    values from distinct contracts are never merged.

    ``shape`` is the schema-static typing classification.  ``tuple`` and
    ``list`` are ordinary typed results when every token decodes; any other
    arrangement keeps this interpretation object so callers can inspect token
    alternatives instead of receiving an invented value.  An empty permitted
    list is a valid interpretation with no token groups, never a failure.
    """

    lexical: str
    token_spans: tuple[TokenSpan, ...]
    token_candidates: tuple[tuple[Candidate, ...], ...]
    context: DatatypeContext
    grammar_fingerprint: str
    revision: int
    contract: ScalarContract
    candidate_id: str
    shape: ListShape
    constraints: TokenConstraintGraph = field(repr=False, compare=False)
    _witness_candidates: Mapping[ScalarWitness, Candidate] = field(
        repr=False, compare=False, default_factory=dict
    )
    _static_decodable: bool = field(repr=False, compare=False, default=True)

    def __post_init__(self) -> None:
        if not isinstance(self.lexical, str):
            raise TypeError("a list interpretation's lexical value must be a string")
        if not isinstance(self.context, DatatypeContext):
            raise TypeError("a list interpretation's context must be a DatatypeContext")
        if not isinstance(self.grammar_fingerprint, str) or not self.grammar_fingerprint:
            raise ValueError("a list interpretation needs a nonempty grammar fingerprint")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise ValueError("a list interpretation's revision must be an integer")
        if not isinstance(self.contract, ScalarContract):
            raise TypeError("a list interpretation's contract must be a ScalarContract")
        if self.contract.kind != "list":
            raise ValueError("a list interpretation requires a whole-list contract")
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise ValueError("a list interpretation's candidate ID must be nonempty")
        if self.shape not in ("tuple", "list", "interpretation"):
            raise ValueError("a list interpretation's shape is not a list shape")
        if not isinstance(self.constraints, TokenConstraintGraph):
            raise TypeError("a list interpretation requires a token constraint graph")
        if not isinstance(self._static_decodable, bool):
            raise TypeError("list decoder eligibility must be a bool")
        spans = tuple(self.token_spans)
        for span in spans:
            if not isinstance(span, TokenSpan):
                raise TypeError("token spans must be TokenSpan values")
        groups: list[tuple[Candidate, ...]] = []
        for group in self.token_candidates:
            candidates = tuple(group)
            if not candidates:
                raise ValueError("every token position requires at least one candidate")
            for item in candidates:
                if not isinstance(item, Candidate):
                    raise TypeError("token candidates must be Candidate values")
            identifiers = [item.candidate_id for item in candidates]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("token candidate IDs must be unique at one token")
            contracts = [item.contract for item in candidates]
            if len(set(contracts)) != len(contracts):
                raise ValueError("token candidate contracts must be unique at one token")
            groups.append(tuple(sorted(candidates, key=lambda item: item.candidate_id)))
        if len(groups) != len(spans):
            raise ValueError("a list interpretation needs one candidate group per token span")
        object.__setattr__(self, "token_spans", spans)
        object.__setattr__(self, "token_candidates", tuple(groups))
        object.__setattr__(
            self,
            "context",
            DatatypeContext(self.context.base_uri, dict(self.context.namespaces)),
        )
        object.__setattr__(
            self,
            "_witness_candidates",
            MappingProxyType(dict(self._witness_candidates)),
        )

    def __eq__(self, other: object) -> bool | NotImplementedType:
        if not isinstance(other, ListInterpretation):
            return NotImplemented
        return (
            self.lexical == other.lexical
            and self.token_spans == other.token_spans
            and self.token_candidates == other.token_candidates
            and self.context == other.context
            and self.grammar_fingerprint == other.grammar_fingerprint
            and self.revision == other.revision
            and self.contract == other.contract
            and self.candidate_id == other.candidate_id
            and self.shape == other.shape
        )

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        """Represent IDs and shape, never a decoded candidate object's repr."""
        return (
            f"ListInterpretation(lexical={self.lexical!r}, "
            f"candidate_id={self.candidate_id!r}, shape={self.shape!r}, "
            f"token_candidate_ids="
            f"{tuple(tuple(item.candidate_id for item in group) for group in self.token_candidates)!r}, "
            f"revision={self.revision!r})"
        )

    def __str__(self) -> str:
        """Return the original list lexical characters."""
        return self.lexical

    @property
    def is_plain(self) -> bool:
        """Whether this is a statically plain, completely decodable list."""
        return (
            self.shape != "interpretation"
            and self._static_decodable
            and self._decoded() is not None
        )

    def _decoded(self) -> tuple[Any, ...] | list[Any] | None:
        """Return one complete plain decoded sequence, or None.

        Only a unique candidate at every token that all decode yields a value.
        A nondecodable token and a genuinely ambiguous token both return None;
        the caller distinguishes them for its error.  Empty homogeneous lists
        use the schema-static item decoder eligibility, so an empty instance
        cannot fabricate an ordinary list for a contract whose items could never
        decode.
        """
        if self.shape != "interpretation" and not self._static_decodable:
            return None
        values: list[Any] = []
        for group in self.token_candidates:
            if len(group) != 1:
                return None
            candidate = group[0]
            if not candidate.decodable:
                return None
            values.append(candidate.decoded)
        return list(values) if self.shape == "list" else tuple(values)

    def as_python(self) -> tuple[Any, ...] | list[Any]:
        """Return the only complete decodable interpretation, or fail explicitly."""
        value = self._decoded()
        if value is not None:
            return value
        if any(len(group) > 1 for group in self.token_candidates):
            raise AmbiguousValueError(
                "several token interpretations survive in this list; "
                "select a complete assignment with as_types(selectors)"
            )
        raise UnsupportedDatatypeOperation(
            "the only surviving token interpretation of this list has no datatype decoder"
        )

    def as_types(self, selectors: tuple[DatatypeSelector, ...]) -> tuple[Any, ...]:
        """Decode one datatype selector per token after a whole-assignment check.

        A selector names the atomic datatype expected at that token position.
        Per-token existence is insufficient: the selected contracts must occur
        together in a complete retained list derivation.  The check reuses the
        compact constraint graph, so it never enumerates token assignments.
        """
        chosen = tuple(selectors)
        if len(chosen) != len(self.token_spans):
            raise ValueError("as_types requires exactly one selector per token")
        selected: list[Candidate] = []
        for index, selector in enumerate(chosen):
            library_uri, type_name = resolve_datatype_selector(selector)
            matches = tuple(
                candidate
                for candidate in self.token_candidates[index]
                if candidate.contract.kind == "atomic"
                and candidate.contract.library_uri == library_uri
                and candidate.contract.type_name == type_name
            )
            if not matches:
                raise ValueError(
                    f"no surviving token candidate matches datatype "
                    f"{(library_uri, type_name)!r} at token {index}"
                )
            contracts = {candidate.contract for candidate in matches}
            if len(contracts) > 1:
                raise AmbiguousValueError(
                    f"token {index} has several distinct contracts for datatype "
                    f"{(library_uri, type_name)!r}; datatype selectors cannot "
                    "distinguish those token contracts"
                )
            candidate = matches[0]
            if not candidate.decodable:
                raise UnsupportedDatatypeOperation(
                    f"token candidate {candidate.candidate_id!r} has no datatype decoder"
                )
            selected.append(candidate)

        def matches_assignment(witness: ScalarWitness) -> bool:
            candidate = self._witness_candidates.get(witness)
            if candidate is None:
                return False
            if candidate.contract.kind == "list":
                return candidate.candidate_id == self.candidate_id
            index = cast(int, witness.occurrence.token_index)
            return candidate.contract == selected[index].contract

        if not self.constraints.accepts(matches_assignment):
            raise ValueError("the selected token datatypes do not form a complete list derivation")
        return tuple(candidate.decoded for candidate in selected)


@dataclass(frozen=True, eq=False, repr=False)
class AlternativeValue:
    """One immutable, non-preferential scalar interpretation snapshot."""

    lexical: str
    candidates: tuple[Candidate, ...]
    context: DatatypeContext
    grammar_fingerprint: str
    revision: int
    interpretations: tuple[ListInterpretation, ...] = ()
    document_evidence: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.lexical, str):
            raise TypeError("the lexical value must be a string")
        if not isinstance(self.context, DatatypeContext):
            raise TypeError("the value context must be a DatatypeContext")
        if not isinstance(self.grammar_fingerprint, str) or not self.grammar_fingerprint:
            raise ValueError("the grammar fingerprint must be a nonempty string")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise ValueError("the parse revision must be an integer")
        if not isinstance(self.document_evidence, bool):
            raise TypeError("document_evidence must be a bool")
        candidates = tuple(self.candidates)
        for item in candidates:
            if not isinstance(item, Candidate):
                raise TypeError("candidates must be Candidate values")
        if not candidates:
            raise ValueError("an alternative value cannot claim completeness with zero candidates")
        identifiers = [item.candidate_id for item in candidates]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("candidate IDs must be unique within an alternative value")
        contracts = [item.contract for item in candidates]
        if len(set(contracts)) != len(contracts):
            raise ValueError("candidate contracts must be unique within an alternative value")
        interpretations = tuple(self.interpretations)
        by_candidate = {item.candidate_id: item for item in candidates}
        seen: set[str] = set()
        for interpretation in interpretations:
            if not isinstance(interpretation, ListInterpretation):
                raise TypeError("list interpretations must be ListInterpretation values")
            if interpretation.candidate_id in seen:
                raise ValueError(
                    "list interpretation candidate IDs must be unique within an alternative value"
                )
            seen.add(interpretation.candidate_id)
            candidate = by_candidate.get(interpretation.candidate_id)
            if candidate is None:
                raise ValueError("a list interpretation requires a matching whole-list candidate")
            if candidate.contract != interpretation.contract:
                raise ValueError("a list interpretation's contract must match its candidate")
            if interpretation.lexical != self.lexical:
                raise ValueError("a list interpretation's lexical value must match its wrapper")
            if interpretation.context != self.context:
                raise ValueError("a list interpretation's context must match its wrapper")
            if interpretation.grammar_fingerprint != self.grammar_fingerprint:
                raise ValueError(
                    "a list interpretation's grammar fingerprint must match its wrapper"
                )
            if interpretation.revision != self.revision:
                raise ValueError("a list interpretation's revision must match its wrapper")
        object.__setattr__(
            self, "candidates", tuple(sorted(candidates, key=lambda item: item.candidate_id))
        )
        object.__setattr__(
            self,
            "interpretations",
            tuple(sorted(interpretations, key=lambda interpretation: interpretation.candidate_id)),
        )
        # Copy the immutable context so a later revision-tree replacement or
        # caller mapping cannot alter this snapshot.
        object.__setattr__(
            self,
            "context",
            DatatypeContext(self.context.base_uri, dict(self.context.namespaces)),
        )

    def __eq__(self, other: object) -> bool | NotImplementedType:
        if not isinstance(other, AlternativeValue):
            return NotImplemented
        return (
            self.lexical == other.lexical
            and self.candidates == other.candidates
            and self.context == other.context
            and self.grammar_fingerprint == other.grammar_fingerprint
            and self.revision == other.revision
            and self.interpretations == other.interpretations
        )

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        """Represent lexical content, candidate IDs, and revision, not decoded objects."""
        return (
            f"AlternativeValue(lexical={self.lexical!r}, "
            f"candidate_ids={tuple(item.candidate_id for item in self.candidates)!r}, "
            f"context={self.context!r}, grammar_fingerprint={self.grammar_fingerprint!r}, "
            f"revision={self.revision!r}, document_evidence={self.document_evidence!r})"
        )

    def __str__(self) -> str:
        """Return the original lexical characters, never a datatype choice."""
        return self.lexical

    def as_type(self, selector: DatatypeSelector) -> Any:
        """Decode the one surviving atomic datatype selected by *selector*.

        ``"xsd:name"`` means the RELAX NG XSD datatype-library URI, never an
        instance prefix binding; a tuple selects an explicit
        ``(library_uri, type_name)``.  Several distinct matching contracts
        require :meth:`as_candidate`.
        """
        library_uri, type_name = resolve_datatype_selector(selector)
        matches = tuple(
            item
            for item in self.candidates
            if item.contract.kind == "atomic"
            and item.contract.library_uri == library_uri
            and item.contract.type_name == type_name
        )
        return self._decoded_match(matches, f"datatype {(library_uri, type_name)!r}")

    def as_candidate(self, candidate_id: str) -> Any:
        """Decode the candidate with exactly *candidate_id*."""
        for item in self.candidates:
            if item.candidate_id == candidate_id:
                for interpretation in self.interpretations:
                    if interpretation.candidate_id == candidate_id:
                        return interpretation.as_python()
                if not item.decodable:
                    raise UnsupportedDatatypeOperation(
                        f"candidate {candidate_id!r} has no datatype decoder"
                    )
                return item.decoded
        raise ValueError(f"no candidate {candidate_id!r} survives at this scalar position")

    def as_interpretation(self, candidate_id: str) -> ListInterpretation:
        """Return the list interpretation attached to *candidate_id*."""
        for interpretation in self.interpretations:
            if interpretation.candidate_id == candidate_id:
                return interpretation
        for item in self.candidates:
            if item.candidate_id == candidate_id:
                raise ValueError(f"candidate {candidate_id!r} is not a list interpretation")
        raise ValueError(f"no candidate {candidate_id!r} survives at this scalar position")

    def as_python(self) -> Any:
        """Return the only decodable candidate's Python value, or fail explicitly."""
        if len(self.candidates) > 1:
            raise AmbiguousValueError(
                "several scalar candidates survive at this position; "
                "select one with as_candidate(candidate_id)"
            )
        if self.interpretations:
            return self.interpretations[0].as_python()
        return self._decoded_match(self.candidates, "the only scalar candidate")

    def _decoded_match(self, matches: tuple[Candidate, ...], description: str) -> Any:
        if not matches:
            raise ValueError(f"no surviving scalar candidate matches {description}")
        contracts = {item.contract for item in matches}
        if len(contracts) > 1:
            raise AmbiguousValueError(
                f"{description} matches several distinct scalar contracts; "
                "select one with as_candidate(candidate_id)"
            )
        item = matches[0]
        if not item.decodable:
            raise UnsupportedDatatypeOperation(
                f"candidate {item.candidate_id!r} has no datatype decoder"
            )
        return item.decoded


def _decode_candidate(
    identifier: str,
    contract: ScalarContract,
    datatype: object,
    lexical: str,
    context: DatatypeContext,
) -> Candidate:
    """Build one candidate from a datatype witness, failing closed.

    A datatype that does not explicitly declare ``supports_decode`` is never
    invoked, regardless of whether it overrides ``decode``.  A declared decoder
    that reports ``UnsupportedDatatypeOperation`` remains nondecodable; any
    other declared-decoder failure propagates as before.
    """
    if not datatype_supports_decode(datatype):
        return Candidate(identifier, contract, None, False)
    try:
        decoded = cast(Datatype, datatype).decode(lexical, context)
    except UnsupportedDatatypeOperation:
        return Candidate(identifier, contract, None, False)
    return Candidate(identifier, contract, decoded, True)


@dataclass(frozen=True)
class _TokenContractPlan:
    """The fixed or homogeneous scalar token plan of a plain list contract."""

    kind: Literal["fixed", "list"]
    patterns: tuple[Pattern, ...]


def _list_token_children(pattern: Pattern) -> tuple[Pattern, ...]:
    """Return the operands that decide a plain list token plan."""
    if isinstance(pattern, Ref):
        return (pattern.target,)
    if isinstance(pattern, (Group, Choice)):
        return (pattern.first, pattern.second)
    if isinstance(pattern, OneOrMore):
        return (pattern.pattern,)
    return ()


def _combine_list_tokens(
    pattern: Pattern, results: dict[int, _TokenContractPlan | None]
) -> _TokenContractPlan | None:
    """Combine already-analysed list content into one token plan."""
    if isinstance(pattern, Ref):
        return results[id(pattern.target)]
    if isinstance(pattern, Empty):
        return _TokenContractPlan("fixed", ())
    if isinstance(pattern, (Data, DataExcept, Value)):
        return _TokenContractPlan("fixed", (pattern,))
    if isinstance(pattern, Group):
        first = results[id(pattern.first)]
        second = results[id(pattern.second)]
        if first is None or second is None:
            return None
        if first.kind == second.kind == "fixed":
            return _TokenContractPlan("fixed", (*first.patterns, *second.patterns))
        return None
    if isinstance(pattern, OneOrMore):
        item = results[id(pattern.pattern)]
        if item is not None and item.kind == "fixed" and len(item.patterns) == 1:
            return _TokenContractPlan("list", item.patterns)
        return None
    if isinstance(pattern, Choice):
        first = results[id(pattern.first)]
        second = results[id(pattern.second)]
        if first is not None and second is not None:
            if first.kind == "fixed" and not first.patterns and second.kind == "list":
                return second
            if second.kind == "fixed" and not second.patterns and first.kind == "list":
                return first
        return None
    return None


def _list_token_plan(pattern: List) -> _TokenContractPlan | None:
    """Derive one plain token plan from a whole-list pattern, or fail closed.

    Only the static ``tuple`` and homogeneous ``list`` shapes have a unique
    token contract per position.  The pattern graph of those shapes is acyclic,
    so this iterative post-order walk terminates on deep grammars as well.
    """
    content = pattern.pattern
    results: dict[int, _TokenContractPlan | None] = {}
    pending: list[tuple[Pattern, bool]] = [(content, False)]
    while pending:
        node, ready = pending.pop()
        key = id(node)
        if key in results:
            continue
        if not ready:
            pending.append((node, True))
            for child in reversed(_list_token_children(node)):
                if id(child) not in results:
                    pending.append((child, False))
            continue
        results[key] = _combine_list_tokens(node, results)
    return results[id(content)]


def _encode_list_token(
    pattern: Pattern,
    value: object,
    context: DatatypeContext,
    index: int,
) -> str:
    """Encode and boundary-check one list item against its token contract."""
    contract = scalar_contract(pattern)
    lexical = encode_scalar(contract, value, context, pattern=pattern)
    if not lexical:
        raise ValueError(f"list token {index} encodes to an empty string")
    if any(character in " \t\n\r" for character in lexical):
        raise ValueError(f"list token {index} encodes with XML whitespace")
    return lexical


def _encode_list_value(
    contract: ScalarContract,
    value: object,
    context: DatatypeContext,
    pattern: List,
) -> str:
    """Encode a declared tuple/list shape with XML whitespace separators."""
    shape = list_shape(pattern)
    if shape == "interpretation":
        raise UnsupportedDatatypeOperation(
            "the whole-list contract has no uniquely derivable token contract; "
            "its optional, heterogeneous, or ambiguous content is a structural gap"
        )
    plan = cast(_TokenContractPlan, _list_token_plan(pattern))
    if shape == "tuple":
        if not isinstance(value, tuple):
            raise TypeError("a fixed tuple-shaped RELAX NG list requires a tuple")
        if len(value) != len(plan.patterns):
            raise ValueError(
                f"the fixed RELAX NG list requires {len(plan.patterns)} tokens, not {len(value)}"
            )
        item_patterns = plan.patterns
    else:
        if not isinstance(value, list):
            raise TypeError("a homogeneous RELAX NG list requires a list")
        item_patterns = tuple(plan.patterns[0] for _ in value)
    return " ".join(
        _encode_list_token(item_pattern, item, context, index)
        for index, (item_pattern, item) in enumerate(zip(item_patterns, value, strict=True))
    )


def encode_scalar(
    contract: ScalarContract,
    value: object,
    context: DatatypeContext,
    *,
    pattern: Pattern,
) -> str:
    """Encode one typed assignment with its compile-time encoding pattern.

    Text contracts accept exactly a ``str``.  Atomic contracts invoke the
    datatype encoder compiled into *pattern*.  A whole-list contract derives
    token contracts only from the compiled list pattern.  The pattern must
    implement *contract*; there is deliberately no ``str(value)`` fallback, no
    instance lookup, and no boolean-to-number coercion.  Encoders that lack
    ``encode`` raise :class:`UnsupportedDatatypeOperation`.
    """
    if not isinstance(contract, ScalarContract):
        raise TypeError("encode_scalar requires a ScalarContract")
    if not isinstance(context, DatatypeContext):
        raise TypeError("encode_scalar requires a DatatypeContext")
    if not isinstance(pattern, Pattern):
        raise TypeError("encode_scalar requires a compiled pattern")
    try:
        observed = scalar_contract(pattern)
    except (TypeError, UnsupportedDatatypeOperation) as error:
        raise TypeError("the supplied pattern is not a RELAX NG scalar pattern") from error
    if observed != contract:
        raise ValueError("the supplied pattern does not implement the scalar contract")
    if contract.kind == "text":
        if not isinstance(value, str):
            raise TypeError("a RELAX NG text scalar assignment requires a string")
        return value
    if contract.kind == "list":
        return _encode_list_value(contract, value, context, cast(List, pattern))
    datatype = cast(Datatype, cast(Any, pattern).datatype)
    lexical = datatype.encode(value, context)
    if not isinstance(lexical, str):
        raise TypeError("a datatype encoder must return a lexical string")
    return lexical


def candidate_for_witness(
    witness: ScalarWitness,
    *,
    grammar_fingerprint: str,
) -> Candidate:
    """Build the candidate for one accepting scalar witness.

    Decoding is optional.  A missing decoder declaration (including every text
    and whole-list contract in this phase) yields ``decodable=False`` with no
    fabricated value and no ``decode`` call.  A declared decoder that reports
    unsupported remains lexical; any other declared-decoder failure propagates.
    """
    if not isinstance(witness, ScalarWitness):
        raise TypeError("candidate_for_witness requires a ScalarWitness")
    contract = scalar_contract(witness.pattern)
    identifier = candidate_id(grammar_fingerprint, contract)
    if contract.kind != "atomic":
        return Candidate(identifier, contract, None, False)
    return _decode_candidate(
        identifier,
        contract,
        witness.datatype,
        witness.lexical,
        witness.context,
    )


def alternative_value_from_witnesses(
    witnesses: Iterable[ScalarWitness],
    *,
    grammar_fingerprint: str,
    revision: int,
    list_interpretations: Iterable[ListInterpretation] = (),
) -> AlternativeValue:
    """Build one alternative snapshot from the witnesses at a single site.

    All witnesses must belong to the same opaque occurrence, lexical content,
    and immutable context.  Equal contracts collapse to one candidate even
    when several accepting derivations produced them.  The wrapper retains
    every surviving candidate; it does not select a preferred datatype.

    A whole-list witness decodes only when a matching ``ListInterpretation``
    yields one complete decodable token assignment.  Supplying no interpretation
    keeps the list candidate lexical, matching the scalar-only construction
    boundary.
    """
    items = tuple(witnesses)
    if not items:
        raise ValueError("at least one scalar witness is required")
    first = items[0]
    if not isinstance(first, ScalarWitness):
        raise ValueError("each candidate must come from a scalar witness")
    for witness in items[1:]:
        if not isinstance(witness, ScalarWitness):
            raise ValueError("each candidate must come from a scalar witness")
        if witness.occurrence is not first.occurrence:
            raise ValueError("candidates must come from one occurrence")
        if witness.lexical != first.lexical:
            raise ValueError("candidates at one occurrence must share one lexical value")
        if witness.context != first.context:
            raise ValueError("candidates at one occurrence must share one context")
    interpretations = tuple(list_interpretations)
    for interpretation in interpretations:
        if not isinstance(interpretation, ListInterpretation):
            raise TypeError("list interpretations must be ListInterpretation values")
    by_candidate = {
        interpretation.candidate_id: interpretation for interpretation in interpretations
    }
    candidates: dict[str, Candidate] = {}
    for witness in items:
        candidate = candidate_for_witness(witness, grammar_fingerprint=grammar_fingerprint)
        attached = by_candidate.get(candidate.candidate_id)
        if candidate.contract.kind == "list" and attached is not None:
            decoded = attached._decoded()
            candidate = (
                Candidate(candidate.candidate_id, candidate.contract, decoded, True)
                if decoded is not None
                else Candidate(candidate.candidate_id, candidate.contract, None, False)
            )
        candidates.setdefault(candidate.candidate_id, candidate)
    return AlternativeValue(
        first.lexical,
        tuple(candidates.values()),
        first.context,
        grammar_fingerprint,
        revision,
        interpretations,
        True,
    )


def list_interpretation(
    witness: ScalarWitness,
    graph: EvidenceGraph,
    *,
    grammar_fingerprint: str,
    revision: int,
) -> ListInterpretation:
    """Build the correlated token view of one retained whole-list witness.

    Token candidates are collected only from that witness's compact constraint
    subgraph, so another list contract at the same lexical site cannot leak
    candidates into this interpretation.  No token assignment is constructed
    here; the graph retains the correlations for later whole-assignment checks.
    """
    if not isinstance(witness, ScalarWitness):
        raise TypeError("a list interpretation requires a ScalarWitness")
    if not isinstance(graph, EvidenceGraph):
        raise TypeError("a list interpretation requires an EvidenceGraph")
    contract = scalar_contract(witness.pattern)
    if contract.kind != "list":
        raise TypeError("a list interpretation requires a whole-list witness")
    constraints = graph.token_constraints(witness)
    by_token: dict[int, list[ScalarWitness]] = defaultdict(list)
    for edge in constraints.edges:
        for token_witness in edge.witnesses:
            occurrence = token_witness.occurrence
            if occurrence.kind == "token" and occurrence.parent is constraints.site:
                by_token[cast(int, occurrence.token_index)].append(token_witness)
    whole_candidate = candidate_for_witness(witness, grammar_fingerprint=grammar_fingerprint)
    witness_candidates: dict[ScalarWitness, Candidate] = {witness: whole_candidate}
    token_candidates: list[tuple[Candidate, ...]] = []
    for occurrence in constraints.token_occurrences:
        candidates: dict[str, Candidate] = {}
        for token_witness in by_token.get(cast(int, occurrence.token_index), ()):
            candidate = candidate_for_witness(
                token_witness, grammar_fingerprint=grammar_fingerprint
            )
            candidates.setdefault(candidate.candidate_id, candidate)
            witness_candidates[token_witness] = candidate
        token_candidates.append(tuple(candidates.values()))
    return ListInterpretation(
        witness.lexical,
        tuple(witness.token_spans),
        tuple(token_candidates),
        witness.context,
        grammar_fingerprint,
        revision,
        contract,
        whole_candidate.candidate_id,
        list_shape(witness.pattern),
        constraints,
        witness_candidates,
        list_items_decodable(witness.pattern),
    )


def list_value(
    witnesses: Iterable[ScalarWitness],
    graph: EvidenceGraph,
    *,
    schema_contracts: Iterable[ScalarContract],
    grammar_fingerprint: str,
    revision: int,
) -> tuple[Any, ...] | list[Any] | AlternativeValue:
    """Return the schema-static value form of one whole-list scalar position.

    ``schema_contracts`` is the complete set of scalar contracts the position
    can have in the grammar, including atomic or text alternatives on the same
    route, not merely this instance's survivors.  One plain, completely
    decodable list contract returns an ordinary tuple or list only when it is
    the sole schema-possible contract.  Any additional scalar branch, a
    schema-ambiguous position, and a list contract without a plain complete
    decoding return an immutable :class:`AlternativeValue` whose whole-list
    candidate carries the attached interpretation.  Selection is therefore
    independent of which branch this particular lexical value made easiest.
    """
    if not isinstance(graph, EvidenceGraph):
        raise TypeError("a list value requires an EvidenceGraph")
    items = tuple(witnesses)
    if not items:
        raise ValueError("a list value requires at least one surviving witness")
    for witness in items:
        if not isinstance(witness, ScalarWitness):
            raise TypeError("list values require ScalarWitness values")
    contracts = tuple(dict.fromkeys(schema_contracts))
    if not contracts:
        raise ValueError("a list value requires at least one schema contract")
    for contract in contracts:
        if not isinstance(contract, ScalarContract):
            raise TypeError("schema contracts must be ScalarContract values")
    if not any(contract.kind == "list" for contract in contracts):
        raise ValueError("a list value requires at least one whole-list schema contract")
    witness_by_contract: dict[ScalarContract, ScalarWitness] = {}
    for witness in items:
        contract = scalar_contract(witness.pattern)
        if contract.kind != "list":
            raise ValueError("list values require whole-list witnesses")
        if contract not in contracts:
            raise ValueError("a surviving witness is not among the schema contracts")
        witness_by_contract.setdefault(contract, witness)
    if len(contracts) == 1:
        interpretation = list_interpretation(
            next(iter(witness_by_contract.values())),
            graph,
            grammar_fingerprint=grammar_fingerprint,
            revision=revision,
        )
        if interpretation.is_plain:
            return interpretation.as_python()
    interpretations = tuple(
        list_interpretation(
            witness,
            graph,
            grammar_fingerprint=grammar_fingerprint,
            revision=revision,
        )
        for witness in witness_by_contract.values()
    )
    return alternative_value_from_witnesses(
        items,
        grammar_fingerprint=grammar_fingerprint,
        revision=revision,
        list_interpretations=interpretations,
    )
