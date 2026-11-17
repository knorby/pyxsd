"""Immutable scalar alternatives built only from accepting RELAX NG evidence.

Candidates identify schema-static contracts, not values or whole derivations.
An alternative snapshot retains the original lexical characters, the surviving
candidate interpretations, the occurrence's immutable datatype context, the
compiled grammar fingerprint, and the parse revision.  It is deliberately
unhashable and has no implicit equality to a raw Python scalar: a lexical
``"12"`` is not the integer ``12`` merely because a datatype could decode it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import NotImplementedType
from typing import Any, cast

from pyxsd.relaxng.contracts import ScalarContract, candidate_id, scalar_contract
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    DatatypeSelector,
    UnsupportedDatatypeOperation,
    resolve_datatype_selector,
)
from pyxsd.relaxng.errors import AmbiguousValueError
from pyxsd.relaxng.evidence import ScalarWitness

__all__ = [
    "AlternativeValue",
    "Candidate",
    "alternative_value_from_witnesses",
    "candidate_for_witness",
]


@dataclass(frozen=True, eq=False)
class Candidate:
    """One surviving scalar contract, its optional decoded value, and its ID.

    ``decoded`` is ``None`` exactly when ``decodable`` is false.  A missing
    decoder never fabricates a string value or uses lexical equality.
    Candidates order lexically by :attr:`candidate_id`; decoded values may be
    unhashable, so candidates are not hashable either.
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


@dataclass(frozen=True, eq=False)
class AlternativeValue:
    """One immutable, non-preferential scalar interpretation snapshot."""

    lexical: str
    candidates: tuple[Candidate, ...]
    context: DatatypeContext
    grammar_fingerprint: str
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.lexical, str):
            raise TypeError("the lexical value must be a string")
        if not isinstance(self.context, DatatypeContext):
            raise TypeError("the value context must be a DatatypeContext")
        if not isinstance(self.grammar_fingerprint, str) or not self.grammar_fingerprint:
            raise ValueError("the grammar fingerprint must be a nonempty string")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise ValueError("the parse revision must be an integer")
        candidates = tuple(self.candidates)
        for item in candidates:
            if not isinstance(item, Candidate):
                raise TypeError("candidates must be Candidate values")
        if not candidates:
            raise ValueError("an alternative value cannot claim completeness with zero candidates")
        identifiers = [item.candidate_id for item in candidates]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("candidate IDs must be unique within an alternative value")
        object.__setattr__(
            self, "candidates", tuple(sorted(candidates, key=lambda item: item.candidate_id))
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
        )

    __hash__ = None  # type: ignore[assignment]

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
                if not item.decodable:
                    raise UnsupportedDatatypeOperation(
                        f"candidate {candidate_id!r} has no datatype decoder"
                    )
                return item.decoded
        raise ValueError(f"no candidate {candidate_id!r} survives at this scalar position")

    def as_python(self) -> Any:
        """Return the only decodable candidate's Python value, or fail explicitly."""
        if len(self.candidates) > 1:
            raise AmbiguousValueError(
                "several scalar candidates survive at this position; "
                "select one with as_candidate(candidate_id)"
            )
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


def candidate_for_witness(
    witness: ScalarWitness,
    *,
    grammar_fingerprint: str,
) -> Candidate:
    """Build the candidate for one accepting scalar witness.

    Decoding is optional.  A missing decoder (including every text and
    whole-list contract in this phase) yields ``decodable=False`` with no
    fabricated value.  Any other decoder failure propagates.
    """
    if not isinstance(witness, ScalarWitness):
        raise TypeError("candidate_for_witness requires a ScalarWitness")
    contract = scalar_contract(witness.pattern)
    identifier = candidate_id(grammar_fingerprint, contract)
    if contract.kind != "atomic":
        return Candidate(identifier, contract, None, False)
    datatype = cast(Datatype, witness.datatype)
    try:
        decoded = datatype.decode(witness.lexical, witness.context)
    except UnsupportedDatatypeOperation:
        return Candidate(identifier, contract, None, False)
    return Candidate(identifier, contract, decoded, True)


def alternative_value_from_witnesses(
    witnesses: Iterable[ScalarWitness],
    *,
    grammar_fingerprint: str,
    revision: int,
) -> AlternativeValue:
    """Build one alternative snapshot from the witnesses at a single site.

    All witnesses must belong to the same opaque occurrence, lexical content,
    and immutable context.  Equal contracts collapse to one candidate even
    when several accepting derivations produced them.  The wrapper retains
    every surviving candidate; it does not select a preferred datatype.
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
    candidates: dict[str, Candidate] = {}
    for witness in items:
        candidate = candidate_for_witness(witness, grammar_fingerprint=grammar_fingerprint)
        candidates.setdefault(candidate.candidate_id, candidate)
    return AlternativeValue(
        first.lexical,
        tuple(candidates.values()),
        first.context,
        grammar_fingerprint,
        revision,
    )
