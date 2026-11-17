"""Schema-static RELAX NG scalar contracts and deterministic identities.

A scalar contract describes one schema-defined interpretation of a scalar
position: lexical ``text``, one compiled atomic datatype, or one whole RELAX NG
``list`` pattern.  It deliberately carries no instance value.  Identifiers are
derived from canonical semantic structure, never from object identity,
``repr`` addresses, mapping order, process hash seeds, or source paths.

Pattern identities encode the compiled semantic pattern graph as
length-prefixed fields.  A :class:`~pyxsd.relaxng.patterns.Ref` is followed to
its bound target and encoded as a deterministic back-reference when its target
is already being encoded, so legal recursive element definitions terminate.
Origins are provenance and never participate.  Equal acyclic patterns have the
same identity whether or not the pool shares one node, and a repeated
reference target is encoded once with a deterministic shared-reference
ordinal instead of expanding an exponentially large acyclic graph.

Datatype payloads must expose a stable ``library_uri``, ``type_name``, and
validated parameter/context metadata.  Built-in/XSD adapters and custom
datatypes that opt into the same stable attributes contribute their library,
type, ordered parameters, whitespace mode, and QName/NOTATION schema-side
namespace identity.  A datatype without that metadata — or with malformed
metadata — is rejected with :class:`UnsupportedDatatypeOperation`.  RELAX NG
exposes no stable public input naming an opaque datatype's library/type, so no
class, module, factory address, or object identity is substituted.  The
datatype-library registration fingerprint in :func:`grammar_fingerprint` still
identifies the library as a whole for datatypes that provide their own stable
metadata.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, cast

from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    DatatypeParameter,
    UnsupportedDatatypeOperation,
)
from pyxsd.relaxng.names import (
    AnyName,
    ExceptName,
    Name,
    NameChoice,
    NameClass,
    NsName,
)
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
)
from pyxsd.relaxng.simplify import CompiledGrammar

__all__ = [
    "LIST_LIBRARY",
    "LIST_TYPE_NAME",
    "ScalarContract",
    "ScalarRestriction",
    "candidate_id",
    "grammar_fingerprint",
    "pattern_identity",
    "scalar_contract",
]

#: Whole-list contracts are structural RELAX NG contracts, not datatype values.
#: The reserved built-in namespace and type name identify that contract without
#: claiming that a ``xsd:`` or custom datatype library defines a ``list`` type.
LIST_LIBRARY = BUILTIN_LIBRARY
LIST_TYPE_NAME = "list"

_PATTERN_PREFIX = "pyxsd.relaxng.pattern/1:"
_GRAMMAR_PREFIX = "pyxsd.relaxng.grammar/1:"
_CANDIDATE_PREFIX = "pyxsd.relaxng.candidate/1:"


def _write_bytes(out: bytearray, value: bytes) -> None:
    out += str(len(value)).encode("ascii") + b":" + value


def _write_text(out: bytearray, value: str) -> None:
    _write_bytes(out, value.encode("utf-8", "surrogatepass"))


def _write_int(out: bytearray, value: int) -> None:
    _write_text(out, str(value))


def _write_optional_text(out: bytearray, value: str | None) -> None:
    if value is None:
        _write_text(out, "none")
    else:
        _write_text(out, "some")
        _write_text(out, value)


def _write_name_class(out: bytearray, name_class: NameClass) -> None:
    if isinstance(name_class, AnyName):
        _write_text(out, "anyName")
    elif isinstance(name_class, Name):
        _write_text(out, "name")
        _write_optional_text(out, name_class.uri)
        _write_text(out, name_class.local)
    elif isinstance(name_class, NsName):
        _write_text(out, "nsName")
        _write_optional_text(out, name_class.uri)
    elif isinstance(name_class, NameChoice):
        _write_text(out, "nameChoice")
        _write_int(out, len(name_class.alternatives))
        for alternative in name_class.alternatives:
            _write_name_class(out, alternative)
    elif isinstance(name_class, ExceptName):
        _write_text(out, "exceptName")
        _write_name_class(out, name_class.base)
        _write_name_class(out, name_class.excluded)
    else:
        raise TypeError(f"unknown RELAX NG name class {type(name_class).__name__}")


def _normalize_parameters(
    parameters: tuple[DatatypeParameter, ...] | list[DatatypeParameter],
) -> tuple[DatatypeParameter, ...]:
    """Return an immutable, deterministically ordered parameter tuple."""
    result: list[DatatypeParameter] = []
    for parameter in parameters:
        if not isinstance(parameter, DatatypeParameter):
            raise TypeError("datatype parameters must be DatatypeParameter values")
        result.append(parameter)
    return tuple(sorted(result, key=lambda item: (item.name, item.value)))


def _normalize_restrictions(
    restrictions: tuple[ScalarRestriction, ...] | list[ScalarRestriction],
) -> tuple[ScalarRestriction, ...]:
    """Return a deduplicated, deterministically ordered restriction tuple."""
    result: set[ScalarRestriction] = set()
    for restriction in restrictions:
        if not isinstance(restriction, ScalarRestriction):
            raise TypeError("scalar restrictions must be ScalarRestriction values")
        result.add(restriction)
    return tuple(sorted(result, key=lambda item: (item.kind, item.pattern_identity)))


def _datatype_description(
    datatype: object,
) -> tuple[
    str,
    str,
    tuple[DatatypeParameter, ...],
    str | None,
    tuple[tuple[str, str], ...],
]:
    """Return validated, stable metadata for one compiled datatype.

    Opaque custom datatypes are rejected rather than identified by class or
    factory identity.  Stable metadata with malformed parameters, whitespace,
    or schema-side namespace context is likewise rejected explicitly: a
    caller-supplied ``None`` or unrelated object must not become an incidental
    ``TypeError`` or a silently different identity.
    """
    library_uri = getattr(datatype, "library_uri", None)
    type_name = getattr(datatype, "type_name", None)
    if not isinstance(library_uri, str) or not isinstance(type_name, str) or not type_name:
        raise UnsupportedDatatypeOperation(
            "the datatype does not expose stable RELAX NG library and type metadata"
        )
    try:
        parameters = _normalize_parameters(getattr(datatype, "parameters", ()))
    except (TypeError, ValueError) as error:
        raise UnsupportedDatatypeOperation(
            "the datatype exposes malformed parameters; a stable identity requires "
            "DatatypeParameter values"
        ) from error
    white_space = getattr(datatype, "white_space", None)
    if white_space is not None and not isinstance(white_space, str):
        raise UnsupportedDatatypeOperation("the datatype exposes a malformed whitespace mode")
    try:
        raw_context = tuple(getattr(datatype, "semantic_context", ()))
    except TypeError as error:
        raise UnsupportedDatatypeOperation(
            "the datatype exposes malformed schema-side namespace context"
        ) from error
    semantic_context: list[tuple[str, str]] = []
    for item in raw_context:
        if isinstance(item, str):
            raise UnsupportedDatatypeOperation(
                "the datatype exposes malformed schema-side namespace context"
            )
        try:
            prefix, uri = item
        except (TypeError, ValueError) as error:
            raise UnsupportedDatatypeOperation(
                "the datatype exposes malformed schema-side namespace context"
            ) from error
        if not isinstance(prefix, str) or not isinstance(uri, str):
            raise UnsupportedDatatypeOperation(
                "the datatype exposes malformed schema-side namespace context"
            )
        semantic_context.append((prefix, uri))
    return library_uri, type_name, parameters, white_space, tuple(semantic_context)


def _write_datatype(out: bytearray, datatype: object) -> None:
    """Encode a datatype's stable semantic description, never its address."""
    library_uri, type_name, parameters, white_space, semantic_context = _datatype_description(
        datatype
    )
    _write_text(out, "namedDatatype")
    _write_text(out, library_uri)
    _write_text(out, type_name)
    _write_int(out, len(parameters))
    for parameter in parameters:
        _write_text(out, parameter.name)
        _write_text(out, parameter.value)
    _write_optional_text(out, white_space)
    _write_int(out, len(semantic_context))
    for prefix, uri in semantic_context:
        _write_text(out, prefix)
        _write_text(out, uri)


def _pattern_operands(pattern: Pattern) -> tuple[Pattern, ...]:
    """Return every semantic child, including a bound reference target."""
    if isinstance(pattern, (Choice, Group, Interleave)):
        return (pattern.first, pattern.second)
    if isinstance(pattern, (Element, Attribute, List, OneOrMore)):
        return (pattern.pattern,)
    if isinstance(pattern, DataExcept):
        return (pattern.excluded,)
    if isinstance(pattern, Ref):
        return (pattern.target,)
    return ()


def _write_pattern_head(out: bytearray, pattern: Pattern) -> tuple[Pattern, ...]:
    """Write one non-reference node's tag and local payload; return its children."""
    if isinstance(pattern, Empty):
        _write_text(out, "Empty")
    elif isinstance(pattern, NotAllowed):
        _write_text(out, "NotAllowed")
    elif isinstance(pattern, Text):
        _write_text(out, "Text")
    elif isinstance(pattern, Choice):
        _write_text(out, "Choice")
        return (pattern.first, pattern.second)
    elif isinstance(pattern, Group):
        _write_text(out, "Group")
        return (pattern.first, pattern.second)
    elif isinstance(pattern, Interleave):
        _write_text(out, "Interleave")
        return (pattern.first, pattern.second)
    elif isinstance(pattern, OneOrMore):
        _write_text(out, "OneOrMore")
        return (pattern.pattern,)
    elif isinstance(pattern, Element):
        _write_text(out, "Element")
        _write_name_class(out, pattern.name_class)
        return (pattern.pattern,)
    elif isinstance(pattern, Attribute):
        _write_text(out, "Attribute")
        _write_name_class(out, pattern.name_class)
        return (pattern.pattern,)
    elif isinstance(pattern, List):
        _write_text(out, "List")
        return (pattern.pattern,)
    elif isinstance(pattern, Data):
        _write_text(out, "Data")
        _write_datatype(out, pattern.datatype)
    elif isinstance(pattern, DataExcept):
        _write_text(out, "DataExcept")
        _write_datatype(out, pattern.datatype)
        return (pattern.excluded,)
    elif isinstance(pattern, Value):
        _write_text(out, "Value")
        _write_datatype(out, pattern.datatype)
        _write_text(out, pattern.lexical)
    else:
        raise TypeError(f"unknown RELAX NG pattern {type(pattern).__name__}")
    return ()


class _IdentityContext:
    """Per-identity-work caches; never participates in encoded bytes."""

    __slots__ = ("acyclic", "digests", "work")

    def __init__(self) -> None:
        self.acyclic: dict[int, bool] = {}
        self.digests: dict[int, bytes] = {}
        self.work = 0


def _is_acyclic(root: Pattern, context: _IdentityContext) -> bool:
    """Whether *root*'s reachable graph contains no reference cycle."""
    cached = context.acyclic.get(id(root))
    if cached is not None:
        return cached
    colors: dict[int, int] = {}
    visited: list[Pattern] = []
    pending: list[tuple[Pattern, int]] = [(root, 0)]
    acyclic = True
    while pending:
        node, index = pending[-1]
        key = id(node)
        if index == 0:
            colors[key] = 1
            visited.append(node)
        children = _pattern_operands(node)
        if index < len(children):
            child = children[index]
            pending[-1] = (node, index + 1)
            state = colors.get(id(child), 0)
            if state == 1:
                acyclic = False
                pending.clear()
                break
            if state == 0:
                pending.append((child, 0))
        else:
            colors[key] = 2
            pending.pop()
    for node in visited:
        context.acyclic[id(node)] = acyclic
    return acyclic


def _acyclic_digest(root: Pattern, context: _IdentityContext) -> bytes:
    """Return a sharing-insensitive structural digest of an acyclic subgraph."""
    cached = context.digests.get(id(root))
    if cached is not None:
        return cached
    pending: list[tuple[Pattern, bool]] = [(root, False)]
    while pending:
        node, ready = pending.pop()
        key = id(node)
        if key in context.digests:
            continue
        if not ready:
            pending.append((node, True))
            pending.extend((child, False) for child in reversed(_pattern_operands(node)))
            continue
        context.work += 1
        if isinstance(node, Ref):
            out = bytearray(b"ref\x00")
            out += context.digests[id(node.target)]
            context.digests[key] = hashlib.sha256(out).digest()
            continue
        out = bytearray(b"node\x00")
        children = _write_pattern_head(out, node)
        for child in children:
            out += context.digests[id(child)]
        context.digests[key] = hashlib.sha256(out).digest()
    return context.digests[id(root)]


def _ref_key(target: Pattern, context: _IdentityContext) -> bytes | int:
    """Return an internal expansion key for one reference target.

    Acyclic targets use a content digest, so separately constructed equal
    targets share one encoded expansion.  A cyclic target uses its live node
    identity only as an internal cache key; that identity is never encoded or
    hashed into an ID.
    """
    if _is_acyclic(target, context):
        return _acyclic_digest(target, context)
    return id(target)


def _write_pattern(
    out: bytearray,
    root: Pattern,
    context: _IdentityContext,
    expanded: dict[bytes | int, int],
) -> None:
    """Encode a pattern graph iteratively with bounded shared-reference work.

    The first occurrence of each acyclic target and each live cyclic target is
    expanded in the existing deterministic byte form, including path-relative
    ``RefBack`` indices for cycles.  Later occurrences of the same semantic
    target emit ``RefShared`` and the ordinal of that first expansion, so a
    doubling definition graph expands each distinct target once instead of
    exponentially.  Ordinals depend only on traversal order, never on object
    addresses or process hashes.
    """
    active: dict[int, int] = {}
    reference_chain: set[int] = set()
    pending: list[tuple[str, Pattern | int]] = [("node", root)]
    while pending:
        action, item = pending.pop()
        if action == "close":
            active.pop(cast(int, item))
            continue
        if action == "unref":
            reference_chain.remove(cast(int, item))
            continue
        node = cast(Pattern, item)
        context.work += 1
        if isinstance(node, Ref):
            key = id(node)
            target = node.target
            target_key = id(target)
            if target_key in active:
                _write_text(out, "RefBack")
                _write_int(out, active[target_key])
                continue
            if key in reference_chain:
                raise ValueError("cyclic RELAX NG reference chain without a pattern node")
            ref_key = _ref_key(target, context)
            ordinal = expanded.get(ref_key)
            if ordinal is not None:
                _write_text(out, "RefShared")
                _write_int(out, ordinal)
                continue
            expanded[ref_key] = len(expanded)
            reference_chain.add(key)
            _write_text(out, "Ref")
            pending.append(("unref", key))
            pending.append(("node", target))
            continue
        key = id(node)
        if key in active:
            raise ValueError("cyclic RELAX NG pattern outside a bound reference")
        active[key] = len(active)
        pending.append(("close", key))
        children = _write_pattern_head(out, node)
        pending.extend(("node", child) for child in reversed(children))


@dataclass(frozen=True)
class ScalarRestriction:
    """One normalized ``value`` or ``except`` restriction identity."""

    kind: Literal["value", "except"]
    pattern_identity: str

    def __post_init__(self) -> None:
        if self.kind not in ("value", "except"):
            raise ValueError("a scalar restriction kind must be 'value' or 'except'")
        if not isinstance(self.pattern_identity, str) or not self.pattern_identity:
            raise ValueError("a scalar restriction pattern identity must be a nonempty string")


@dataclass(frozen=True)
class ScalarContract:
    """One frozen, schema-static scalar interpretation.

    ``kind`` is ``"text"``, ``"atomic"``, or ``"list"``.  Text contracts carry
    no datatype-library or type identity.  A list contract uses
    :data:`LIST_LIBRARY` and :data:`LIST_TYPE_NAME` as a structural marker; it
    never matches :meth:`AlternativeValue.as_type`, which selects atomic
    datatypes only.
    """

    kind: Literal["text", "atomic", "list"]
    library_uri: str | None
    type_name: str | None
    parameters: tuple[DatatypeParameter, ...]
    restrictions: tuple[ScalarRestriction, ...]
    pattern_identity: str

    def __post_init__(self) -> None:
        if self.kind not in ("text", "atomic", "list"):
            raise ValueError("a scalar contract kind must be 'text', 'atomic', or 'list'")
        if not isinstance(self.pattern_identity, str) or not self.pattern_identity:
            raise ValueError("a scalar contract pattern identity must be a nonempty string")
        parameters = _normalize_parameters(self.parameters)
        restrictions = _normalize_restrictions(self.restrictions)
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "restrictions", restrictions)
        if self.kind == "text":
            if self.library_uri is not None or self.type_name is not None:
                raise ValueError("a text scalar contract has no datatype-library identity")
            if parameters or restrictions:
                raise ValueError("a text scalar contract has no parameters or restrictions")
            return
        if not isinstance(self.library_uri, str):
            raise ValueError("a non-text scalar contract requires a library URI string")
        if not isinstance(self.type_name, str) or not self.type_name:
            raise ValueError("a non-text scalar contract requires a nonempty type name")


def _pattern_identity_and_work(pattern: Pattern) -> tuple[str, int]:
    """Return a pattern identity and the structural encoding work it required."""
    context = _IdentityContext()
    out = bytearray(b"pyxsd.relaxng.pattern/1\x00")
    _write_pattern(out, pattern, context, {})
    return _PATTERN_PREFIX + hashlib.sha256(out).hexdigest(), context.work


def pattern_identity(pattern: Pattern) -> str:
    """Return a deterministic identity for one compiled semantic pattern.

    The digest covers node kinds, name classes, datatype descriptions,
    schema-side QName namespace identity, value lexicals, and bound reference
    cycles.  It ignores origins, source locations, object identity, and
    instance values, so recompiling the same semantic grammar yields the same
    identity.
    """
    identity, _ = _pattern_identity_and_work(pattern)
    return identity


def grammar_fingerprint(grammar: CompiledGrammar, datatype_fingerprint: str) -> str:
    """Return the release-facing identity of one compiled grammar.

    Inputs, in canonical order, are the compiled start pattern, every named
    definition and its pattern, the normalized dependency content closure
    (read purpose and content digest, never a source path), and the effective
    datatype-registry fingerprint supplied by compilation.  That registry
    fingerprint covers the fixed built-in/XSD identities and every custom
    registration's stable fingerprint.  Origins, absolute paths, object
    addresses, and mapping insertion order do not participate.

    Stability is scoped to the compiled semantic grammar and datatype
    registry: recompiling the same schema with the same registrations yields
    the same value in any process.  Arbitrary grammars that merely accept the
    same language are not claimed to share an identity.
    """
    if not isinstance(grammar, CompiledGrammar):
        raise TypeError("grammar must be a CompiledGrammar")
    if not isinstance(datatype_fingerprint, str):
        raise TypeError("the datatype fingerprint must be a string")
    if not datatype_fingerprint:
        raise ValueError("the datatype fingerprint must not be empty")
    out = bytearray(b"pyxsd.relaxng.grammar/1\x00")
    _write_text(out, datatype_fingerprint)
    context = _IdentityContext()
    _write_text(out, "start")
    _write_pattern(out, grammar.start, context, {})
    _write_text(out, "definitions")
    names = sorted(grammar.definitions)
    _write_int(out, len(names))
    for name in names:
        _write_text(out, name)
        _write_pattern(out, grammar.definitions[name].pattern, context, {})
    dependencies = sorted(
        {(dependency.purpose, dependency.content_sha256) for dependency in grammar.dependencies}
    )
    _write_text(out, "dependencies")
    _write_int(out, len(dependencies))
    for purpose, content_sha256 in dependencies:
        _write_text(out, purpose)
        _write_text(out, content_sha256)
    return _GRAMMAR_PREFIX + hashlib.sha256(out).hexdigest()


def _write_contract(out: bytearray, contract: ScalarContract) -> None:
    _write_text(out, contract.kind)
    _write_optional_text(out, contract.library_uri)
    _write_optional_text(out, contract.type_name)
    _write_int(out, len(contract.parameters))
    for parameter in contract.parameters:
        _write_text(out, parameter.name)
        _write_text(out, parameter.value)
    _write_int(out, len(contract.restrictions))
    for restriction in contract.restrictions:
        _write_text(out, restriction.kind)
        _write_text(out, restriction.pattern_identity)
    _write_text(out, contract.pattern_identity)


def candidate_id(grammar_fingerprint: str, contract: ScalarContract) -> str:
    """Return the deterministic opaque ID of one scalar candidate contract.

    The digest covers the compiled grammar fingerprint and every semantic
    contract field: kind, datatype-library URI, type name, normalized
    parameters, normalized value/except restrictions, and scalar-pattern
    identity.  It never covers the lexical value, decoded Python value,
    occurrence, revision, object identity, process hash seed, or source path.
    """
    if not isinstance(grammar_fingerprint, str):
        raise TypeError("the grammar fingerprint must be a string")
    if not grammar_fingerprint:
        raise ValueError("the grammar fingerprint must not be empty")
    if not isinstance(contract, ScalarContract):
        raise TypeError("contract must be a ScalarContract")
    out = bytearray(b"pyxsd.relaxng.candidate/1\x00")
    _write_text(out, grammar_fingerprint)
    _write_contract(out, contract)
    return _CANDIDATE_PREFIX + hashlib.sha256(out).hexdigest()


def _datatype_metadata(datatype: object) -> tuple[str, str, tuple[DatatypeParameter, ...]]:
    """Read validated RELAX NG library/type metadata from a compiled datatype."""
    library_uri, type_name, parameters, _, _ = _datatype_description(datatype)
    return library_uri, type_name, parameters


def scalar_contract(pattern: Pattern) -> ScalarContract:
    """Build the schema-static contract of one scalar pattern.

    ``text`` stays lexical and acquires no ``xsd:string`` identity.  ``data``,
    ``dataExcept``, and ``value`` patterns derive atomic metadata from their
    compiled datatype.  A ``list`` pattern is one structural whole-list
    contract; list decoding and token assignments belong to list
    interpretations.
    """
    identity = pattern_identity(pattern)
    if isinstance(pattern, Text):
        return ScalarContract("text", None, None, (), (), identity)
    if isinstance(pattern, List):
        return ScalarContract("list", LIST_LIBRARY, LIST_TYPE_NAME, (), (), identity)
    if isinstance(pattern, (Data, DataExcept, Value)):
        library_uri, type_name, parameters = _datatype_metadata(pattern.datatype)
        restrictions: tuple[ScalarRestriction, ...] = ()
        if isinstance(pattern, DataExcept):
            restrictions = (ScalarRestriction("except", pattern_identity(pattern.excluded)),)
        elif isinstance(pattern, Value):
            restrictions = (ScalarRestriction("value", identity),)
        return ScalarContract(
            "atomic",
            library_uri,
            type_name,
            parameters,
            restrictions,
            identity,
        )
    raise TypeError(f"{type(pattern).__name__} is not a RELAX NG scalar pattern")
