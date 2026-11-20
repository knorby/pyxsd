"""JSON-safe scalar values and immutable, occurrence-scoped wire assertions.

Tags retain only lexical input, scoped datatype context, and contract identities.
Verification reads the complete accepting evidence; it never decodes a value,
selects a branch, resolves a URI, or treats metadata as resource permission.
Tree adapters decide scalar positions and attach import-side locations.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from pyxsd.data_conversion.contracts import JsonValue, raise_conversion, validate_format
from pyxsd.data_conversion.names import expanded_to_eqname
from pyxsd.data_conversion.values import scalar_to_json
from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import XML_NS
from pyxsd.relaxng.contracts import candidate_id, scalar_contract
from pyxsd.relaxng.datatypes import DatatypeContext
from pyxsd.relaxng.values import AlternativeValue, ListInterpretation

if TYPE_CHECKING:
    from pyxsd.relaxng.document import RelaxNGDocument
    from pyxsd.relaxng.evidence import EvidenceOccurrence, ScalarWitness
    from pyxsd.relaxng.tree import RelaxNGElement

__all__ = [
    "ScalarTagClaim",
    "ScalarTagLocation",
    "parse_scalar_tag",
    "scalar_tag",
    "scalar_to_relaxng_json",
    "verify_tag_claims",
]


@dataclass(frozen=True)
class ScalarTagLocation:
    """An import-side scalar address, never part of the wire representation.

    ``element_path`` indexes element children from the root. Exactly one of an
    expanded attribute name or an ordered character-content index is present.
    """

    element_path: tuple[int, ...]
    attribute: str | None = None
    content_index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.element_path, tuple) or any(
            type(index) is not int or index < 0 for index in self.element_path
        ):
            raise ValueError("element_path must be a tuple of nonnegative integer child indexes")
        if (self.attribute is None) == (self.content_index is None):
            raise ValueError("exactly one of attribute or content_index must be set")
        if self.attribute is not None:
            if not isinstance(self.attribute, str) or not self.attribute:
                raise ValueError("attribute must be a nonempty expanded-name string")
            try:
                expanded_to_eqname(self.attribute)
            except PyXSDError as error:
                raise ValueError("attribute must be an expanded-name string") from error
        elif type(self.content_index) is not int or self.content_index < 0:
            raise ValueError("content_index must be a nonnegative integer")


def _context_from_json(value: object, pointer: str) -> DatatypeContext:
    """Validate a scoped frame without interpreting or accessing its URIs."""
    if not isinstance(value, dict) or value.keys() != {"base_uri", "namespaces"}:
        raise_conversion("scalar-tag-context", pointer, "expected exactly base_uri and namespaces")
    base_uri, namespaces = value["base_uri"], value["namespaces"]
    if base_uri is not None and not isinstance(base_uri, str):
        raise_conversion("scalar-tag-context", pointer, "base_uri must be a string or null")
    if not isinstance(namespaces, dict) or any(
        not isinstance(prefix, str) or not isinstance(uri, str)
        for prefix, uri in namespaces.items()
    ):
        raise_conversion(
            "scalar-tag-context", pointer, "namespaces must map string prefixes to URIs"
        )
    if "xml" in namespaces:
        raise_conversion(
            "scalar-tag-context", pointer, "the xml binding is implicit, not wire data"
        )
    return DatatypeContext(base_uri, namespaces)


def _context_json(context: DatatypeContext, pointer: str) -> dict[str, JsonValue]:
    """Omit only the standard implicit xml binding, preserving every other key."""
    namespaces: dict[str, JsonValue] = dict(context.namespaces)
    if "xml" in namespaces and namespaces.pop("xml") != XML_NS:
        raise_conversion("scalar-tag-context", pointer, "a rebound xml prefix is not representable")
    result: dict[str, JsonValue] = {"base_uri": context.base_uri, "namespaces": namespaces}
    # DatatypeContext freezes mappings but does not validate their wire types.
    validated = _context_from_json(result, pointer)
    return {"base_uri": validated.base_uri, "namespaces": dict(validated.namespaces)}


@dataclass(frozen=True)
class ScalarTagClaim:
    """An immutable parsed assertion, optionally bound by a tree importer."""

    pointer: str
    grammar: str
    lexical: str
    context: DatatypeContext
    candidates: tuple[str, ...]
    location: ScalarTagLocation | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.grammar, str) or not self.grammar:
            raise_conversion(
                "scalar-tag-grammar", self.pointer, "grammar must be a nonempty string"
            )
        if not isinstance(self.lexical, str):
            raise_conversion("scalar-tag-lexical", self.pointer, "lexical must be a string")
        if not isinstance(self.context, DatatypeContext):
            raise_conversion("scalar-tag-context", self.pointer, "expected a DatatypeContext")
        context = _context_from_json(_context_json(self.context, self.pointer), self.pointer)
        candidates = tuple(self.candidates)
        if (
            not candidates
            or any(not isinstance(item, str) or not item for item in candidates)
            or candidates != tuple(sorted(set(candidates)))
        ):
            raise_conversion(
                "scalar-tag-candidates",
                self.pointer,
                "candidate IDs must be nonempty, sorted and unique",
            )
        if self.location is not None and not isinstance(self.location, ScalarTagLocation):
            raise_conversion("scalar-tag-location", self.pointer, "expected a ScalarTagLocation")
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "candidates", candidates)


def scalar_tag(value: AlternativeValue | ListInterpretation) -> dict[str, JsonValue]:
    """Export an exact version-1 tag, with whole-list IDs rather than token IDs."""
    identifiers = (
        (item.candidate_id for item in value.candidates)
        if isinstance(value, AlternativeValue)
        else (value.candidate_id,)
    )
    return {
        "#value": {
            "version": 1,
            "grammar": value.grammar_fingerprint,
            "lexical": value.lexical,
            "context": _context_json(value.context, ""),
            "candidates": cast(list[JsonValue], sorted(set(identifiers))),
        }
    }


def scalar_to_relaxng_json(value: Any, *, format: str, typed: bool = True) -> JsonValue:
    """Convert a scalar for JSON-safe export, not for raw ``to_dict``.

    Wrappers tag only in typed native/profile formats. Conventional formats and
    lexical mode retain their authored characters without inventing metadata.
    Ordinary scalars reuse the shared precision and nonfinite-number rules;
    ordinary tuple-shaped lists become JSON arrays, like list-shaped lists.
    """
    selected_format = validate_format(format)
    if isinstance(value, (AlternativeValue, ListInterpretation)):
        if typed and selected_format in ("pyxsd", "pyxsd-json"):
            return scalar_tag(value)
        return value.lexical
    if typed and isinstance(value, (tuple, list)):
        return [scalar_to_relaxng_json(item, format=selected_format) for item in value]
    return scalar_to_json(value, format=selected_format, typed=typed)


def parse_scalar_tag(value: object, pointer: str) -> ScalarTagClaim:
    """Parse structure, including empty lexical text, without inferring a tree address."""
    if not isinstance(value, dict) or value.keys() != {"#value"}:
        raise_conversion("scalar-tag-invalid", pointer, "expected exactly one #value member")
    body = value["#value"]
    if not isinstance(body, dict) or body.keys() != {
        "version",
        "grammar",
        "lexical",
        "context",
        "candidates",
    }:
        raise_conversion("scalar-tag-invalid", pointer, "unexpected or missing #value body members")
    if type(body["version"]) is not int or body["version"] != 1:
        raise_conversion("scalar-tag-version", pointer, "only integer version 1 is supported")
    if not isinstance(body["candidates"], list):
        raise_conversion("scalar-tag-candidates", pointer, "candidate IDs must be a JSON array")
    return ScalarTagClaim(
        pointer,
        body["grammar"],
        body["lexical"],
        _context_from_json(body["context"], pointer),
        tuple(body["candidates"]),
    )


def _claim_mismatch(
    claim: ScalarTagClaim, witnesses: tuple[ScalarWitness, ...], fingerprint: str
) -> str | None:
    """Compare every surviving witness, without invoking datatype decoders."""
    if any(witness.lexical != claim.lexical for witness in witnesses):
        return "scalar-tag-lexical-mismatch"
    if any(
        _context_json(witness.context, claim.pointer) != _context_json(claim.context, claim.pointer)
        for witness in witnesses
    ):
        return "scalar-tag-context-mismatch"
    identifiers = tuple(
        sorted(
            {candidate_id(fingerprint, scalar_contract(witness.pattern)) for witness in witnesses}
        )
    )
    if identifiers != claim.candidates:
        return "scalar-tag-candidates-mismatch"
    return None


def verify_tag_claims(claims: Iterable[ScalarTagClaim], document: RelaxNGDocument) -> None:
    """Verify complete assertions only against valid, current typed evidence.

    Invalid instances retain their normal validation report and provide no
    authoritative verification. Bound locations never fall back to lexical
    matching; unbound claims must match exactly one scalar occurrence.
    """
    if document.report.has_errors:
        return
    claims = tuple(claims)
    pointer = claims[0].pointer if claims else ""
    if not document.typed:
        raise_conversion(
            "scalar-tag-evidence-required", pointer, "verification requires a typed document"
        )
    if not document.interpretations_current:
        raise_conversion(
            "scalar-tag-evidence-stale", pointer, "call revalidate() before verification"
        )
    graph = document.binding_evidence
    if graph is None:
        raise_conversion(
            "scalar-tag-evidence-required", pointer, "complete binding evidence is required"
        )
    fingerprint = document.schema._grammar_fingerprint
    paths: dict[RelaxNGElement, tuple[int, ...]] = {}
    pending: list[tuple[RelaxNGElement, tuple[int, ...]]] = [(document.root, ())]
    while pending:
        node, path = pending.pop()
        paths[node] = path
        pending.extend((child, (*path, index)) for index, child in enumerate(node.children))
    by_site: dict[EvidenceOccurrence, set[ScalarWitness]] = defaultdict(set)
    for edge in graph.retained_edges:
        for witness in edge.witnesses:
            if witness.occurrence.kind in ("attribute", "text"):
                by_site[witness.occurrence].add(witness)
    occurrences = {
        ScalarTagLocation(paths[site.element], site.name, site.content_index): tuple(witnesses)
        for site, witnesses in by_site.items()
    }
    bound: set[ScalarTagLocation] = set()
    for claim in claims:
        if claim.grammar != fingerprint:
            raise_conversion(
                "scalar-tag-grammar-mismatch", claim.pointer, "grammar fingerprint differs"
            )
        if claim.location is not None:
            if claim.location in bound:
                raise_conversion(
                    "scalar-tag-location-duplicate", claim.pointer, "location claimed twice"
                )
            bound.add(claim.location)
            witnesses = occurrences.get(claim.location)
            if witnesses is None:
                raise_conversion(
                    "scalar-tag-location-mismatch", claim.pointer, "no scalar at this location"
                )
            mismatch = _claim_mismatch(claim, witnesses, fingerprint)
            if mismatch is not None:
                raise_conversion(
                    mismatch, claim.pointer, "claim differs from complete accepting evidence"
                )
        else:
            matches = sum(
                _claim_mismatch(claim, witnesses, fingerprint) is None
                for witnesses in occurrences.values()
            )
            if matches != 1:
                code = (
                    "scalar-tag-occurrence-ambiguous"
                    if matches > 1
                    else "scalar-tag-occurrence-mismatch"
                )
                raise_conversion(
                    code, claim.pointer, "unbound claim must match exactly one occurrence"
                )
