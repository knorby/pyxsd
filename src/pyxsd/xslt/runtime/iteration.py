"""Explicit XSLT iteration state and normative sorting/grouping algorithms.

Iteration is heap state, not Python loop state.  An :class:`IterationState`
records the input sequence, the processing position, the current iteration
parameters, and any pending ``xsl:next-iteration`` or ``xsl:break`` control.
The processor owns one scheduler frame for the iteration and a fresh body frame
per item, so parameters and early exit survive suspension and never depend on a
Python ``for``/``while`` temporary.

Sorting and grouping use XDM value semantics directly.  Strings are compared
with the selected collation (the codepoint collation by default), numbers use
XPath numeric promotion with NaN ordered below every other number, and
grouping keys use ``fn:deep-equal`` semantics rather than Python container or
``locale`` ordering.
"""

from __future__ import annotations

import math
import struct
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import cmp_to_key
from itertools import pairwise
from typing import Any
from urllib.parse import urljoin, urlsplit

from pyxsd.xdm import XdmAtomic, XdmItem, XdmSequence
from pyxsd.xdm.nodes import XdmNode
from pyxsd.xslt.errors import XsltDynamicError

__all__ = [
    "CODEPOINT_COLLATION",
    "GROUP_ADJACENT",
    "GROUP_BY",
    "GROUP_ENDING_WITH",
    "GROUP_STARTING_WITH",
    "Collation",
    "Completion",
    "Group",
    "IterationItem",
    "IterationState",
    "MergeGroup",
    "MergeItem",
    "ResolvedSortKey",
    "SortKey",
    "atomize_key_sequence",
    "check_merge_order",
    "compare_merge_key_sets",
    "compare_sort_key_sets",
    "deep_equal_sequences",
    "group_adjacent",
    "group_by",
    "group_ending_with",
    "group_starting_with",
    "grouping_key_set",
    "merge_groups",
    "merge_key_equal",
    "resolve_collation",
    "sort_merge_input",
    "sort_records",
]

CODEPOINT_COLLATION = "http://www.w3.org/2005/xpath-functions/collation/codepoint"
HTML_ASCII_CASE_INSENSITIVE_COLLATION = (
    "http://www.w3.org/2005/xpath-functions/collation/html-ascii-case-insensitive"
)
XQUERY_CASEBLIND_COLLATION = "http://www.w3.org/2010/09/qt-fots-catalog/collation/caseblind"
UCA_COLLATION = "http://www.w3.org/2013/collation/UCA"

GROUP_BY = "group-by"
GROUP_ADJACENT = "group-adjacent"
GROUP_STARTING_WITH = "group-starting-with"
GROUP_ENDING_WITH = "group-ending-with"

_STRING_LOCALS = frozenset(
    {
        "anyURI",
        "ENTITIES",
        "ENTITY",
        "ID",
        "IDREF",
        "IDREFS",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "NMTOKENS",
        "normalizedString",
        "string",
        "token",
        "untypedAtomic",
    }
)
_NUMERIC_LOCALS = frozenset(
    {
        "byte",
        "decimal",
        "double",
        "float",
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
_QNAME_LOCALS = frozenset({"NOTATION", "QName"})
_BOOLEAN_LOCALS = frozenset({"boolean"})
_UNORDERED_LOCALS = frozenset({"boolean", "NOTATION", "QName", "base64Binary", "hexBinary"})


def _local_name(item: XdmAtomic) -> str:
    name = item.type_name
    return name.partition("}")[2] if name.startswith("{") else name.rpartition(":")[2]


def _family(item: XdmAtomic) -> str:
    local = _local_name(item)
    if local in _STRING_LOCALS:
        return "string"
    if local in _NUMERIC_LOCALS:
        return "numeric"
    if local in _QNAME_LOCALS:
        return "qname"
    if local in _BOOLEAN_LOCALS:
        return "boolean"
    return "other"


def _is_nan(value: Any) -> bool:
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, Decimal):
        return value.is_nan()
    return False


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf
    except (TypeError, ValueError):
        return math.nan


def _as_float32(value: Any) -> float:
    number = _as_float(value)
    try:
        return struct.unpack(">f", struct.pack(">f", number))[0]
    except OverflowError:
        return math.inf if number > 0 else -math.inf


def _bounded_number(value: Any) -> float:
    """Convert one numeric payload to binary64 for number-typed ordering."""
    return _as_float(value)


def _number_value(item: XdmAtomic) -> float:
    family = _family(item)
    if family == "boolean":
        return 1.0 if bool(item.value) else 0.0
    if family == "numeric":
        return _bounded_number(item.value)
    lexical = item.lexical.strip()
    if not lexical:
        return math.nan
    try:
        return float(lexical)
    except ValueError:
        return math.nan


# ---------------------------------------------------------------------------
# Collations


@dataclass(frozen=True, slots=True)
class Collation:
    """One recognized collation with deterministic string ordering.

    ``kind`` is ``codepoint``, ``html-ascii``, ``caseblind``, or ``uca``.  The
    UCA variant is a deterministic approximation that implements the normative
    parameters that affect ordering (strength, caseFirst, and numeric) without
    depending on the process locale.
    """

    uri: str
    kind: str = "codepoint"
    strength: str = "tertiary"
    case_first: str = "upper"
    numeric: bool = False
    language: str = ""

    def _ascii_casefold(self, value: str) -> str:
        return value.translate(
            str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        )

    @staticmethod
    def _primary(value: str) -> str:
        decomposed = unicodedata.normalize("NFD", value.casefold())
        return "".join(char for char in decomposed if not unicodedata.combining(char))

    @staticmethod
    def _secondary(value: str) -> str:
        return unicodedata.normalize("NFD", value.casefold())

    def _case_key(self, value: str) -> tuple[int, ...]:
        upper_first = self.case_first == "upper"
        ranks: list[int] = []
        for char in value:
            if char.isupper():
                rank = 0 if upper_first else 2
            elif char.islower():
                rank = 2 if upper_first else 0
            else:
                rank = 1
            ranks.append(rank)
        return tuple(ranks)

    @staticmethod
    def _numeric_tokens(value: str) -> tuple[tuple[int, object], ...]:
        tokens: list[tuple[int, object]] = []
        index = 0
        while index < len(value):
            if value[index].isdigit():
                end = index
                while end < len(value) and value[end].isdigit():
                    end += 1
                tokens.append((0, int(value[index:end])))
                index = end
                continue
            tokens.append((1, value[index]))
            index += 1
        return tuple(tokens)

    def _uca_key(self, value: str) -> tuple[Any, ...]:
        primary = self._primary(value)
        primary_key: Any = self._numeric_tokens(primary) if self.numeric else primary
        if self.strength == "primary":
            return (primary_key,)
        secondary = self._secondary(value)
        parts: list[Any] = [primary_key, secondary]
        if self.strength in ("tertiary", "quaternary", "identical"):
            parts.append(self._case_key(value))
        return tuple(parts)

    def compare(self, left: str, right: str) -> int:
        if self.kind == "codepoint":
            return 0 if left == right else (-1 if left < right else 1)
        if self.kind == "html-ascii":
            left_key = self._ascii_casefold(left)
            right_key = self._ascii_casefold(right)
            if left_key == right_key:
                return 0
            return -1 if left_key < right_key else 1
        if self.kind == "caseblind":
            left_key = left.casefold()
            right_key = right.casefold()
            if left_key == right_key:
                return 0
            return -1 if left_key < right_key else 1
        uca_left = self._uca_key(left)
        uca_right = self._uca_key(right)
        if uca_left == uca_right:
            return 0
        return -1 if uca_left < uca_right else 1

    def equal(self, left: str, right: str) -> bool:
        return self.compare(left, right) == 0


def _resolve_relative(uri: str, base_uri: str | None) -> str:
    if urlsplit(uri).scheme:
        return uri
    if base_uri is None:
        return uri
    return urljoin(base_uri, uri)


def resolve_collation(
    uri: str | None,
    *,
    code: str,
    default: str | None = None,
    base_uri: str | None = None,
    language: str = "",
    case_order: str | None = None,
) -> Collation:
    """Resolve and validate one collation URI.

    ``code`` is the XSLT dynamic error code used for an unrecognized collation
    (``XTDE1035`` for sorting, ``XTDE1110`` for grouping).  A missing URI uses
    *default*; when that is also missing the Unicode codepoint collation is
    selected.  A ``lang``/``case-order`` pair without an explicit collation
    selects a deterministic UCA-style ordering.
    """
    if uri is None:
        if default is None:
            if language or case_order is not None:
                return Collation(
                    uri=f"urn:pyxsd:language:{language}:{case_order or ''}",
                    kind="uca",
                    strength="tertiary",
                    case_first=(case_order or "upper").replace("-first", ""),
                    language=language,
                )
            return Collation(CODEPOINT_COLLATION)
        uri = default
    if not isinstance(uri, str):
        raise TypeError("a collation URI must be a string")
    uri = _resolve_relative(uri, base_uri)
    if uri == CODEPOINT_COLLATION:
        return Collation(uri)
    if uri == HTML_ASCII_CASE_INSENSITIVE_COLLATION:
        return Collation(uri, kind="html-ascii")
    if uri == XQUERY_CASEBLIND_COLLATION:
        return Collation(uri, kind="caseblind")
    if uri.startswith(UCA_COLLATION):
        parameters: dict[str, str] = {}
        query = urlsplit(uri).query
        for clause in query.replace("&", ";").split(";"):
            if not clause:
                continue
            name, separator, value = clause.partition("=")
            if separator:
                parameters[name] = value
        fallback = parameters.get("fallback", "yes").lower()
        if fallback == "no":
            raise XsltDynamicError(
                f"the Unicode collation {uri!r} is not available with fallback=no",
                code=code,
            )
        strength = parameters.get("strength", "tertiary").lower()
        strength = {
            "1": "primary",
            "2": "secondary",
            "3": "tertiary",
            "4": "quaternary",
            "5": "identical",
        }.get(strength, strength)
        if strength not in ("primary", "secondary", "tertiary", "quaternary", "identical"):
            raise XsltDynamicError(f"unsupported collation strength in {uri!r}", code=code)
        case_first = parameters.get("caseFirst", "upper").lower()
        if case_first not in ("upper", "lower"):
            raise XsltDynamicError(f"unsupported collation caseFirst in {uri!r}", code=code)
        numeric = parameters.get("numeric", "no").lower() in ("yes", "true", "1")
        return Collation(
            uri,
            kind="uca",
            strength=strength,
            case_first=case_first,
            numeric=numeric,
            language=parameters.get("lang", ""),
        )
    raise XsltDynamicError(
        f"the collation {uri!r} is not recognized",
        code=code,
    )


# ---------------------------------------------------------------------------
# Compilation-facing plans and runtime iteration state


@dataclass(frozen=True, slots=True)
class SortKey:
    """One compiled ``xsl:sort`` component."""

    instruction: Any
    select: Any
    children: tuple[Any, ...]
    lang: Any = None
    order: Any = None
    collation: Any = None
    stable: Any = None
    case_order: Any = None
    data_type: Any = None
    base_uri: str | None = None
    xslt_version: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedSortKey:
    """One sort component after AVT evaluation and collation resolution."""

    order: str = "ascending"
    collation: Collation = field(default_factory=lambda: Collation(CODEPOINT_COLLATION))
    data_type: str = "native"
    numeric_mode: str = "exact"

    def __post_init__(self) -> None:
        if self.order not in ("ascending", "descending"):
            raise ValueError(f"unknown sort order {self.order!r}")
        if self.data_type not in ("native", "text", "number"):
            raise ValueError(f"unknown sort data type {self.data_type!r}")
        if self.numeric_mode not in ("exact", "float", "double"):
            raise ValueError(f"unknown numeric comparison mode {self.numeric_mode!r}")
        if not isinstance(self.collation, Collation):
            raise TypeError("a resolved sort key requires a Collation")


@dataclass(frozen=True, slots=True)
class Completion:
    """The ``xsl:on-completion`` select expression or sequence constructor."""

    instruction: Any
    select: Any
    children: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class IterationItem:
    """One processing item and its group payload."""

    item: XdmItem
    position: int
    size: int
    group: XdmSequence | None = None
    grouping_key: XdmSequence | None = None


@dataclass(slots=True)
class IterationState:
    """Mutable, scheduler-owned iteration state.

    The object lives on the heap and is shared only by the iteration frame and
    its current body frame.  Parameters and control flags are therefore never
    hidden in a Python call temporary.
    """

    kind: str
    body: tuple[Any, ...]
    items: tuple[IterationItem, ...]
    base_variables: Mapping[str, XdmSequence]
    parameters: dict[str, XdmSequence] = field(default_factory=dict)
    parameter_plans: Mapping[str, Any] = field(default_factory=dict)
    on_completion: Completion | None = None
    index: int = 0
    control: str | None = None
    pending_parameters: dict[str, XdmSequence] | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("for-each", "iterate", "for-each-group"):
            raise ValueError(f"unknown iteration kind {self.kind!r}")
        if self.control not in (None, "next", "break"):
            raise ValueError(f"unknown iteration control {self.control!r}")
        self.base_variables = dict(self.base_variables)
        self.parameter_plans = dict(self.parameter_plans)


# ---------------------------------------------------------------------------
# Merging


@dataclass(frozen=True, slots=True)
class MergeItem:
    """One item selected by a merge source and its composite merge key.

    ``source`` is the merge source's stylesheet order, ``anchor`` the order of
    the input sequence selected by one ``for-each-item``/``for-each-source``
    anchor, and ``position`` the item's order within that input sequence.
    Together they define the normative current-merge-group ordering.
    """

    item: XdmItem
    source: int
    anchor: int
    position: int
    key: tuple[XdmSequence, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.item, XdmItem):
            raise TypeError("a merge item requires an XDM item")
        for label, value in (
            ("source", self.source),
            ("anchor", self.anchor),
            ("position", self.position),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"a merge item {label} must be a non-negative integer")
        if not isinstance(self.key, tuple):
            raise ValueError("a merge item key must be a tuple of key components")
        for component in self.key:
            if not isinstance(component, tuple) or not all(
                isinstance(value, XdmAtomic) for value in component
            ):
                raise ValueError(
                    "a merge item key component must be an XDM sequence of atomic values"
                )


@dataclass(frozen=True, slots=True)
class MergeGroup:
    """One group of items sharing a composite merge key."""

    items: tuple[MergeItem, ...]
    key: tuple[XdmSequence, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("a merge group is never empty")
        if not all(isinstance(item, MergeItem) for item in self.items):
            raise TypeError("a merge group requires merge items")
        if not isinstance(self.key, tuple):
            raise ValueError("a merge group key must be a tuple of key components")


# ---------------------------------------------------------------------------
# Atomic and sequence comparison


def _compare_numeric(left: XdmAtomic, right: XdmAtomic, mode: str = "exact") -> int:
    left_value = left.value
    right_value = right.value
    left_nan = _is_nan(left_value)
    right_nan = _is_nan(right_value)
    if left_nan or right_nan:
        if left_nan and right_nan:
            return 0
        return -1 if left_nan else 1
    if mode == "double":
        left_float = _as_float(left_value)
        right_float = _as_float(right_value)
    elif mode == "float":
        left_float = _as_float32(left_value)
        right_float = _as_float32(right_value)
    else:
        if left_value == right_value:
            return 0
        return -1 if left_value < right_value else 1
    if left_float == right_float:
        return 0
    return -1 if left_float < right_float else 1


def _compare_atomic(left: XdmAtomic, right: XdmAtomic, key: ResolvedSortKey) -> int:
    data_type = key.data_type
    if data_type == "text":
        left_text = left.lexical
        right_text = right.lexical
        return key.collation.compare(left_text, right_text)
    if data_type == "number":
        left_number = _number_value(left)
        right_number = _number_value(right)
        if math.isnan(left_number) or math.isnan(right_number):
            if math.isnan(left_number) and math.isnan(right_number):
                return 0
            return -1 if math.isnan(left_number) else 1
        if left_number == right_number:
            return 0
        return -1 if left_number < right_number else 1

    left_family = _family(left)
    right_family = _family(right)
    if left_family == "string" and right_family == "string":
        return key.collation.compare(left.lexical, right.lexical)
    if left_family == "numeric" and right_family == "numeric":
        return _compare_numeric(left, right, key.numeric_mode)
    if left_family != right_family:
        raise XsltDynamicError(
            f"sort keys {left.lexical!r} and {right.lexical!r} are not comparable",
            code="XTDE1030",
        )
    if _local_name(left) in _UNORDERED_LOCALS:
        raise XsltDynamicError(
            f"values of type {_local_name(left)} are not ordered by the lt operator",
            code="XTDE1030",
        )
    if _local_name(left) != _local_name(right):
        raise XsltDynamicError(
            f"sort keys of types {_local_name(left)} and {_local_name(right)} are not comparable",
            code="XTDE1030",
        )
    try:
        if left.value == right.value:
            return 0
        return -1 if left.value < right.value else 1
    except (TypeError, ValueError) as exc:
        raise XsltDynamicError(
            f"sort keys {left.lexical!r} and {right.lexical!r} are not comparable",
            code="XTDE1030",
        ) from exc


def _compare_key_sequences(
    left: XdmSequence,
    right: XdmSequence,
    key: ResolvedSortKey,
) -> int:
    if not left and not right:
        return 0
    if not left:
        comparison = -1
    elif not right:
        comparison = 1
    else:
        if len(left) > 1 or len(right) > 1:
            raise XsltDynamicError(
                "a sort key value must contain at most one item",
                code="XTTE1020",
            )
        left_item = left[0]
        right_item = right[0]
        if not isinstance(left_item, XdmAtomic) or not isinstance(right_item, XdmAtomic):
            raise XsltDynamicError("a sort key value must be atomic", code="XPTY0004")
        comparison = _compare_atomic(left_item, right_item, key)
    return comparison if key.order == "ascending" else -comparison


def compare_sort_key_sets(
    left: tuple[XdmSequence, ...],
    right: tuple[XdmSequence, ...],
    keys: tuple[ResolvedSortKey, ...],
) -> int:
    """Compare two fully evaluated sort key tuples (major to minor)."""
    for left_value, right_value, key in zip(left, right, keys, strict=True):
        comparison = _compare_key_sequences(left_value, right_value, key)
        if comparison:
            return comparison
    return 0


def _sort_numeric_modes(
    keys: tuple[ResolvedSortKey, ...],
    key_values: Sequence[tuple[XdmSequence, ...]],
) -> tuple[ResolvedSortKey, ...]:
    """Select one numeric comparison type for all values of each sort key.

    XSLT 3.0 requires the same implementation of ``lt`` for every comparison in
    one sort key component: the most specific type to which all ordinary values
    can be converted by subtype substitution and type promotion.  Pairwise
    promotion can be non-transitive, so the mode is selected across the complete
    value set first.
    """
    modes: list[str] = []
    for index, key in enumerate(keys):
        locals_found: set[str] = set()
        ordinary = False
        comparable = True
        for values in key_values:
            sequence = values[index]
            if not sequence:
                continue
            if len(sequence) != 1 or not isinstance(sequence[0], XdmAtomic):
                comparable = False
                break
            item = sequence[0]
            if key.data_type == "text":
                continue
            if key.data_type == "number":
                continue
            if _family(item) != "numeric":
                comparable = False
                break
            ordinary = True
            locals_found.add(_local_name(item))
        if not comparable or not ordinary:
            modes.append("exact")
        elif "double" in locals_found:
            modes.append("double")
        elif "float" in locals_found:
            modes.append("float")
        else:
            modes.append("exact")
    return tuple(replace(key, numeric_mode=mode) for key, mode in zip(keys, modes, strict=True))


def sort_records(
    records: Sequence[Any],
    keys: tuple[ResolvedSortKey, ...],
    key_values: Sequence[tuple[XdmSequence, ...]],
) -> tuple[Any, ...]:
    """Sort *records* stably by their pre-evaluated sort key values."""
    if len(records) != len(key_values):
        raise ValueError("each record requires one sort key tuple")
    keys = _sort_numeric_modes(keys, key_values)
    decorated = list(enumerate(zip(records, key_values, strict=True)))

    def compare(left: tuple[int, Any], right: tuple[int, Any]) -> int:
        left_index, (_, left_keys) = left
        right_index, (_, right_keys) = right
        comparison = compare_sort_key_sets(left_keys, right_keys, keys)
        if comparison:
            return comparison
        # Python's sort is stable, but an explicit index keeps equal keys in
        # initial-sequence order even if a future implementation changes that.
        return -1 if left_index < right_index else (1 if left_index > right_index else 0)

    decorated.sort(key=cmp_to_key(compare))
    return tuple(record for _, (record, _) in decorated)


def _numeric_equal(left: XdmAtomic, right: XdmAtomic) -> bool:
    left_value = left.value
    right_value = right.value
    left_nan = _is_nan(left_value)
    right_nan = _is_nan(right_value)
    if left_nan or right_nan:
        return left_nan and right_nan
    left_local = _local_name(left)
    right_local = _local_name(right)
    if left_local in ("double", "float") or right_local in ("double", "float"):
        return _as_float(left_value) == _as_float(right_value)
    return bool(left_value == right_value)


def atomic_equal(
    left: XdmAtomic,
    right: XdmAtomic,
    collation: Collation,
) -> bool:
    """``fn:deep-equal`` equality for two atomic values.

    Values that the ``eq`` operator cannot compare are simply unequal, except
    that two NaNs are equal as ``fn:deep-equal`` requires.
    """
    left_family = _family(left)
    right_family = _family(right)
    if left_family == "string" and right_family == "string":
        return collation.equal(left.lexical, right.lexical)
    if left_family == "numeric" and right_family == "numeric":
        return _numeric_equal(left, right)
    if left_family != right_family:
        return False
    if left_family == "boolean":
        return bool(left.value) is bool(right.value)
    if left_family == "qname":
        return left.same_key(right)
    if left_family == "other" and _local_name(left) != _local_name(right):
        return False
    try:
        return bool(left.value == right.value)
    except (TypeError, ValueError):
        return False


def deep_equal_sequences(
    left: XdmSequence,
    right: XdmSequence,
    collation: Collation,
) -> bool:
    """``fn:deep-equal`` for two atomic sequences."""
    if len(left) != len(right):
        return False
    for left_item, right_item in zip(left, right, strict=True):
        if not isinstance(left_item, XdmAtomic) or not isinstance(right_item, XdmAtomic):
            return False
        if not atomic_equal(left_item, right_item, collation):
            return False
    return True


# ---------------------------------------------------------------------------
# Grouping


@dataclass(frozen=True, slots=True)
class Group:
    """One non-empty group in first-appearance order."""

    items: tuple[XdmItem, ...]
    initial_item: XdmItem
    grouping_key: XdmSequence | None

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("a group is never empty")
        if self.items[0] is not self.initial_item:
            raise ValueError("a group initial item must be its first member")


def _find_group(
    groups: list[list[XdmItem]], keys: list[XdmSequence], key: XdmSequence, collation: Collation
) -> int | None:
    for index, existing in enumerate(keys):
        if deep_equal_sequences(existing, key, collation):
            return index
    return None


def group_by(
    items: Sequence[XdmItem],
    key_sets: Sequence[Sequence[XdmSequence]],
    collation: Collation,
) -> tuple[Group, ...]:
    """Value-based grouping with zero or more keys per item.

    ``key_sets`` has one entry per population item; each entry is the item's
    sequence of distinct grouping keys.  Composite keys are represented as one
    entry containing the full key sequence.
    """
    groups: list[list[XdmItem]] = []
    group_keys: list[XdmSequence] = []
    for item, keys in zip(items, key_sets, strict=True):
        seen: list[int] = []
        for key in keys:
            index = _find_group(groups, group_keys, key, collation)
            if index is None:
                groups.append([item])
                group_keys.append(key)
                seen.append(len(groups) - 1)
                continue
            if index not in seen:
                groups[index].append(item)
                seen.append(index)
    return tuple(
        Group(tuple(members), members[0], group_keys[index]) for index, members in enumerate(groups)
    )


def group_adjacent(
    items: Sequence[XdmItem],
    keys: Sequence[XdmSequence],
    collation: Collation,
) -> tuple[Group, ...]:
    """Adjacent grouping; equal neighbouring keys share a group."""
    groups: list[list[XdmItem]] = []
    group_keys: list[XdmSequence] = []
    for item, key in zip(items, keys, strict=True):
        if groups and deep_equal_sequences(group_keys[-1], key, collation):
            groups[-1].append(item)
            continue
        groups.append([item])
        group_keys.append(key)
    return tuple(
        Group(tuple(members), members[0], group_keys[index]) for index, members in enumerate(groups)
    )


def group_starting_with(
    items: Sequence[XdmItem],
    matches: Sequence[bool],
) -> tuple[Group, ...]:
    groups: list[list[XdmItem]] = []
    for item, matches_pattern in zip(items, matches, strict=True):
        if not groups or matches_pattern:
            groups.append([item])
        else:
            groups[-1].append(item)
    return tuple(Group(tuple(members), members[0], None) for members in groups)


def group_ending_with(
    items: Sequence[XdmItem],
    matches: Sequence[bool],
) -> tuple[Group, ...]:
    groups: list[list[XdmItem]] = []
    previous_matched = False
    for index, (item, matches_pattern) in enumerate(zip(items, matches, strict=True)):
        if index == 0 or previous_matched:
            groups.append([item])
        else:
            groups[-1].append(item)
        previous_matched = matches_pattern
    return tuple(Group(tuple(members), members[0], None) for members in groups)


# ---------------------------------------------------------------------------
# Merging algorithms


def merge_key_equal(
    left: tuple[XdmSequence, ...],
    right: tuple[XdmSequence, ...],
    keys: tuple[ResolvedSortKey, ...],
) -> bool:
    """Whether two composite merge keys are equal under their collations."""
    if len(left) != len(right):
        return False
    return all(
        deep_equal_sequences(left_value, right_value, key.collation)
        for left_value, right_value, key in zip(left, right, keys, strict=True)
    )


def compare_merge_key_sets(
    left: tuple[XdmSequence, ...],
    right: tuple[XdmSequence, ...],
    keys: tuple[ResolvedSortKey, ...],
) -> int:
    """Compare two composite merge keys across input sequences.

    Values that cannot be compared by the ``le`` operator are the merge
    instruction's type error, not a sort-key comparison failure.
    """
    try:
        return compare_sort_key_sets(left, right, keys)
    except XsltDynamicError as exc:
        if exc.code == "XTDE1030":
            raise XsltDynamicError(
                "merge key values from different input sequences are not comparable",
                code="XTTE2230",
                cause=exc,
            ) from exc
        raise


def check_merge_order(
    items: Sequence[MergeItem],
    keys: tuple[ResolvedSortKey, ...],
) -> None:
    """Reject an input sequence that is not already sorted on its merge key."""
    for previous, current in pairwise(items):
        if compare_sort_key_sets(previous.key, current.key, keys) > 0:
            raise XsltDynamicError(
                "a merge input sequence is not correctly sorted on its merge key",
                code="XTDE2220",
            )


def sort_merge_input(
    items: Sequence[MergeItem],
    keys: tuple[ResolvedSortKey, ...],
) -> tuple[MergeItem, ...]:
    """Sort one input sequence by its composite merge key values."""
    return sort_records(
        tuple(items),
        keys,
        tuple(item.key for item in items),
    )


def merge_groups(
    inputs: Sequence[Sequence[MergeItem]],
    keys: tuple[ResolvedSortKey, ...],
) -> tuple[MergeGroup, ...]:
    """Merge sorted input sequences into groups in composite-key order.

    *inputs* must already be ordered by merge source, then anchor, then input
    position.  The algorithm consumes only the heads of the grounded sequences
    and retains duplicate items and repeated occurrences exactly as selected.
    """
    positions = [0] * len(inputs)
    groups: list[MergeGroup] = []
    for sequence in inputs:
        for item in sequence:
            if len(item.key) != len(keys):
                raise ValueError("a merge item key must match the merge key specification")
    while True:
        candidates = [
            (index, sequence[positions[index]])
            for index, sequence in enumerate(inputs)
            if positions[index] < len(sequence)
        ]
        if not candidates:
            break
        best_item = candidates[0][1]
        for _, item in candidates[1:]:
            if compare_merge_key_sets(item.key, best_item.key, keys) < 0:
                best_item = item
        members: list[MergeItem] = []
        for index, sequence in enumerate(inputs):
            position = positions[index]
            while position < len(sequence) and merge_key_equal(
                sequence[position].key, best_item.key, keys
            ):
                members.append(sequence[position])
                position += 1
            positions[index] = position
        groups.append(MergeGroup(tuple(members), best_item.key))
    return tuple(groups)


# ---------------------------------------------------------------------------
# Atomization and grouping keys


def atomize_key_sequence(sequence: XdmSequence) -> tuple[XdmAtomic, ...]:
    """Atomize one sort/group key expression result.

    ``xs:untypedAtomic`` values are cast to ``xs:string`` for grouping and
    sorting as XSLT 3.0 requires.
    """
    values: list[XdmAtomic] = []
    for item in sequence:
        if isinstance(item, XdmAtomic):
            atomic = item
        elif isinstance(item, XdmNode):
            atomics = tuple(value for value in item.typed_value if isinstance(value, XdmAtomic))
            values.extend(
                XdmAtomic.string(value.lexical) if _local_name(value) == "untypedAtomic" else value
                for value in atomics
            )
            continue
        else:
            raise XsltDynamicError(
                "a grouping or sort key cannot atomize a function item",
                code="FOTY0013",
            )
        values.append(
            XdmAtomic.string(atomic.lexical) if _local_name(atomic) == "untypedAtomic" else atomic
        )
    return tuple(values)


def grouping_key_set(
    sequence: XdmSequence,
    *,
    composite: bool,
    collation: Collation,
) -> tuple[XdmSequence, ...]:
    """Distinct grouping keys for one population item."""
    atomics = atomize_key_sequence(sequence)
    if composite:
        return (tuple(atomics),)
    distinct: list[XdmSequence] = []
    for atomic in atomics:
        key: XdmSequence = (atomic,)
        if not any(deep_equal_sequences(existing, key, collation) for existing in distinct):
            distinct.append(key)
    return tuple(distinct)


def adjacent_grouping_key(
    sequence: XdmSequence,
    *,
    composite: bool,
) -> XdmSequence:
    """One grouping key for ``group-adjacent``."""
    atomics = atomize_key_sequence(sequence)
    if composite:
        return tuple(atomics)
    if len(atomics) != 1:
        raise XsltDynamicError(
            "xsl:for-each-group group-adjacent requires exactly one key value "
            "unless composite='yes'",
            code="XTTE1100",
        )
    return (atomics[0],)
