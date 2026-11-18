"""Scalar Pydantic annotations for compiled RELAX NG projection routes.

A standalone model position may have one schema-static scalar contract or
several.  Both cases are decided from the compiled projection plan, never from
a Python value's type or from one document's easiest branch:

- A unique decodable contract returns its ordinary Python value (text, decoded
  atomic value, or declared tuple/list shape).
- A unique contract without a complete decoder, and every ambiguous contract
  position, returns an immutable :class:`~pyxsd.relaxng.values.AlternativeValue`.
- Ambiguous atomic positions accept only a lexical string, an exact Phase 5
  ``#value`` tag, or an alternative snapshot.  A bare non-string scalar,
  tuple, or list is rejected because it cannot name an ambiguous lexical
  spelling.  Typed list values are accepted only at a unique statically
  representable list contract.

Every accepted tag or snapshot is treated as an assertion to recheck.  The
complete candidate set is recomputed from the static contract set, the lexical
text, and the scoped datatype context; supplied candidate IDs never filter or
authorize a subset.  Standalone snapshots carry a revision marker that no live
document revision can use.  A bare :class:`ListInterpretation` is accepted only
as one scoped whole-list assertion after grammar/context/contract/candidate
recomputation; its parse-local token constraint graph is not retained in the
standalone value.  No process-global registry, mutable caller context, or
document evidence participates.

A unique contract returns an ordinary value only when the decoded value can be
re-encoded to a value-equal lexical form.  Decoder-only contracts return an
immutable wrapper, so a value from a document can always be validated again.
Bare strings are always lexical input: for a permissive decoder-only contract a
decoded-looking string cannot be distinguished from lexical text and is decoded
as lexical.  Exact decoded provenance for such a contract requires a tag or
snapshot.  A non-string decoded Python value for a decoder-only contract is not
supported because no honest lexical form exists.
"""

from __future__ import annotations

import base64
import inspect
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Annotated, Any, Literal, cast

from pydantic.main import IncEx

from pydantic import (
    BaseModel,
    BeforeValidator,
    SerializationInfo,
    ValidationInfo,
    WithJsonSchema,
    WrapSerializer,
)
from pyxsd.data_conversion.names import eqname_to_expanded, expanded_to_eqname
from pyxsd.exceptions import PyXSDError
from pyxsd.integrations import IntegrationError
from pyxsd.relaxng.contracts import (
    ScalarContract,
    candidate_id,
    list_items_decodable,
    list_shape,
    scalar_contract,
)
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    XSD_LIBRARY,
    Datatype,
    DatatypeContext,
    UnsupportedDatatypeOperation,
    datatype_supports_decode,
    datatype_supports_encode,
)
from pyxsd.relaxng.json_values import parse_scalar_tag, scalar_tag
from pyxsd.relaxng.patterns import (
    Choice,
    Empty,
    List,
    OneOrMore,
    Pattern,
    nullable,
)
from pyxsd.relaxng.projection import ProjectionPlan
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.validator import DerivationContext, text_derivative, xml_whitespace_tokens
from pyxsd.relaxng.values import (
    AlternativeValue,
    Candidate,
    ListInterpretation,
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
_UNSET = object()
#: Parameters supported by the installed Pydantic baseline.  Newer optional
#: parameters are forwarded only when this signature advertises them.
_MODEL_DUMP_PARAMETERS = frozenset(inspect.signature(BaseModel.model_dump).parameters)
_QNAME_TYPE_NAMES = frozenset({"QName", "NOTATION"})
_XSD_INTEGER_TYPES = frozenset(
    {
        "byte",
        "int",
        "integer",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "short",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)
_XSD_FLOAT_TYPES = frozenset({"double", "float"})
_XSD_LIST_VALUED_TYPES = frozenset({"ENTITIES", "IDREFS", "NMTOKENS"})
_DECIMAL_PATTERN = r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$"
_INTEGER_PATTERN = r"^[+-]?[0-9]+$"
_BOOLEAN_PATTERN = r"^(?:true|false|1|0)$"
_FLOAT_PATTERN = r"^(?:[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)|-?INF|NaN)$"
_EQNAME_PATTERN = r"^(?:Q\{[\s\S]*\})?[^\s:{}#@/]+$"
_QNAME_PATTERN = r"^(?:[^\s:{}#@/$]+:)?[^\s:{}#@/$]+$"
_B64_W = r"[ \t\r\n]*"
_B64_C = rf"{_B64_W}[A-Za-z0-9+/]{_B64_W}"
_B64_PAD = rf"{_B64_W}={_B64_W}"
_BASE64_PATTERN = (
    rf"^{_B64_W}(?:(?:{_B64_C}){{4}})*"
    rf"(?:(?:{_B64_C}){{2}}{_B64_PAD}{_B64_PAD}|(?:{_B64_C}){{3}}{_B64_PAD})?{_B64_W}$"
)
_HEX_PATTERN = r"^(?:[0-9A-Fa-f]{2})*$"
_TAG_KEY = "#value"


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
        grammar_fingerprint: str | None = None,
    ) -> None:
        if not isinstance(schema, RelaxNGSchema):
            raise TypeError("ScalarCodec requires a RelaxNGSchema")
        if not isinstance(plan, ProjectionPlan):
            raise TypeError("ScalarCodec requires a ProjectionPlan")
        if grammar_fingerprint is not None and not isinstance(grammar_fingerprint, str):
            raise TypeError("the grammar fingerprint must be a string or None")
        expected = schema._grammar_fingerprint
        if grammar_fingerprint is not None and grammar_fingerprint != expected:
            raise IntegrationError(
                "ScalarCodec grammar fingerprint does not match the compiled RELAX NG "
                "schema; tags and candidate IDs must use the public schema identity"
            )
        self.schema = schema
        self.plan = plan
        self.grammar_fingerprint = expected
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
    datatype = cast(Datatype, cast(Any, pattern).datatype)
    try:
        return datatype.decode(lexical, context)
    except UnsupportedDatatypeOperation:
        return _UNDECODABLE


def _list_items_encodable(pattern: List) -> bool:
    """Whether every declared token has an explicit encoder."""

    plan = _list_token_plan(pattern)
    if plan is None:
        return False
    if plan.kind == "fixed" and not plan.patterns:
        return True
    assert plan.patterns
    return all(
        datatype_supports_encode(getattr(token_pattern, "datatype", None))
        for token_pattern in plan.patterns
    )


def _decode_list(pattern: List, lexical: str, context: DatatypeContext) -> object:
    """Return the declared tuple/list shape of one whole-list lexical value.

    A statically plain fixed or homogeneous list is decoded from its compiled
    token patterns.  Any optional, heterogeneous-repeating, ambiguous, or
    otherwise non-plain arrangement has no single declared shape and remains
    undecodable; it is never collapsed to its lexical string.
    """
    shape = list_shape(pattern)
    if shape == "interpretation" or not list_items_decodable(pattern):
        return _UNDECODABLE
    plan = _list_token_plan(pattern)
    assert plan is not None
    tokens = xml_whitespace_tokens(lexical)
    if plan.kind == "fixed":
        assert len(tokens) == len(plan.patterns)
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


def _lexical_equal(
    contract: ScalarContract,
    pattern: Pattern,
    left: str,
    right: str,
    context: DatatypeContext,
) -> bool:
    """Compare two lexical forms in the contract's value space."""
    if contract.kind == "atomic":
        datatype = cast(Datatype, cast(Any, pattern).datatype)
        return datatype.equal(left, context, right, context)
    shape = list_shape(cast(List, pattern))
    assert shape != "interpretation"
    plan = _list_token_plan(cast(List, pattern))
    assert plan is not None
    left_tokens = xml_whitespace_tokens(left)
    right_tokens = xml_whitespace_tokens(right)
    assert len(left_tokens) == len(right_tokens)
    if plan.kind == "fixed":
        assert len(left_tokens) == len(plan.patterns)
        patterns = plan.patterns
    else:
        assert plan.patterns
        patterns = tuple(plan.patterns[0] for _ in left_tokens)
    return all(
        _lexical_equal(
            scalar_contract(token_pattern),
            token_pattern,
            left_token.text,
            right_token.text,
            context,
        )
        for token_pattern, left_token, right_token in zip(
            patterns, left_tokens, right_tokens, strict=True
        )
    )


def _round_trips(
    codec: ScalarCodec,
    contract: ScalarContract,
    pattern: Pattern,
    decoded: object,
    lexical: str,
    context: DatatypeContext,
) -> bool:
    """Whether a decoded value re-encodes to a value-equal lexical form."""
    if contract.kind == "list":
        if not _list_items_encodable(cast(List, pattern)):
            return False
    elif not datatype_supports_encode(getattr(pattern, "datatype", None)):
        return False
    try:
        encoded = encode_scalar(contract, decoded, context, pattern=pattern)
    except (TypeError, ValueError, UnsupportedDatatypeOperation):
        return False
    if not codec.accepts(pattern, encoded, context):
        return False
    return _lexical_equal(contract, pattern, encoded, lexical, context)


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
        if candidate.decodable and candidate.decoded is not None:
            pattern = codec.pattern_for(contract)
            if _round_trips(codec, contract, pattern, candidate.decoded, lexical, context):
                return candidate.decoded
    return _wrapper(codec, candidates, lexical, context)


def _encode_assignment(
    contract: ScalarContract,
    pattern: Pattern,
    value: object,
    context: DatatypeContext,
) -> str:
    """Encode one typed assignment without a ``str(value)`` fallback."""
    if contract.kind == "list":
        if not _list_items_encodable(cast(List, pattern)):
            raise ValueError(
                "this RELAX NG list contract has a decoder-only item and no "
                "declared encoder; supply a lexical string, an exact #value tag, "
                "or an AlternativeValue snapshot"
            )
    elif not datatype_supports_encode(getattr(pattern, "datatype", None)):
        raise ValueError(
            "this RELAX NG scalar contract is decoder-only; it has no declared "
            "encoder. Supply a lexical string, an exact #value tag, or an "
            "AlternativeValue snapshot instead"
        )
    try:
        return encode_scalar(contract, value, context, pattern=pattern)
    except UnsupportedDatatypeOperation as error:
        raise ValueError(
            "this RELAX NG scalar contract is decoder-only; it has no encoder for "
            "the supplied Python value. Supply a lexical string, an exact #value "
            "tag, or an AlternativeValue snapshot instead"
        ) from error
    except (TypeError, ValueError) as error:
        raise ValueError(str(error)) from error


def _is_xsd_list_valued(contract: ScalarContract) -> bool:
    return (
        contract.kind == "atomic"
        and contract.library_uri == XSD_LIBRARY
        and contract.type_name in _XSD_LIST_VALUED_TYPES
    )


def _is_binary(contract: ScalarContract) -> bool:
    return (
        contract.kind == "atomic"
        and contract.library_uri == XSD_LIBRARY
        and contract.type_name in {"base64Binary", "hexBinary"}
    )


def _decoded_string_lexical(
    codec: ScalarCodec,
    contract: ScalarContract,
    pattern: Pattern,
    value: str,
    context: DatatypeContext,
) -> str | None:
    """Return the encoded lexical if *value* is this contract's decoded string."""
    datatype = getattr(pattern, "datatype", None)
    if not datatype_supports_encode(datatype):
        return None
    try:
        encoded = encode_scalar(contract, value, context, pattern=pattern)
    except (TypeError, ValueError, UnsupportedDatatypeOperation):
        return None
    if not codec.accepts(pattern, encoded, context):
        return None
    if not datatype_supports_decode(datatype):
        return None
    try:
        decoded = cast(Datatype, datatype).decode(encoded, context)
    except UnsupportedDatatypeOperation:
        return None
    return encoded if decoded == value else None


def _string_lexical(
    codec: ScalarCodec,
    contract: ScalarContract,
    pattern: Pattern,
    value: str,
    context: DatatypeContext,
) -> str:
    """Resolve one string input as a decoded form or a lexical form.

    A decoder+encoder contract is first asked whether the string is its decoded
    value; this prevents a ``result:value`` decoded string from being decoded a
    second time as lexical ``result:value``.  Otherwise the string is treated as
    lexical evidence and validated against the same unique contract.
    """
    encoded = _decoded_string_lexical(codec, contract, pattern, value, context)
    if encoded is not None:
        return encoded
    lexical = _lexical_string(contract, value, context)
    if codec.accepts(pattern, lexical, context):
        return lexical
    raise ValueError(
        f"scalar lexical {lexical!r} does not satisfy the {contract.kind} contract "
        f"in context {_context_text(context)}; a bare string is always lexical, so "
        "a decoded value requires an exact #value tag or an AlternativeValue snapshot"
    )


def _xsd_list_json_lexical(value: list[Any], context: DatatypeContext) -> str:
    """Join a JSON array for one XSD list-valued atomic contract."""
    del context
    tokens: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("an XSD list value requires string tokens in JSON input")
        tokens.append(item)
    return " ".join(tokens)


def _json_token_lexical(
    codec: ScalarCodec,
    contract: ScalarContract,
    pattern: Pattern,
    value: object,
    context: DatatypeContext,
) -> str:
    """Convert one JSON list item to its declared token lexical form."""
    if isinstance(value, str):
        return _string_lexical(codec, contract, pattern, value, context)
    if _is_xsd_list_valued(contract) and isinstance(value, list):
        return _xsd_list_json_lexical(value, context)
    return _encode_assignment(contract, pattern, value, context)


def _json_list_lexical(
    codec: ScalarCodec,
    contract: ScalarContract,
    pattern: List,
    value: list[Any],
    context: DatatypeContext,
) -> str:
    """Convert one JSON array to the declared RELAX NG list lexical value."""
    shape = list_shape(pattern)
    assert shape != "interpretation"
    plan = _list_token_plan(pattern)
    assert plan is not None
    if shape == "tuple":
        if len(value) != len(plan.patterns):
            raise ValueError(
                f"this fixed RELAX NG list requires {len(plan.patterns)} tokens, not {len(value)}"
            )
        patterns = plan.patterns
    else:
        assert plan.patterns
        patterns = tuple(plan.patterns[0] for _ in value)
    tokens: list[str] = []
    for token_pattern, item in zip(patterns, value, strict=True):
        token = _json_token_lexical(
            codec,
            scalar_contract(token_pattern),
            token_pattern,
            item,
            context,
        )
        if any(character in " \t\r\n" for character in token):
            raise ValueError(
                "a RELAX NG list token cannot contain XML whitespace; an XSD "
                "list-valued or other multi-token value does not fit one token"
            )
        tokens.append(token)
    return " ".join(tokens)


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
    if (
        contract.kind != "atomic"
        or contract.library_uri != XSD_LIBRARY
        or contract.type_name not in _QNAME_TYPE_NAMES
    ):
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
        lexical = _string_lexical(codec, contract, pattern, value, context)
    elif mode == "json" and isinstance(value, list):
        if contract.kind == "list":
            if not list_items_decodable(cast(List, pattern)) or not _list_items_encodable(
                cast(List, pattern)
            ):
                raise ValueError(
                    "this RELAX NG list contract has a decoder-only item and no "
                    "complete declared encoder; supply a lexical string, an exact "
                    "#value tag, or an AlternativeValue snapshot instead of a JSON array"
                )
            lexical = _json_list_lexical(
                codec,
                contract,
                cast(List, pattern),
                value,
                context,
            )
        elif _is_xsd_list_valued(contract):
            lexical = _xsd_list_json_lexical(value, context)
        else:
            lexical = _encode_assignment(contract, pattern, value, context)
    else:
        lexical = _encode_assignment(contract, pattern, value, context)
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


def _raw_value(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    value: object,
    context: DatatypeContext,
    mode: str,
) -> Any:
    """Validate a bare value; only a unique static contract accepts typed input."""
    if len(contracts) > 1:
        if not isinstance(value, str):
            raise ValueError(
                "an ambiguous RELAX NG scalar position requires a lexical string, "
                "an exact #value tag, or an AlternativeValue snapshot; "
                f"got {type(value).__name__}"
            )
        candidates = _surviving_candidates(codec, contracts, value, context)
        return _wrapper(codec, candidates, value, context)
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


def _interpretation_value(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    interpretation: ListInterpretation,
    supplied: DatatypeContext | None,
) -> Any:
    """Recheck a bare whole-list interpretation as one scoped assertion.

    Its token-level constraint graph belongs to one document parse and is not
    retained in a standalone model value.  The whole-list grammar identity,
    contract, candidate ID, lexical content, and scoped context are checked by
    recomputation; the result uses the same static value form as any other
    recomputed candidate set.
    """
    if not any(contract.kind == "list" for contract in contracts):
        raise ValueError("a ListInterpretation requires a list scalar position")
    if interpretation.grammar_fingerprint != codec.grammar_fingerprint:
        raise ValueError(
            "list interpretation grammar fingerprint does not match this compiled RELAX NG grammar"
        )
    context = _scoped_context(interpretation.context, supplied, kind="list interpretation")
    if candidate_id(codec.grammar_fingerprint, interpretation.contract) != (
        interpretation.candidate_id
    ):
        raise ValueError("list interpretation candidate ID does not match its whole-list contract")
    if interpretation.contract not in contracts:
        raise ValueError(
            "list interpretation contract is not statically possible at this scalar position"
        )
    candidates = _surviving_candidates(codec, contracts, interpretation.lexical, context)
    expected = {candidate.candidate_id: candidate.contract for candidate in candidates}
    if expected.get(interpretation.candidate_id) != interpretation.contract:
        raise ValueError(
            "list interpretation candidate does not match the complete recomputed "
            "candidates at this scalar position"
        )
    return _value_from_candidates(
        codec,
        contracts,
        candidates,
        interpretation.lexical,
        context,
    )


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
    if isinstance(value, ListInterpretation):
        return _interpretation_value(codec, contracts, value, supplied)
    if isinstance(value, Mapping) and "#value" in value:
        return _tag_value(codec, contracts, value, supplied)
    return _raw_value(codec, contracts, value, context, mode)


def _tag_schema() -> dict[str, Any]:
    """The structural version-1 tag shape plus its native-check disclosure."""
    context: dict[str, Any] = {
        "type": "object",
        "properties": {
            "base_uri": {"type": ["string", "null"]},
            "namespaces": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "not": {"required": ["xml"]},
            },
        },
        "required": ["base_uri", "namespaces"],
        "additionalProperties": False,
    }
    body: dict[str, Any] = {
        "type": "object",
        "properties": {
            "version": {"type": "integer", "const": 1},
            "grammar": {"type": "string", "minLength": 1},
            "lexical": {"type": "string"},
            "context": context,
            "candidates": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        },
        "required": ["version", "grammar", "lexical", "context", "candidates"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {_TAG_KEY: body},
        "required": [_TAG_KEY],
        "additionalProperties": False,
        "x-pyxsd-validation": {
            "grammar-fingerprint": (
                "must equal the compiled schema fingerprint; checked by native "
                "RELAX NG Pydantic validation, not by JSON Schema"
            ),
            "candidate-completeness": (
                "candidate IDs must equal the complete recomputed set; checked by "
                "native RELAX NG Pydantic validation, not by JSON Schema"
            ),
        },
    }


_OPAQUE_ARRAY_DEPTH = 32


def _opaque_scalar_schema() -> dict[str, Any]:
    """A bounded recursive scalar shape for a custom decoded value."""
    branches: list[dict[str, Any]] = [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
    ]
    scalar: dict[str, Any] = {"anyOf": list(branches)}
    for _ in range(_OPAQUE_ARRAY_DEPTH):
        scalar = {
            "anyOf": [
                *branches,
                {"type": "array", "items": scalar},
            ]
        }
    scalar["x-pyxsd-validation"] = {
        "custom-scalar": (
            "bounded recursive scalar shape; the custom datatype remains "
            "authoritative for lexical and value constraints"
        )
    }
    return scalar


def _plain_atomic_schema(contract: ScalarContract) -> dict[str, Any]:
    """The JSON value a decodable atomic contract exports."""
    if contract.library_uri == BUILTIN_LIBRARY:
        return {"type": "string", "x-pyxsd-type": contract.type_name}
    if contract.library_uri != XSD_LIBRARY:
        return _opaque_scalar_schema()
    name = contract.type_name
    assert name is not None
    if name == "boolean":
        return {"type": "boolean"}
    if name in _XSD_INTEGER_TYPES:
        return {"type": "integer"}
    if name == "decimal":
        return {"type": "string", "pattern": _DECIMAL_PATTERN, "x-pyxsd-type": "decimal"}
    if name in _XSD_FLOAT_TYPES:
        return {
            "anyOf": [
                {"type": "number"},
                {"enum": ["INF", "-INF", "NaN"]},
            ]
        }
    if name in _QNAME_TYPE_NAMES:
        return {
            "type": "string",
            "pattern": _EQNAME_PATTERN,
            "x-pyxsd-qname": True,
            "x-pyxsd-validation": {
                "qname-context": (
                    "prefix resolution and lexical QName acceptance require the "
                    "scoped validation context; JSON Schema cannot prove a prefix binding"
                )
            },
        }
    if name in _XSD_LIST_VALUED_TYPES:
        return {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "x-pyxsd-type": name,
            "x-pyxsd-validation": {
                "xsd-list-lexical": "tokens are validated by the native XSD list datatype"
            },
        }
    if name == "base64Binary":
        return {
            "type": "string",
            "pattern": _BASE64_PATTERN,
            "x-pyxsd-type": name,
            "x-pyxsd-encoding": "base64",
            "x-pyxsd-validation": {
                "base64": "XML whitespace is collapsed before base64 validation"
            },
        }
    if name == "hexBinary":
        return {
            "type": "string",
            "pattern": _HEX_PATTERN,
            "x-pyxsd-type": name,
            "x-pyxsd-encoding": "hex",
        }
    return {
        "type": "string",
        "x-pyxsd-type": name,
        "x-pyxsd-validation": {
            "lexical": (
                "validated by the native XSD datatype; JSON Schema does not encode "
                "the full temporal or lexical space"
            )
        },
    }


def _list_minimum(pattern: List) -> int:
    """Return the static minimum token count of one plain list pattern."""
    node = pattern.pattern
    if isinstance(node, Choice):
        first = node.first
        second = node.second
        assert isinstance(first, Empty) or isinstance(second, Empty)
        return 0
    assert isinstance(node, OneOrMore)
    return 1


def _lexical_branch(contract: ScalarContract) -> dict[str, Any] | None:
    """The additional lexical-string input branch a contract accepts."""
    if contract.kind == "text":
        return None
    if contract.kind == "list":
        return {
            "type": "string",
            "x-pyxsd-validation": {
                "list-lexical": "RELAX NG list token grammar is validated natively"
            },
        }
    if contract.library_uri == XSD_LIBRARY:
        name = contract.type_name
        if name in _XSD_INTEGER_TYPES:
            return {"type": "string", "pattern": _INTEGER_PATTERN}
        if name == "boolean":
            return {"type": "string", "pattern": _BOOLEAN_PATTERN}
        if name in _XSD_FLOAT_TYPES:
            return {"type": "string", "pattern": _FLOAT_PATTERN}
        if name in _QNAME_TYPE_NAMES:
            return {
                "type": "string",
                "pattern": _QNAME_PATTERN,
                "x-pyxsd-qname": True,
                "x-pyxsd-validation": {
                    "qname-context": (
                        "lexical QName prefixes are resolved with the scoped "
                        "validation context; JSON Schema cannot prove the binding"
                    )
                },
            }
        if name in _XSD_LIST_VALUED_TYPES:
            return {
                "type": "string",
                "x-pyxsd-validation": {
                    "xsd-list-lexical": (
                        "a lexical string is accepted for an XSD list-valued atomic; "
                        "the native datatype validates its tokens"
                    )
                },
            }
    base = _plain_atomic_schema(contract)
    if base.get("type") == "string":
        return None
    return {
        "type": "string",
        "x-pyxsd-validation": {
            "lexical": (
                "native datatype validation accepts the lexical forms in this "
                "string branch; JSON Schema does not encode the full lexical space"
            )
        },
    }


def _list_item_schema(contract: ScalarContract) -> dict[str, Any]:
    """One list item shape; JSON lexical strings remain accepted items."""
    lexical = _lexical_branch(contract)
    if lexical is None:
        return _plain_atomic_schema(contract)
    return _merge_schemas(_plain_atomic_schema(contract), lexical)


def _list_schema(codec: ScalarCodec, contract: ScalarContract) -> dict[str, Any]:
    """A plain fixed or homogeneous array, or the structural tag shape."""
    pattern = codec.plan.encoding_patterns[contract.pattern_identity]
    assert isinstance(pattern, List)
    shape = list_shape(pattern)
    if (
        shape == "interpretation"
        or not list_items_decodable(pattern)
        or not _list_items_encodable(pattern)
    ):
        return _tag_schema()
    plan = _list_token_plan(pattern)
    assert plan is not None
    if plan.kind == "fixed":
        body: dict[str, Any] = {
            "type": "array",
            "items": False,
            "minItems": len(plan.patterns),
            "maxItems": len(plan.patterns),
            "prefixItems": [_list_item_schema(scalar_contract(token)) for token in plan.patterns],
        }
    else:
        assert plan.patterns
        body = {
            "type": "array",
            "items": _list_item_schema(scalar_contract(plan.patterns[0])),
        }
        minimum = _list_minimum(pattern)
        if minimum > 0:
            body["minItems"] = minimum
    body["x-pyxsd-list-shape"] = shape
    return body


def _atomic_schema(codec: ScalarCodec, contract: ScalarContract) -> dict[str, Any]:
    """One atomic scalar's JSON shape, or the structural tag shape."""
    pattern = codec.pattern_for(contract)
    datatype = getattr(pattern, "datatype", None)
    if not datatype_supports_decode(datatype) or not datatype_supports_encode(datatype):
        return _tag_schema()
    return _plain_atomic_schema(contract)


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
    tag = _tag_schema()
    if len(contracts) > 1:
        schema = _merge_schemas({"type": "string"}, tag)
    else:
        contract = contracts[0]
        if contract.kind == "text":
            base = {"type": "string"}
        elif contract.kind == "list":
            base = _list_schema(codec, contract)
        else:
            base = _atomic_schema(codec, contract)
        lexical = _lexical_branch(contract)
        branches = [base] if lexical is None else [base, lexical]
        branches.append(tag)
        schema = _merge_schemas(*branches)
    if may_be_empty:
        schema = _merge_schemas(schema, {"type": "null"})
    return schema


def _generic_json_value(value: Any) -> Any:
    """Convert one custom decoded value without pretending a datatype choice."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (tuple, list)):
        return [_generic_json_value(item) for item in value]
    raise TypeError(f"cannot convert {type(value).__name__} to a JSON scalar")


def _atomic_json_value(contract: ScalarContract, value: Any) -> Any:
    """Serialize one atomic decoded value using its declared contract."""
    if _is_binary(contract):
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            if contract.type_name == "hexBinary":
                return raw.hex().upper()
            return base64.b64encode(raw).decode("ascii")
        return _generic_json_value(value)
    if _is_xsd_list_valued(contract):
        tokens = getattr(value, "tokens", None)
        if tokens is not None:
            return [str(token) for token in tokens]
        if isinstance(value, (tuple, list)):
            return [str(item) for item in value]
        if isinstance(value, str):
            return [span.text for span in xml_whitespace_tokens(value)]
        return _generic_json_value(value)
    if contract.library_uri != XSD_LIBRARY:
        return _generic_json_value(value)
    name = contract.type_name
    if name == "boolean":
        return bool(value)
    if name in _XSD_INTEGER_TYPES:
        return int(value)
    if name == "decimal":
        return format(value, "f") if isinstance(value, Decimal) else str(value)
    if name in _XSD_FLOAT_TYPES:
        number = float(value)
        if number != number:
            return "NaN"
        if number == float("inf"):
            return "INF"
        if number == float("-inf"):
            return "-INF"
        return number
    if name in _QNAME_TYPE_NAMES:
        expanded = getattr(value, "to_expanded_name", None)
        if callable(expanded):
            return expanded_to_eqname(expanded(strict=True))
        return _generic_json_value(value)
    lexical = getattr(value, "lexical", None)
    if callable(lexical):
        return str(lexical())
    if isinstance(value, str):
        return str(value)
    return _generic_json_value(value)


def _list_json_value(
    codec: ScalarCodec,
    contract: ScalarContract,
    value: Any,
) -> Any:
    """Serialize one whole-list value using its declared token contracts."""
    assert isinstance(value, (tuple, list))
    pattern = codec.plan.encoding_patterns[contract.pattern_identity]
    assert isinstance(pattern, List)
    shape = list_shape(pattern)
    plan = _list_token_plan(pattern)
    assert shape != "interpretation" and plan is not None
    if plan.kind == "fixed":
        assert len(value) == len(plan.patterns)
        patterns = plan.patterns
    else:
        assert plan.patterns
        patterns = tuple(plan.patterns[0] for _ in value)
    return [
        _atomic_json_value(scalar_contract(token_pattern), item)
        for token_pattern, item in zip(patterns, value, strict=True)
    ]


def _json_value_for_contract(
    codec: ScalarCodec,
    contracts: tuple[ScalarContract, ...],
    value: Any,
) -> Any:
    """Convert one accepted Python scalar to its contract-aware JSON value."""
    assert len(contracts) == 1
    contract = contracts[0]
    if contract.kind == "text":
        return value
    if contract.kind == "list":
        return _list_json_value(codec, contract, value)
    return _atomic_json_value(contract, value)


def _serializer(
    codec: ScalarCodec | None,
    contracts: tuple[ScalarContract, ...],
) -> Any:
    """Build one wrap serializer preserving Python wrappers and tagging JSON."""

    def serialize(value: Any, handler: Callable[[Any], Any], info: SerializationInfo) -> Any:
        del handler
        if value is None:
            return None
        if isinstance(value, (AlternativeValue, ListInterpretation)):
            if info.mode == "json":
                return scalar_tag(value)
            return value
        if info.mode != "json":
            return value
        if codec is None:
            return value
        return _json_value_for_contract(codec, contracts, value)

    return serialize


def _include_field_spec(include: Any, key: str, name: str) -> Any:
    """Resolve one field's include spec; ``False`` means absent."""
    if include is None:
        return None
    if include is True:
        return True
    if isinstance(include, Mapping):
        if key in include:
            return include[key]
        if name in include:
            return include[name]
        return None
    if isinstance(include, (set, frozenset, list, tuple)):
        return bool(key in include or name in include)
    return None


def _exclude_field_spec(exclude: Any, key: str, name: str) -> Any:
    """Resolve one field's exclude spec; ``None`` means no exclusion."""
    if exclude is None:
        return None
    if isinstance(exclude, Mapping):
        if key in exclude:
            return exclude[key]
        if name in exclude:
            return exclude[name]
        return None
    if isinstance(exclude, (set, frozenset, list, tuple)):
        return True if (key in exclude or name in exclude) else None
    return None


def _included_indices(spec: Any, length: int) -> set[int]:
    if spec is None or spec is True:
        return set(range(length))
    if isinstance(spec, int):
        return {spec} if 0 <= spec < length else set()
    if isinstance(spec, Mapping):
        return {
            index
            for index, value in spec.items()
            if isinstance(index, int) and 0 <= index < length and value is not False
        }
    if isinstance(spec, (set, frozenset, list, tuple)):
        return {index for index in spec if isinstance(index, int) and 0 <= index < length}
    return set(range(length))


def _excluded_indices(spec: Any, length: int) -> set[int]:
    if spec is None:
        return set()
    if isinstance(spec, int):
        return {spec} if 0 <= spec < length else set()
    if isinstance(spec, Mapping):
        return {
            index
            for index, value in spec.items()
            if isinstance(index, int) and 0 <= index < length and value is True
        }
    if isinstance(spec, (set, frozenset, list, tuple)):
        return {index for index in spec if isinstance(index, int) and 0 <= index < length}
    return set()


def _list_item_spec(spec: Any, index: int) -> Any:
    if isinstance(spec, Mapping):
        return spec.get(index)
    return None


def _restore_scalar_list(
    values: list[Any] | tuple[Any, ...],
    data: list[Any],
    include: Any,
    exclude: Any,
) -> list[Any]:
    """Restore wrappers in a filtered list by original index."""
    length = len(values)
    selected = _included_indices(include, length)
    selected -= _excluded_indices(exclude, length)
    restored: list[Any] = []
    for index, dumped in zip(sorted(selected), data, strict=False):
        item_include = _list_item_spec(include, index)
        item_exclude = _list_item_spec(exclude, index)
        restored.append(
            _restore_scalar_wrappers(
                values[index],
                dumped,
                include=item_include,
                exclude=item_exclude,
            )
        )
    return restored


def _restore_scalar_wrappers(
    value: Any,
    data: Any,
    *,
    include: Any = None,
    exclude: Any = None,
) -> Any:
    """Restore wrappers recursively in a Python-mode dump tree."""
    if isinstance(value, (AlternativeValue, ListInterpretation)):
        return value
    if isinstance(value, BaseModel):
        if getattr(type(value), "__pydantic_root_model__", False):
            return _restore_scalar_wrappers(
                getattr(value, "root", None), data, include=include, exclude=exclude
            )
        assert isinstance(data, dict)
        for name, field in getattr(type(value), "model_fields", {}).items():
            key = field.alias if field.alias in data else name
            if key not in data:
                continue
            child = getattr(value, name, None)
            child_include = _include_field_spec(include, key, name)
            child_exclude = _exclude_field_spec(exclude, key, name)
            if isinstance(child, (tuple, list)) and isinstance(data[key], list):
                data[key] = _restore_scalar_list(
                    child,
                    data[key],
                    child_include,
                    child_exclude,
                )
            else:
                data[key] = _restore_scalar_wrappers(
                    child,
                    data[key],
                    include=child_include,
                    exclude=child_exclude,
                )
        return data
    return data


def _normalize_filter_spec(spec: Any) -> Any:
    """Convert integer list-index selectors to the set/dict form Pydantic accepts."""
    if isinstance(spec, int) and not isinstance(spec, bool):
        return {spec}
    if isinstance(spec, Mapping):
        return {key: _normalize_filter_spec(value) for key, value in spec.items()}
    return spec


def model_dump_with_scalar_wrappers(
    self: Any,
    *,
    mode: Literal["json", "python"] | str = "python",
    include: IncEx | None = None,
    exclude: IncEx | None = None,
    context: Any | None = None,
    by_alias: bool | None = None,
    exclude_unset: bool = False,
    exclude_defaults: bool = False,
    exclude_none: bool = False,
    exclude_computed_fields: bool = False,
    round_trip: bool = False,
    warnings: bool | Literal["none", "warn", "error"] = True,
    fallback: Callable[[Any], Any] | None = None,
    serialize_as_any: bool = False,
    polymorphic_serialization: bool | object | None = _UNSET,
    **kwargs: Any,
) -> Any:
    """Public Python dumps restore exact scalar wrapper objects.

    Pydantic serializes dataclass values even when a wrap serializer returns the
    object unchanged, so generated models override :meth:`model_dump` after the
    normal dump has applied aliases, exclusions, and defaults.  JSON mode keeps
    the serializer's tags and contract-aware values.  List include/exclude
    filters use original indices when restoring wrappers, not filtered
    positions.

    ``polymorphic_serialization`` is forwarded only when the installed Pydantic
    baseline advertises it.  On older versions, explicitly supplying it raises
    the ordinary unexpected-keyword ``TypeError``; omitting it preserves the
    baseline behavior.  Any additional future keyword arguments are forwarded
    through ``**kwargs`` unchanged.
    """
    selected: dict[str, Any] = {
        "mode": mode,
        "include": _normalize_filter_spec(include),
        "exclude": _normalize_filter_spec(exclude),
        "context": context,
        "by_alias": by_alias,
        "exclude_unset": exclude_unset,
        "exclude_defaults": exclude_defaults,
        "exclude_none": exclude_none,
        "exclude_computed_fields": exclude_computed_fields,
        "round_trip": round_trip,
        "warnings": warnings,
        "fallback": fallback,
        "serialize_as_any": serialize_as_any,
    }
    if polymorphic_serialization is not _UNSET:
        if "polymorphic_serialization" not in _MODEL_DUMP_PARAMETERS:
            raise TypeError(
                "model_dump() got an unexpected keyword argument 'polymorphic_serialization'"
            )
        selected["polymorphic_serialization"] = polymorphic_serialization
    selected.update(kwargs)
    data = BaseModel.model_dump(self, **selected)
    if mode != "python":
        return data
    return _restore_scalar_wrappers(self, data, include=include, exclude=exclude)


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
        WrapSerializer(_serializer(None, contracts), return_type=Any, when_used="always"),
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
            WrapSerializer(_serializer(None, ()), return_type=Any, when_used="always"),
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
        WrapSerializer(_serializer(codec, unique), return_type=Any, when_used="always"),
        WithJsonSchema(_json_schema(codec, unique, may_be_empty=may_be_empty)),
    ]
