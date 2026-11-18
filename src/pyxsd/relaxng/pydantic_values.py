"""Scalar Pydantic annotations for compiled RELAX NG projection routes.

A standalone model position may have one schema-static scalar contract or
several.  Both cases are decided from the compiled projection plan, never from
a Python value's type or from one document's easiest branch:

- A unique decodable contract returns its ordinary Python value (text, decoded
  atomic value, or declared tuple/list shape).
- A unique contract without a complete decoder, and every ambiguous contract
  position, returns an immutable :class:`~pyxsd.relaxng.values.AlternativeValue`.
- Ambiguous atomic positions accept only a lexical string, an exact Phase 5
  ``#value`` tag, or an alternative snapshot.  A bare non-string scalar is
  rejected because it cannot name an ambiguous lexical spelling.

Every accepted tag or snapshot is treated as an assertion to recheck.  The
complete candidate set is recomputed from the static contract set, the lexical
text, and the scoped datatype context; supplied candidate IDs never filter or
authorize a subset.  Standalone snapshots carry a revision marker that no live
document revision can use.  No process-global registry, mutable caller context,
or document evidence participates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, cast

from pydantic import BeforeValidator, PlainSerializer, ValidationInfo, WithJsonSchema
from pyxsd.data_conversion.names import eqname_to_expanded
from pyxsd.exceptions import PyXSDError
from pyxsd.integrations import IntegrationError
from pyxsd.relaxng.contracts import (
    ScalarContract,
    candidate_id,
    list_shape,
    scalar_contract,
)
from pyxsd.relaxng.datatypes import (
    Datatype,
    DatatypeContext,
    UnsupportedDatatypeOperation,
    datatype_supports_decode,
)
from pyxsd.relaxng.json_schema import _ScalarSchemaMixin
from pyxsd.relaxng.json_values import parse_scalar_tag, scalar_to_relaxng_json
from pyxsd.relaxng.patterns import List, Pattern, nullable
from pyxsd.relaxng.projection import ProjectionPlan
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.validator import DerivationContext, text_derivative, xml_whitespace_tokens
from pyxsd.relaxng.values import (
    AlternativeValue,
    Candidate,
    _list_token_plan,
    encode_scalar,
)

__all__ = [
    "STANDALONE_REVISION",
    "ScalarCodec",
    "context_from_validation",
    "relaxng_scalar_annotation",
]

#: Revision carried by a value built outside any mutable document revision.
#: Live lexical revisions start at 1, so this marker cannot claim document
#: authority or become current merely by matching a number.
STANDALONE_REVISION = 0

_UNDECODABLE = object()
_QNAME_TYPE_NAMES = frozenset({"QName", "NOTATION"})


def _relaxng_context(info: ValidationInfo | None) -> DatatypeContext | None:
    """Return the supplied scoped RELAX NG context, or ``None`` when absent.

    The caller's mapping is validated and copied entry by entry.  Neither the
    outer context mapping nor the nested namespace mapping is mutated, and no
    object from the caller becomes part of the returned immutable context.
    """
    context = getattr(info, "context", None)
    if context is None:
        return None
    if not isinstance(context, Mapping):
        raise ValueError("Pydantic validation context must be a mapping")
    if "relaxng" not in context:
        return None
    scoped = context["relaxng"]
    if not isinstance(scoped, Mapping):
        raise ValueError("validation context 'relaxng' must be a mapping")
    unknown = set(scoped) - {"namespaces", "base_uri"}
    if unknown:
        raise ValueError(
            "validation context 'relaxng' accepts only 'namespaces' and 'base_uri'; "
            f"unexpected {sorted(unknown)!r}"
        )
    base_uri = scoped.get("base_uri")
    if base_uri is not None and not isinstance(base_uri, str):
        raise ValueError("validation context base_uri must be a string or None")
    namespaces = scoped.get("namespaces", {})
    if not isinstance(namespaces, Mapping):
        raise ValueError("validation context namespaces must be a mapping")
    copied: dict[str, str] = {}
    for prefix, uri in namespaces.items():
        if not isinstance(prefix, str) or not isinstance(uri, str):
            raise ValueError("validation context namespaces must map string prefixes to URIs")
        copied[prefix] = uri
    if "xml" in copied:
        raise ValueError("the xml namespace binding is implicit and cannot be supplied")
    return DatatypeContext(base_uri, copied)


def context_from_validation(info: ValidationInfo | None) -> DatatypeContext:
    """Return the scoped instance context for one validation.

    ``info.context["relaxng"]`` may provide ``namespaces`` and ``base_uri``.
    Missing context is :class:`DatatypeContext(None, {})`.  The returned value
    owns copies of every mapping, so a later caller mutation cannot change it.
    """
    supplied = _relaxng_context(info)
    return supplied if supplied is not None else DatatypeContext(None, {})


def _context_text(context: DatatypeContext) -> str:
    return f"base_uri={context.base_uri!r}, namespaces={dict(context.namespaces)!r}"


class ScalarCodec:
    """Schema-static scalar resolution for one compiled projection plan.

    The codec holds no process-global state and no document evidence.  Patterns
    come from :attr:`ProjectionPlan.encoding_patterns`; a missing or mismatched
    pattern is a schema/configuration defect and raises
    :class:`~pyxsd.integrations.IntegrationError` rather than guessing another
    datatype or falling back to ``str``.
    """

    def __init__(
        self,
        schema: RelaxNGSchema,
        plan: ProjectionPlan,
        grammar_fingerprint: str,
    ) -> None:
        if not isinstance(schema, RelaxNGSchema):
            raise TypeError("ScalarCodec requires a RelaxNGSchema")
        if not isinstance(plan, ProjectionPlan):
            raise TypeError("ScalarCodec requires a ProjectionPlan")
        if not isinstance(grammar_fingerprint, str) or not grammar_fingerprint:
            raise ValueError("ScalarCodec requires a nonempty grammar fingerprint")
        self.schema = schema
        self.plan = plan
        self.grammar_fingerprint = grammar_fingerprint
        self.encoding_patterns = plan.encoding_patterns
        self._pool = schema.grammar.graph

    def pattern_for(self, contract: ScalarContract) -> Pattern:
        """Resolve the one compiled encoding pattern for *contract*."""
        if not isinstance(contract, ScalarContract):
            raise TypeError("the scalar contract must be a ScalarContract")
        pattern = self.encoding_patterns.get(contract.pattern_identity)
        if pattern is None:
            raise IntegrationError(
                "no compiled encoding pattern for RELAX NG scalar contract "
                f"{contract.pattern_identity!r}; the annotation does not belong to "
                "this projection plan"
            )
        if scalar_contract(pattern) != contract:
            raise IntegrationError(
                "the compiled encoding pattern does not implement RELAX NG scalar "
                f"contract {contract.pattern_identity!r}"
            )
        return pattern

    def accepts(self, pattern: Pattern, lexical: str, context: DatatypeContext) -> bool:
        """Whether *pattern* accepts one lexical value in scoped *context*."""
        derivative = DerivationContext(context, self._pool)
        return nullable(text_derivative(pattern, lexical, derivative))


def _contracts(contracts: Any) -> tuple[ScalarContract, ...]:
    """Return deterministic unique contracts; reject non-contract input."""
    if not isinstance(contracts, tuple):
        contracts = tuple(contracts)
    unique: dict[str, ScalarContract] = {}
    for contract in contracts:
        if not isinstance(contract, ScalarContract):
            raise IntegrationError("scalar annotations require ScalarContract values")
        unique.setdefault(contract.pattern_identity, contract)
    return tuple(sorted(unique.values(), key=lambda item: item.pattern_identity))


def _decode_token(
    pattern: Pattern,
    lexical: str,
    context: DatatypeContext,
) -> object:
    """Decode one list token through its static scalar pattern, or fail closed."""
    contract = scalar_contract(pattern)
    if contract.kind != "atomic":
        return _UNDECODABLE
    datatype = getattr(pattern, "datatype", None)
    if not datatype_supports_decode(datatype):
        return _UNDECODABLE
    try:
        return cast(Datatype, datatype).decode(lexical, context)
    except UnsupportedDatatypeOperation:
        return _UNDECODABLE


def _decode_list(pattern: List, lexical: str, context: DatatypeContext) -> object:
    """Return the declared tuple/list shape of one whole-list lexical value.

    A statically plain fixed or homogeneous list is decoded from its compiled
    token patterns.  Any optional, heterogeneous-repeating, ambiguous, or
    otherwise non-plain arrangement has no single declared shape and remains
    undecodable; it is never collapsed to its lexical string.
    """
    shape = list_shape(pattern)
    if shape == "interpretation":
        return _UNDECODABLE
    plan = _list_token_plan(pattern)
    assert plan is not None
    tokens = xml_whitespace_tokens(lexical)
    if plan.kind == "fixed":
        if len(tokens) != len(plan.patterns):
            return _UNDECODABLE
        patterns = plan.patterns
    else:
        assert plan.patterns
        patterns = tuple(plan.patterns[0] for _ in tokens)
    values: list[Any] = []
    for token, token_pattern in zip(tokens, patterns, strict=True):
        value = _decode_token(token_pattern, token.text, context)
        if value is _UNDECODABLE:
            return _UNDECODABLE
        values.append(value)
    return tuple(values) if shape == "tuple" else values


def _candidate(
    contract: ScalarContract,
    pattern: Pattern,
    lexical: str,
    context: DatatypeContext,
    grammar_fingerprint: str,
) -> Candidate:
    """Build the complete candidate identity for one surviving contract."""
    identifier = candidate_id(grammar_fingerprint, contract)
    if contract.kind == "text":
        return Candidate(identifier, contract, None, False)
    if contract.kind == "list":
        decoded = _decode_list(cast(List, pattern), lexical, context)
        if decoded is _UNDECODABLE:
            return Candidate(identifier, contract, None, False)
        return Candidate(identifier, contract, decoded, True)
    datatype = getattr(pattern, "datatype", None)
    if not datatype_supports_decode(datatype):
        return Candidate(identifier, contract, None, False)
    try:
        decoded = cast(Datatype, datatype).decode(lexical, context)
    except UnsupportedDatatypeOperation:
        return Candidate(identifier, contract, None, False)
    return Candidate(identifier, contract, decoded, True)


def _surviving_candidates(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    lexical: str,
    context: DatatypeContext,
) -> tuple[Candidate, ...]:
    """Recompute every schema contract that accepts *lexical* in *context*."""
    candidates: list[Candidate] = []
    for contract in contracts:
        pattern = codec.pattern_for(contract)
        if not codec.accepts(pattern, lexical, context):
            continue
        candidates.append(
            _candidate(contract, pattern, lexical, context, codec.grammar_fingerprint)
        )
    if not candidates:
        raise ValueError(
            f"scalar lexical {lexical!r} is not accepted by any schema contract in "
            f"context {_context_text(context)}"
        )
    return tuple(sorted(candidates, key=lambda item: item.candidate_id))


def _wrapper(
    codec: ScalarCodec,
    candidates: tuple[Candidate, ...],
    lexical: str,
    context: DatatypeContext,
) -> AlternativeValue:
    """Build an immutable standalone alternative snapshot."""
    return AlternativeValue(
        lexical,
        candidates,
        context,
        codec.grammar_fingerprint,
        STANDALONE_REVISION,
    )


def _value_from_candidates(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    candidates: tuple[Candidate, ...],
    lexical: str,
    context: DatatypeContext,
) -> Any:
    """Return the stable value form for one recomputed candidate set."""
    if len(contracts) == 1:
        contract = contracts[0]
        assert len(candidates) == 1
        candidate = candidates[0]
        if contract.kind == "text":
            return lexical
        if candidate.decodable:
            return candidate.decoded
    return _wrapper(codec, candidates, lexical, context)


def _encode_assignment(
    contract: ScalarContract,
    pattern: Pattern,
    value: object,
    context: DatatypeContext,
    mode: str,
) -> str:
    """Encode one typed assignment without a ``str(value)`` fallback."""
    shaped = value
    if (
        contract.kind == "list"
        and mode == "json"
        # JSON has one sequence spelling.  A fixed tuple contract still returns
        # its declared tuple shape; only the wire input is normalized here.
        and isinstance(pattern, List)
        and list_shape(pattern) == "tuple"
        and isinstance(value, list)
    ):
        shaped = tuple(value)
    try:
        return encode_scalar(contract, shaped, context, pattern=pattern)
    except (TypeError, ValueError, UnsupportedDatatypeOperation) as error:
        raise ValueError(str(error)) from error


def _lexical_string(
    contract: ScalarContract,
    value: str,
    context: DatatypeContext,
) -> str:
    """Translate a JSON-profile EQName back to an in-scope lexical QName.

    ``pyxsd-json`` exports a decodable QName as ``Q{uri}local`` because JSON
    has no QName value type.  Re-encoding for validation never invents a
    prefix: the expanded URI must already have an in-scope binding, or be the
    current default namespace when the name is unprefixed.
    """
    if contract.kind != "atomic" or contract.type_name not in _QNAME_TYPE_NAMES:
        return value
    if not value.startswith("Q{"):
        return value
    try:
        expanded = eqname_to_expanded(value)
    except PyXSDError:
        return value
    end = value.find("}", 2)
    assert end >= 2
    uri = value[2:end]
    local = value[end + 1 :]
    if not uri:
        if context.namespaces.get("", "") != "":
            raise ValueError(
                f"QName {value!r} has no namespace, but the default namespace is "
                f"{context.namespaces.get('', '')!r}; no prefix can undeclare it"
            )
        return local
    expanded_uri, expanded_local = expanded[1:].split("}", 1)
    assert expanded_uri == uri and expanded_local == local
    for prefix in sorted(context.namespaces):
        if prefix and context.namespaces[prefix] == uri:
            return f"{prefix}:{local}"
    if context.namespaces.get("", None) == uri:
        return local
    raise ValueError(
        f"QName {value!r} requires an in-scope prefix for {uri!r} in context "
        f"{_context_text(context)}"
    )


def _lexical_from_input(
    codec: ScalarCodec,
    contract: ScalarContract,
    pattern: Pattern,
    value: object,
    context: DatatypeContext,
    mode: str,
) -> str:
    """Convert one unique-contract input to a checked lexical value."""
    if contract.kind == "text":
        if not isinstance(value, str):
            raise ValueError("a RELAX NG text scalar requires a string")
        lexical = value
    elif isinstance(value, str):
        # The explicit lexical form is checked against the same contract; it
        # does not select a datatype because this position is statically unique.
        lexical = _lexical_string(contract, value, context)
    else:
        lexical = _encode_assignment(contract, pattern, value, context, mode)
    if not codec.accepts(pattern, lexical, context):
        raise ValueError(
            f"scalar lexical {lexical!r} does not satisfy the {contract.kind} contract "
            f"in context {_context_text(context)}"
        )
    return lexical


def _unique_value(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    value: object,
    context: DatatypeContext,
    mode: str,
) -> Any:
    """Validate and normalize one unique static contract position."""
    contract = contracts[0]
    pattern = codec.pattern_for(contract)
    lexical = _lexical_from_input(codec, contract, pattern, value, context, mode)
    candidates = _surviving_candidates(codec, contracts, lexical, context)
    return _value_from_candidates(codec, contracts, candidates, lexical, context)


def _ambiguous_list_value(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    value: tuple[Any, ...] | list[Any],
    context: DatatypeContext,
    mode: str,
) -> Any:
    """Encode one statically representable list assignment at an ambiguous site.

    Only whole-list contracts can encode a sequence.  Every successful encoding
    must agree on one lexical form; otherwise the typed value does not identify
    one honest lexical spelling.  The encoded contract must survive the normal
    complete candidate recomputation, so a list value cannot silently fall back
    to a different atomic alternative.
    """
    encodings: list[tuple[ScalarContract, str]] = []
    for contract in contracts:
        if contract.kind != "list":
            continue
        pattern = codec.pattern_for(contract)
        try:
            lexical = _encode_assignment(contract, pattern, value, context, mode)
        except ValueError:
            continue
        encodings.append((contract, lexical))
    if not encodings:
        raise ValueError(
            "an ambiguous RELAX NG scalar position requires a lexical string, "
            "an exact #value tag, an AlternativeValue snapshot, or a list value "
            "accepted by one declared whole-list contract"
        )
    lexicals = {lexical for _, lexical in encodings}
    if len(lexicals) > 1:
        raise ValueError(
            "the supplied list value encodes to several lexical forms at an ambiguous "
            "RELAX NG scalar position; supply a lexical string or a tag instead"
        )
    lexical = next(iter(lexicals))
    candidates = _surviving_candidates(codec, contracts, lexical, context)
    encoded = {contract for contract, _ in encodings}
    if not any(candidate.contract in encoded for candidate in candidates):
        raise ValueError(
            "the supplied list value does not survive its declared whole-list contract "
            f"in context {_context_text(context)}"
        )
    return _wrapper(codec, candidates, lexical, context)


def _raw_value(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    value: object,
    context: DatatypeContext,
    mode: str,
) -> Any:
    """Validate a bare value; only a unique static contract accepts typed input."""
    if len(contracts) > 1:
        if isinstance(value, str):
            candidates = _surviving_candidates(codec, contracts, value, context)
            return _wrapper(codec, candidates, value, context)
        if isinstance(value, (tuple, list)):
            return _ambiguous_list_value(codec, contracts, value, context, mode)
        raise ValueError(
            "an ambiguous RELAX NG scalar position requires a lexical string, "
            "an exact #value tag, or an AlternativeValue snapshot; "
            f"got {type(value).__name__}"
        )
    return _unique_value(codec, contracts, value, context, mode)


def _scoped_context(
    embedded: DatatypeContext,
    supplied: DatatypeContext | None,
    *,
    kind: str,
) -> DatatypeContext:
    """Use an embedded tag/snapshot context, checking any supplied scope."""
    if supplied is not None and embedded != supplied:
        raise ValueError(
            f"scalar {kind} context {_context_text(embedded)} does not match the "
            f"validation context {_context_text(supplied)}"
        )
    return embedded


def _snapshot_value(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    snapshot: AlternativeValue,
    supplied: DatatypeContext | None,
) -> Any:
    """Recheck and normalize one immutable alternative snapshot."""
    if snapshot.grammar_fingerprint != codec.grammar_fingerprint:
        raise ValueError(
            "scalar snapshot grammar fingerprint does not match this compiled RELAX NG grammar"
        )
    context = _scoped_context(snapshot.context, supplied, kind="snapshot")
    candidates = _surviving_candidates(codec, contracts, snapshot.lexical, context)
    expected = tuple(candidate.candidate_id for candidate in candidates)
    claimed = tuple(candidate.candidate_id for candidate in snapshot.candidates)
    if expected != claimed:
        raise ValueError(
            "scalar snapshot candidate set does not match the complete recomputed "
            f"candidates {expected!r}"
        )
    expected_contracts = {candidate.candidate_id: candidate.contract for candidate in candidates}
    for candidate in snapshot.candidates:
        if expected_contracts.get(candidate.candidate_id) != candidate.contract:
            raise ValueError(
                f"scalar snapshot candidate {candidate.candidate_id!r} does not "
                "carry its recomputed contract"
            )
    return _value_from_candidates(codec, contracts, candidates, snapshot.lexical, context)


def _tag_value(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    value: Mapping[Any, Any],
    supplied: DatatypeContext | None,
) -> Any:
    """Parse and recheck one exact Phase 5 tagged scalar claim."""
    try:
        claim = parse_scalar_tag(dict(value), "")
    except PyXSDError as error:
        raise ValueError(str(error)) from error
    if claim.grammar != codec.grammar_fingerprint:
        raise ValueError(
            "scalar tag grammar fingerprint does not match this compiled RELAX NG grammar"
        )
    context = _scoped_context(claim.context, supplied, kind="tag")
    candidates = _surviving_candidates(codec, contracts, claim.lexical, context)
    expected = tuple(candidate.candidate_id for candidate in candidates)
    if claim.candidates != expected:
        raise ValueError(
            "scalar tag candidate set does not match the complete recomputed "
            f"candidates {expected!r}"
        )
    return _value_from_candidates(codec, contracts, candidates, claim.lexical, context)


def _validate(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    value: Any,
    info: ValidationInfo | None,
    *,
    may_be_empty: bool,
) -> Any:
    """Shared field validator for codec-backed scalar annotations."""
    if value is None:
        if may_be_empty:
            return None
        raise ValueError("scalar content is required; None denotes empty content")
    supplied = _relaxng_context(info)
    context = supplied if supplied is not None else DatatypeContext(None, {})
    mode = getattr(info, "mode", "python")
    if isinstance(value, AlternativeValue):
        return _snapshot_value(codec, contracts, value, supplied)
    if isinstance(value, Mapping) and "#value" in value:
        return _tag_value(codec, contracts, value, supplied)
    return _raw_value(codec, contracts, value, context, mode)


def _json_scalar(value: Any) -> Any:
    """Serialize one scalar for Python/JSON-mode dumps."""
    return scalar_to_relaxng_json(value, format="pyxsd-json")


class _FieldSchemaBuilder(_ScalarSchemaMixin):
    """Field-level reuse of the document profile's exact scalar shapes."""

    def __init__(self, plan: ProjectionPlan) -> None:
        self.plan = plan
        self.defs: dict[str, Any] = {}
        self.unvalidated: set[str] = set()
        self._tagged = True
        self._qname_eqname = True
        self._opaque_key = "relaxng-scalar"
        self._opaque_shell: dict[str, Any] | None = None

    def _opaque_scalar(self) -> dict[str, Any]:
        """A bounded inline shape; no dangling ``$defs`` reference in a model."""
        return {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
            ]
        }


def _merge_schemas(*schemas: dict[str, Any]) -> dict[str, Any]:
    """Flatten and deduplicate ``anyOf`` branches deterministically."""
    branches: list[dict[str, Any]] = []
    for schema in schemas:
        if set(schema) == {"anyOf"}:
            branches.extend(cast(list[dict[str, Any]], schema["anyOf"]))
        else:
            branches.append(schema)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for branch in branches:
        key = repr(branch)
        if key not in seen:
            seen.add(key)
            unique.append(branch)
    return unique[0] if len(unique) == 1 else {"anyOf": unique}


def _json_schema(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    *,
    may_be_empty: bool,
) -> dict[str, Any]:
    """Describe accepted scalar input for one Pydantic model annotation."""
    builder = _FieldSchemaBuilder(codec.plan)
    tag = builder._tag_schema()
    if len(contracts) > 1:
        schema = _merge_schemas({"type": "string"}, tag)
    else:
        contract = contracts[0]
        if contract.kind == "text":
            base = {"type": "string"}
        elif contract.kind == "list":
            base = builder._list_schema(contract)
        else:
            base = builder._atomic_schema(contract)
        # Every codec-backed position accepts its lexical form and an exact
        # tag; typed scalar positions additionally accept their Python/JSON
        # value shape.  The tag shape never freezes grammar or candidate IDs.
        schema = _merge_schemas(base, {"type": "string"}, tag)
    if may_be_empty:
        schema = _merge_schemas(schema, {"type": "null"})
    return schema


def _empty_string(value: Any) -> str:
    """Require the only lexical content an empty pattern admits."""
    if value is None:
        raise ValueError("empty RELAX NG content requires an empty string")
    if value != "":
        raise ValueError("empty RELAX NG content requires an empty string")
    return ""


def _codec_less_annotation(
    contracts: tuple[ScalarContract, ...],
    *,
    may_be_empty: bool,
) -> Any:
    """Text-only structural support when no compiled codec is supplied.

    Datatype operations require the compiled patterns and grammar identity, so
    an annotation without a codec fails at construction instead of guessing.
    """
    if any(contract.kind != "text" for contract in contracts):
        raise IntegrationError(
            "RELAX NG datatype scalar annotations require a ScalarCodec; "
            "compile the schema and pass its codec"
        )

    def validate_text(value: Any, info: ValidationInfo) -> Any:
        del info
        if value is None:
            if may_be_empty:
                return None
            raise ValueError("scalar content is required; None denotes empty content")
        if not isinstance(value, str):
            raise ValueError("a RELAX NG text scalar requires a string")
        return value

    schema: dict[str, Any] = {"type": "string"}
    if may_be_empty:
        schema = _merge_schemas(schema, {"type": "null"})
    return Annotated[
        Any,
        BeforeValidator(validate_text),
        PlainSerializer(_json_scalar, return_type=Any, when_used="json"),
        WithJsonSchema(schema),
    ]


def relaxng_scalar_annotation(
    contracts: Any,
    *,
    codec: ScalarCodec | None = None,
    may_be_empty: bool = False,
) -> Any:
    """Return one Pydantic annotation for a schema-static scalar position.

    ``contracts`` is the complete scalar contract set from the compiled route
    (root content, ``$`` content, or one attribute).  Without ``codec`` only
    text content can be described; every datatype contract raises
    :class:`~pyxsd.integrations.IntegrationError` instead of selecting an
    unchecked operation.  The adapter always supplies its codec.
    """
    unique = _contracts(contracts)
    if not unique:

        def validate_empty(value: Any, info: ValidationInfo) -> Any:
            del info
            if value is None and may_be_empty:
                return None
            return _empty_string(value)

        schema: dict[str, Any] = {"const": ""}
        if may_be_empty:
            schema = _merge_schemas(schema, {"type": "null"})
        return Annotated[
            Any,
            BeforeValidator(validate_empty),
            PlainSerializer(_json_scalar, return_type=Any, when_used="json"),
            WithJsonSchema(schema),
        ]
    if codec is None:
        return _codec_less_annotation(unique, may_be_empty=may_be_empty)
    if not isinstance(codec, ScalarCodec):
        raise TypeError("codec must be a ScalarCodec or None")
    for contract in unique:
        codec.pattern_for(contract)

    def validate(value: Any, info: ValidationInfo) -> Any:
        return _validate(codec, unique, value, info, may_be_empty=may_be_empty)

    return Annotated[
        Any,
        BeforeValidator(validate),
        PlainSerializer(_json_scalar, return_type=Any, when_used="json"),
        WithJsonSchema(_json_schema(codec, unique, may_be_empty=may_be_empty)),
    ]
