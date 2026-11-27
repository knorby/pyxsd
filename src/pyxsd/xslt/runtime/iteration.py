"""Explicit XSLT iteration state, execution, and normative sorting/grouping.

Iteration is heap state, not Python loop state.  An :class:`IterationState`
records the input sequence, the processing position, the current iteration
parameters, and any pending ``xsl:next-iteration`` or ``xsl:break`` control.
The processor owns one scheduler frame for the iteration and a fresh body frame
per item, so parameters and early exit survive suspension and never depend on a
Python ``for``/``while`` temporary.

The module also executes ``xsl:for-each``, ``xsl:iterate``,
``xsl:for-each-group``, ``xsl:merge``, and sorting.  Each entry point receives
the invocation coordinator and an immutable evaluation context, and uses the
coordinator's shared expression, plan, frame, resource, and accumulator
services.

Sorting and grouping use XDM value semantics directly.  Strings are compared
with the selected collation (the codepoint collation by default), numbers use
XPath numeric promotion with NaN ordered below every other number, and
grouping keys use ``fn:deep-equal`` semantics rather than Python container or
``locale`` ordering.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cmp_to_key
from itertools import pairwise
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin, urlsplit

from elementpath.exceptions import MissingContextError

from pyxsd.xdm import XdmAtomic, XdmItem, XdmSequence
from pyxsd.xdm.legacy import ResultTreeFragment
from pyxsd.xdm.nodes import XdmNode
from pyxsd.xdm.values import _as_float, _as_float32, _is_nan
from pyxsd.xpath.environment import Focus
from pyxsd.xslt.compiler.patterns import PatternMatcher
from pyxsd.xslt.compiler.plans import (
    _BreakPlan,
    _ForEachGroupPlan,
    _ForEachPlan,
    _IteratePlan,
    _MergePlan,
    _MergeSourcePlan,
    _NextIterationPlan,
    _ParamPlan,
    _PerformSortPlan,
    _WithParamPlan,
)
from pyxsd.xslt.errors import XsltDynamicError, XsltStaticError
from pyxsd.xslt.results import _build_result_tree
from pyxsd.xslt.runtime.context import EvaluationContext
from pyxsd.xslt.runtime.frames import (
    NO_GROUP,
    NO_INPUT,
    Call,
    Frame,
    FrameResult,
    _TemplateContext,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xslt.compiler.packages import Component
    from pyxsd.xslt.processor import _Invocation

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
    "execute_iteration",
    "execute_merge",
    "group_adjacent",
    "group_by",
    "group_ending_with",
    "group_starting_with",
    "grouping_key_set",
    "iteration_control",
    "merge_groups",
    "merge_key_equal",
    "perform_sort",
    "promote_merge_keys",
    "resolve_collation",
    "sort_merge_input",
    "sort_records",
    "sorted_items",
    "start_iteration_plan",
    "step_iterate",
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
_UNORDERED_LOCALS = frozenset(
    {"boolean", "NOTATION", "QName", "base64Binary", "hexBinary", "duration"}
)

#: Date/time types whose order is defined by their timeline value rather than
#: their lexical form.
_DATETIME_LOCALS = frozenset({"date", "time", "dateTime", "dateTimeStamp"})


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

    ``kind`` is ``codepoint``, ``html-ascii``, ``caseblind``, ``uca``, or
    ``custom``.  The UCA variant is a deterministic approximation that
    implements the normative parameters that affect ordering (strength,
    caseFirst, and numeric) without depending on the process locale.  A custom
    collation carries a host ``custom_compare`` callable, a ``sort_key``
    callable, or both; equality ignores those callables so a resolved
    collation compares by its observable identity.
    """

    uri: str
    kind: str = "codepoint"
    strength: str = "tertiary"
    case_first: str = "upper"
    numeric: bool = False
    language: str = ""
    custom_compare: Callable[[str, str], int] | None = field(
        default=None, compare=False, repr=False
    )
    sort_key: Callable[[str], Any] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in ("codepoint", "html-ascii", "caseblind", "uca", "custom"):
            raise ValueError(f"unknown collation kind {self.kind!r}")
        if self.kind == "custom":
            if self.custom_compare is not None and not callable(self.custom_compare):
                raise TypeError("a custom collation compare must be callable")
            if self.sort_key is not None and not callable(self.sort_key):
                raise TypeError("a custom collation sort key must be callable")
            if self.custom_compare is None and self.sort_key is None:
                raise ValueError("a custom collation requires a compare or sort key")

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
        if self.kind == "custom":
            if self.custom_compare is not None:
                return self.custom_compare(left, right)
            assert self.sort_key is not None
            left_key = self.sort_key(left)
            right_key = self.sort_key(right)
            if left_key == right_key:
                return 0
            return -1 if left_key < right_key else 1
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
    registry: Mapping[str, Collation] | None = None,
) -> Collation:
    """Resolve and validate one collation URI.

    ``code`` is the XSLT dynamic error code used for an unrecognized collation
    (``XTDE1035`` for sorting, ``XTDE1110`` for grouping).  A missing URI uses
    *default*; when that is also missing the Unicode codepoint collation is
    selected.  A ``lang``/``case-order`` pair without an explicit collation
    selects a deterministic UCA-style ordering.  *registry* supplies custom
    collations registered by the compiled stylesheet; it never widens the
    collation set for general XPath string functions.
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
    if registry is not None:
        custom = registry.get(uri)
        if custom is not None:
            return custom
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
    implicit_timezone: Any = None

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

    ``key`` is the comparison key after any ``data-type`` conversion, while
    ``current_key`` is the item's atomized key with ``xs:untypedAtomic`` cast to
    ``xs:string`` and no comparison conversion.  ``current-merge-key()`` exposes
    the first group item's ``current_key``, not its comparison key.
    """

    item: XdmItem
    source: int
    anchor: int
    position: int
    key: tuple[XdmSequence, ...]
    current_key: tuple[XdmSequence, ...] | None = None

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
        _validate_merge_key_components(self.key, "a merge item key")
        current_key = self.key if self.current_key is None else self.current_key
        _validate_merge_key_components(current_key, "a merge item current key")
        object.__setattr__(self, "current_key", current_key)


@dataclass(frozen=True, slots=True)
class MergeGroup:
    """One group of items sharing a composite merge key.

    ``key`` is the first item's current key (what ``current-merge-key()``
    exposes); ``comparison_key`` is the first item's comparison key used to
    order the group.
    """

    items: tuple[MergeItem, ...]
    key: tuple[XdmSequence, ...]
    comparison_key: tuple[XdmSequence, ...] | None = None

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("a merge group is never empty")
        if not all(isinstance(item, MergeItem) for item in self.items):
            raise TypeError("a merge group requires merge items")
        _validate_merge_key_components(self.key, "a merge group key")
        comparison_key = self.key if self.comparison_key is None else self.comparison_key
        _validate_merge_key_components(comparison_key, "a merge group comparison key")
        object.__setattr__(self, "comparison_key", comparison_key)


def _validate_merge_key_components(key: object, label: str) -> None:
    if not isinstance(key, tuple):
        raise ValueError(f"{label} must be a tuple of key components")
    for component in key:
        if not isinstance(component, tuple) or not all(
            isinstance(value, XdmAtomic) for value in component
        ):
            raise ValueError(f"{label} component must be an XDM sequence of atomic values")


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
    if _local_name(left) in _DATETIME_LOCALS or _local_name(right) in _DATETIME_LOCALS:
        return _compare_datetime(left, right, key.implicit_timezone)
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


def _compare_datetime(left: XdmAtomic, right: XdmAtomic, implicit_timezone: Any) -> int:
    """Compare two date/time values on their timeline.

    pyxsd keeps these atomics in their XSD lexical form, so a direct string
    comparison orders ``19:00:30+01:00`` before ``18:00:00``.  F&O compares the
    timeline value and supplies the implicit timezone for an operand without
    one.
    """
    from elementpath.datatypes import Date, DateTime, Time

    local = _local_name(left)
    if local != _local_name(right):
        raise XsltDynamicError(
            f"sort keys {left.lexical!r} and {right.lexical!r} are not comparable",
            code="XTDE1030",
        )
    datatype = cast(Any, {"date": Date, "time": Time}.get(local, DateTime))
    try:
        first = datatype.fromstring(left.lexical)
        second = datatype.fromstring(right.lexical)
    except (TypeError, ValueError) as exc:
        raise XsltDynamicError(
            f"sort keys {left.lexical!r} and {right.lexical!r} are not comparable",
            code="XTDE1030",
        ) from exc
    if implicit_timezone is not None:
        if first.tzinfo is None:
            first.tzinfo = implicit_timezone
        if second.tzinfo is None:
            second.tzinfo = implicit_timezone
    if first == second:
        return 0
    return -1 if first < second else 1


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


def _sort_records_with_keys(
    records: Sequence[Any],
    keys: tuple[ResolvedSortKey, ...],
    key_values: Sequence[tuple[XdmSequence, ...]],
) -> tuple[Any, ...]:
    """Sort records stably with already-resolved comparison keys."""
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


def sort_records(
    records: Sequence[Any],
    keys: tuple[ResolvedSortKey, ...],
    key_values: Sequence[tuple[XdmSequence, ...]],
) -> tuple[Any, ...]:
    """Sort *records* stably by their pre-evaluated sort key values."""
    if len(records) != len(key_values):
        raise ValueError("each record requires one sort key tuple")
    return _sort_records_with_keys(
        records,
        _sort_numeric_modes(keys, key_values),
        key_values,
    )


def _numeric_equal(left: XdmAtomic, right: XdmAtomic) -> bool:
    left_value = left.value
    right_value = right.value
    left_nan = _is_nan(left_value)
    right_nan = _is_nan(right_value)
    if left_nan or right_nan:
        return left_nan and right_nan
    left_local = _local_name(left)
    right_local = _local_name(right)
    if left_local == "double" or right_local == "double":
        return _as_float(left_value) == _as_float(right_value)
    if left_local == "float" or right_local == "float":
        # F&O numeric promotion prefers double, then float, then decimal: a
        # float/decimal comparison happens in binary32, not binary64.
        return _as_float32(left_value) == _as_float32(right_value)
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


def promote_merge_keys(
    inputs: Sequence[Sequence[MergeItem]],
    keys: tuple[ResolvedSortKey, ...],
) -> tuple[ResolvedSortKey, ...]:
    """Select one numeric comparison mode for every merge key component.

    XSLT requires one ``lt`` implementation for all comparisons of a key
    component.  Pairwise promotion can be non-transitive, so the mode is chosen
    across every item of every merge input sequence before any ordering check or
    group comparison.
    """
    key_values = tuple(item.key for sequence in inputs for item in sequence)
    return _sort_numeric_modes(keys, key_values)


def check_merge_order(
    items: Sequence[MergeItem],
    keys: tuple[ResolvedSortKey, ...],
    *,
    comparison_keys: tuple[ResolvedSortKey, ...] | None = None,
) -> None:
    """Reject an input sequence that is not already sorted on its merge key.

    Without an explicit ``comparison_keys`` tuple the promoted numeric mode is
    selected across this sequence, so a mixed ``xs:double``/``xs:decimal``
    sequence that is equal under double promotion is not falsely rejected.
    """
    effective = (
        comparison_keys
        if comparison_keys is not None
        else _sort_numeric_modes(keys, tuple(item.key for item in items))
    )
    for previous, current in pairwise(items):
        if compare_sort_key_sets(previous.key, current.key, effective) > 0:
            raise XsltDynamicError(
                "a merge input sequence is not correctly sorted on its merge key",
                code="XTDE2220",
            )


def sort_merge_input(
    items: Sequence[MergeItem],
    keys: tuple[ResolvedSortKey, ...],
    *,
    comparison_keys: tuple[ResolvedSortKey, ...] | None = None,
) -> tuple[MergeItem, ...]:
    """Sort one input sequence by its composite merge key values.

    Without explicit ``comparison_keys`` the numeric mode is selected across
    this sequence.  ``_run_merge`` supplies the merge-wide promoted tuple so a
    ``sort-before-merge`` sequence is ordered with the same ``lt``
    implementation used for its subsequent order check and group merge.
    """
    key_values = tuple(item.key for item in items)
    effective = (
        comparison_keys if comparison_keys is not None else _sort_numeric_modes(keys, key_values)
    )
    return _sort_records_with_keys(tuple(items), effective, key_values)


def merge_groups(
    inputs: Sequence[Sequence[MergeItem]],
    keys: tuple[ResolvedSortKey, ...],
    *,
    comparison_keys: tuple[ResolvedSortKey, ...] | None = None,
) -> tuple[MergeGroup, ...]:
    """Merge sorted input sequences into groups in composite-key order.

    *inputs* must already be ordered by merge source, then anchor, then input
    position.  The algorithm consumes only the heads of the grounded sequences
    and retains duplicate items and repeated occurrences exactly as selected.
    Group identity comes from the first item's ``current_key`` while ordering
    uses the promoted comparison key.
    """
    for sequence in inputs:
        for item in sequence:
            if len(item.key) != len(keys):
                raise ValueError("a merge item key must match the merge key specification")
    effective = comparison_keys if comparison_keys is not None else promote_merge_keys(inputs, keys)
    positions = [0] * len(inputs)
    groups: list[MergeGroup] = []
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
            if compare_merge_key_sets(item.key, best_item.key, effective) < 0:
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
        groups.append(
            MergeGroup(
                tuple(members),
                best_item.current_key if best_item.current_key is not None else best_item.key,
                best_item.key,
            )
        )
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
        if isinstance(item, ResultTreeFragment):
            values.append(XdmAtomic.string(item.string_value))
            continue
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


# ---------------------------------------------------------------------------
# Instruction-family execution


def start_iteration_plan(
    invocation: _Invocation,
    plan: _ForEachPlan | _IteratePlan | _ForEachGroupPlan,
    frame: Frame,
    cursor: int,
) -> FrameResult:
    context = frame.context
    variables = context.locals
    if isinstance(plan, _ForEachPlan):
        items = invocation._evaluate(plan.expression, context)
        invocation.profile_for(plan.expression.static_context.xslt_version).check_legacy_node_set(
            items, "the select attribute of xsl:for-each"
        )
        items = sorted_items(invocation, items, plan.sorts, context)
        state = IterationState(
            kind="for-each",
            body=plan.children,
            items=tuple(
                IterationItem(item, position, len(items))
                for position, item in enumerate(items, start=1)
            ),
            base_variables=dict(variables),
        )
    elif isinstance(plan, _IteratePlan):
        items = invocation._evaluate(plan.expression, context)
        parameters = initial_iteration_parameters(invocation, plan.parameters, context)
        state = IterationState(
            kind="iterate",
            body=plan.children,
            items=tuple(
                IterationItem(item, position, len(items))
                for position, item in enumerate(items, start=1)
            ),
            base_variables=dict(variables),
            parameters=parameters,
            parameter_plans={parameter.name: parameter for parameter in plan.parameters},
            on_completion=plan.on_completion,
        )
    else:
        groups = grouped_items(invocation, plan, frame)
        state = IterationState(
            kind="for-each-group",
            body=plan.children,
            items=tuple(
                IterationItem(
                    group.initial_item,
                    position,
                    len(groups),
                    group.items,
                    group.grouping_key,
                )
                for position, group in enumerate(groups, start=1)
            ),
            base_variables=dict(variables),
        )
    child = Frame(
        kind="iteration",
        component=frame.component,
        identity=f"xsl:{state.kind}",
        program=state,
        context=context,
        location=plan.instruction.location,
        group=frame.group,
        grouping_key=frame.grouping_key,
    )
    return Call(child, resume=replace(frame, cursor=cursor + 1))


def step_iterate(invocation: _Invocation, frame: Frame) -> FrameResult:
    state = cast(IterationState, frame.program)
    body_returned = frame.input is not NO_INPUT
    frame = invocation._consume_input(frame)
    if body_returned:
        if state.control == "break":
            state.control = None
            return invocation._return_frame(frame)
        if state.control == "next":
            state.parameters = state.pending_parameters or state.parameters
            state.pending_parameters = None
        state.control = None
        state.index += 1
    if state.index >= len(state.items):
        return complete_iteration(invocation, frame, state)
    entry = state.items[state.index]
    variables: dict[str, XdmSequence] = dict(state.base_variables)
    if state.kind == "iterate":
        for name, value in state.parameters.items():
            invocation._bind(variables, name, value)
    group = frame.group if entry.group is None else entry.group
    if entry.group is None:
        grouping_key = frame.grouping_key
    else:
        grouping_key = NO_GROUP if entry.grouping_key is None else entry.grouping_key
    child = Frame(
        kind="instruction",
        component=frame.component,
        identity=f"iteration body {state.index + 1}",
        program=state.body,
        context=replace(
            frame.context,
            focus=Focus.singleton(
                entry.item,
                position=entry.position,
                size=entry.size,
            ),
            locals=variables,
        ),
        location=frame.location,
        group=NO_GROUP if group is None else group,
        grouping_key=NO_GROUP if grouping_key is None else grouping_key,
        iteration=state,
    )
    return Call(child, resume=frame, tail=False)


def complete_iteration(invocation: _Invocation, frame: Frame, state: IterationState) -> FrameResult:
    completion = state.on_completion
    if completion is None:
        return invocation._return_frame(frame)
    variables: dict[str, XdmSequence] = dict(state.base_variables)
    for name, value in state.parameters.items():
        invocation._bind(variables, name, value)
    completion_context = replace(frame.context, focus=Focus.absent(), locals=variables)
    try:
        if completion.select is not None:
            value = invocation._evaluate(completion.select, completion_context)
        else:
            value = invocation._execute_plans(
                completion.children,
                context=completion_context,
            )
    except MissingContextError as exc:
        error = XsltDynamicError(
            "xsl:on-completion has no context item",
            code="XPDY0002",
            location=completion.instruction.location,
            cause=exc,
        )
        error.__cause__ = exc
        error.__suppress_context__ = True
        raise error from exc
    return invocation._return_frame(replace(frame, values=frame.values + tuple(value)))


def iteration_control(
    invocation: _Invocation,
    plan: _NextIterationPlan | _BreakPlan,
    frame: Frame,
) -> FrameResult:
    state = frame.iteration
    if not isinstance(state, IterationState) or state.kind != "iterate":
        raise XsltStaticError(
            f"xsl:{plan.instruction.local_name} requires an enclosing xsl:iterate",
            code="XTSE3120",
            uri=plan.instruction.module,
            location=plan.instruction.location,
        )
    if isinstance(plan, _NextIterationPlan):
        supplied = iteration_parameter_values(invocation, plan.parameters, frame, state)
        state.pending_parameters = {**state.parameters, **supplied}
        state.control = "next"
        return invocation._return_frame(frame)
    value = break_value(invocation, plan, frame)
    state.control = "break"
    return invocation._return_frame(replace(frame, values=frame.values + tuple(value)))


def break_value(invocation: _Invocation, plan: _BreakPlan, frame: Frame) -> XdmSequence:
    if plan.expression is not None:
        return invocation._evaluate(plan.expression, frame.context)
    return invocation._execute_plans(plan.children, context=frame.context)


def initial_iteration_parameters(
    invocation: _Invocation,
    parameters: tuple[_ParamPlan, ...],
    context: EvaluationContext,
) -> dict[str, XdmSequence]:
    values: dict[str, XdmSequence] = {}
    environment = dict(context.locals)
    for parameter in parameters:
        value = invocation._parameter_default(
            parameter,
            replace(context, locals=environment),
        )
        value = invocation._coerce_parameter(
            parameter, value, code="XTTE0590", package_name=context.package_name
        )
        invocation._bind(values, parameter.name, value)
        invocation._bind(environment, parameter.name, value)
    return values


def iteration_parameter_values(
    invocation: _Invocation,
    parameters: tuple[_WithParamPlan, ...],
    frame: Frame,
    state: IterationState,
) -> dict[str, XdmSequence]:
    values: dict[str, XdmSequence] = {}
    for parameter in parameters:
        if parameter.expression is not None:
            value = invocation._evaluate(parameter.expression, frame.context)
        elif parameter.children:
            sequence = invocation._execute_plans(
                parameter.children,
                context=replace(frame.context, final_output=False),
            )
            value = sequence if parameter.has_as else (_build_result_tree(sequence),)
        else:
            value = () if parameter.has_as else (XdmAtomic.string(""),)
        if parameter.sequence_type is not None:
            value = invocation._coerce(
                value,
                parameter.sequence_type,
                code="XTTE0590",
                label=f"parameter {parameter.name!r}",
                package_name=frame.context.package_name,
            )
        declared = state.parameter_plans[parameter.name]
        value = invocation._coerce_parameter(
            declared, value, code="XTTE0590", package_name=frame.context.package_name
        )
        invocation._bind(values, parameter.name, value)
    return values


def execute_merge(
    plan: _MergePlan, invocation: _Invocation, *, context: EvaluationContext
) -> XdmSequence:
    resolved = merge_resolved_keys(invocation, plan.sources, context)
    inputs: list[tuple[MergeItem, ...]] = []
    sort_before_merge: list[bool] = []
    for source_index, source in enumerate(plan.sources):
        for anchor_index, sequence in enumerate(merge_input_sequences(invocation, source, context)):
            items = tuple(
                merge_item(
                    invocation,
                    source,
                    resolved,
                    item,
                    source_index=source_index,
                    anchor_index=anchor_index,
                    position=position,
                    context=context,
                )
                for position, item in enumerate(sequence, start=1)
            )
            inputs.append(items)
            sort_before_merge.append(source.sort_before_merge)
    comparison_keys = promote_merge_keys(inputs, resolved)
    for index, input_sequence in enumerate(inputs):
        if sort_before_merge[index]:
            inputs[index] = sort_merge_input(
                input_sequence,
                resolved,
                comparison_keys=comparison_keys,
            )
    for input_sequence in inputs:
        check_merge_order(input_sequence, resolved, comparison_keys=comparison_keys)
    groups = merge_groups(inputs, resolved, comparison_keys=comparison_keys)
    names = _merge_source_names(plan.sources)
    result: list[XdmItem] = []
    for position, group in enumerate(groups, start=1):
        members: dict[str, list[XdmItem]] = {name: [] for name in names}
        for item in group.items:
            members[names[item.source]].append(item.item)
        group_map: dict[str, XdmSequence] = {
            name: tuple(values) for name, values in members.items()
        }
        action_focus = Focus.singleton(
            group.items[0].item,
            position=position,
            size=len(groups),
        )
        key_sequence: XdmSequence = tuple(atomic for component in group.key for atomic in component)
        with invocation._merge_scope(group_map, key_sequence, names):
            result.extend(
                invocation._execute_plans(
                    plan.action,
                    context=replace(context, focus=action_focus),
                )
            )
    return tuple(result)


def merge_resolved_keys(
    invocation: _Invocation,
    sources: tuple[_MergeSourcePlan, ...],
    context: EvaluationContext,
) -> tuple[ResolvedSortKey, ...]:
    resolved_by_source: list[tuple[ResolvedSortKey, ...]] = []
    signatures_by_source: list[tuple[tuple[object, ...], ...]] = []
    for source in sources:
        resolved: list[ResolvedSortKey] = []
        signatures: list[tuple[object, ...]] = []
        for key in source.keys:
            key_resolved, signature = resolve_key_spec(invocation, key, context)
            resolved.append(key_resolved)
            signatures.append(signature)
        resolved_by_source.append(tuple(resolved))
        signatures_by_source.append(tuple(signatures))
    baseline = signatures_by_source[0]
    for other_signatures in signatures_by_source[1:]:
        if other_signatures != baseline:
            raise XsltDynamicError(
                "xsl:merge-key definitions in corresponding positions must have the "
                "same effective lang, order, collation, case-order, and data-type",
                code="XTDE2210",
            )
    return resolved_by_source[0]


def merge_input_sequences(
    invocation: _Invocation,
    source: _MergeSourcePlan,
    context: EvaluationContext,
) -> tuple[XdmSequence, ...]:
    package_name = context.package_name
    if source.for_each_item is not None:
        anchors = invocation._evaluate(source.for_each_item, context)
        return tuple(
            invocation._evaluate(
                source.select,
                replace(
                    context,
                    focus=Focus.singleton(
                        anchor,
                        position=position,
                        size=len(anchors),
                    ),
                ),
            )
            for position, anchor in enumerate(anchors, start=1)
        )
    if source.for_each_source is not None:
        uri_values = invocation._evaluate(source.for_each_source, context)
        atomics = atomize_key_sequence(uri_values)
        documents = tuple(
            invocation.document_resources.resolve(
                atomic.lexical,
                base=source.base_uri,
                validation=source.validation,
                type_name=source.type_name,
                schema_pool=invocation._package_schema_pool(package_name),
                input_type_annotations=invocation._document_annotations(
                    package_name,
                    validation=source.validation,
                    type_name=source.type_name,
                ),
                package=package_name,
            )
            for atomic in atomics
        )
        keys = source.use_accumulators
        for document in documents:
            invocation.accumulators.restrict(document, keys)
        return tuple(
            invocation._evaluate(
                source.select,
                replace(
                    context,
                    focus=Focus.singleton(
                        document,
                        position=position,
                        size=len(documents),
                    ),
                ),
            )
            for position, document in enumerate(documents, start=1)
        )
    return (invocation._evaluate(source.select, context),)


def merge_item(
    invocation: _Invocation,
    source: _MergeSourcePlan,
    resolved: tuple[ResolvedSortKey, ...],
    item: XdmItem,
    *,
    source_index: int,
    anchor_index: int,
    position: int,
    context: EvaluationContext,
) -> MergeItem:
    comparison_key, current_key = merge_key_values(
        invocation,
        source,
        resolved,
        item,
        context,
    )
    return MergeItem(
        item=item,
        source=source_index,
        anchor=anchor_index,
        position=position,
        key=comparison_key,
        current_key=current_key,
    )


def merge_key_values(
    invocation: _Invocation,
    source: _MergeSourcePlan,
    resolved: tuple[ResolvedSortKey, ...],
    item: XdmItem,
    context: EvaluationContext,
) -> tuple[tuple[XdmSequence, ...], tuple[XdmSequence, ...]]:
    key_context = replace(context, focus=Focus.singleton(item))
    comparison_values: list[XdmSequence] = []
    current_values: list[XdmSequence] = []
    for key, resolved_key in zip(source.keys, resolved, strict=True):
        if key.select is not None:
            value = invocation._evaluate(key.select, key_context)
        else:
            value = invocation._execute_plans(
                key.children,
                context=replace(key_context, final_output=False),
            )
        atomics = atomize_key_sequence(value)
        if len(atomics) > 1:
            raise XsltDynamicError(
                "a merge key value must contain at most one item after atomization",
                code="XTTE1020",
                location=key.instruction.location,
            )
        if not atomics:
            comparison_values.append(())
            current_values.append(())
            continue
        atomic = atomics[0]
        current_values.append((atomic,))
        if resolved_key.data_type == "text":
            comparison_values.append((XdmAtomic.string(atomic.lexical),))
        else:
            comparison_values.append((atomic,))
    return tuple(comparison_values), tuple(current_values)


def resolved_sort_keys(
    invocation: _Invocation,
    sorts: tuple[SortKey, ...],
    context: EvaluationContext,
) -> tuple[ResolvedSortKey, ...]:
    return tuple(resolve_key_spec(invocation, sort, context)[0] for sort in sorts)


def resolve_key_spec(
    invocation: _Invocation,
    sort: SortKey,
    context: EvaluationContext,
) -> tuple[ResolvedSortKey, tuple[object, ...]]:
    """Resolve one sort/merge key and retain its effective-value signature."""
    label = sort.instruction.local_name
    order_value = None
    order = "ascending"
    if sort.order is not None:
        order_value = invocation._value_source(sort.order, context).strip()
        order = order_value
        if order not in ("ascending", "descending"):
            raise XsltDynamicError(
                f"xsl:{label} order must be ascending or descending, got {order!r}",
                code="XTDE0030",
                location=sort.instruction.location,
            )
    data_type_value = None
    data_type = "native"
    if sort.data_type is not None:
        data_type_value = invocation._value_source(sort.data_type, context).strip()
        local = (
            data_type_value[2:].partition("}")[2]
            if data_type_value.startswith("Q{")
            else data_type_value.rpartition(":")[2]
        )
        if local == "text":
            data_type = "text"
        elif local == "number":
            data_type = "number"
        elif not local:
            raise XsltDynamicError(
                f"xsl:{label} data-type must not be empty",
                code="XTDE0030",
                location=sort.instruction.location,
            )
    case_order = None
    if sort.case_order is not None:
        case_order = invocation._value_source(sort.case_order, context).strip()
        if case_order not in ("upper-first", "lower-first"):
            raise XsltDynamicError(
                f"xsl:{label} case-order must be upper-first or lower-first, got {case_order!r}",
                code="XTDE0030",
                location=sort.instruction.location,
            )
    if sort.stable is not None:
        stable = invocation._value_source(sort.stable, context)
        if stable.strip() not in (
            "yes",
            "true",
            "1",
            "no",
            "false",
            "0",
        ):
            raise XsltDynamicError(
                f"xsl:{label} stable has an invalid boolean value {stable!r}",
                code="XTDE0030",
                location=sort.instruction.location,
            )
    language = ""
    if sort.lang is not None:
        language = invocation._value_source(sort.lang, context).strip()
    collation_uri = None
    if sort.collation is not None:
        collation_uri = invocation._value_source(sort.collation, context).strip()
    collation = resolve_collation(
        collation_uri,
        code="XTDE1035",
        default=None,
        base_uri=sort.base_uri,
        language=language if collation_uri is None else "",
        case_order=case_order if collation_uri is None else None,
        registry=_extension_collations(invocation),
    )
    signature = (
        order_value,
        language if sort.lang is not None else None,
        collation.uri if collation_uri is not None else None,
        case_order,
        data_type_value,
    )
    return (
        ResolvedSortKey(
            order=order,
            collation=collation,
            data_type=data_type,
            implicit_timezone=invocation.implicit_timezone,
        ),
        signature,
    )


def sort_values_for(
    invocation: _Invocation,
    item: XdmItem,
    position: int,
    size: int,
    sorts: tuple[SortKey, ...],
    resolved: tuple[ResolvedSortKey, ...],
    context: EvaluationContext,
) -> tuple[XdmSequence, ...]:
    key_context = replace(
        context,
        focus=Focus.singleton(item, position=position, size=size),
    )
    values: list[XdmSequence] = []
    for sort, key in zip(sorts, resolved, strict=True):
        if sort.select is not None:
            value = invocation._evaluate(sort.select, key_context)
        else:
            value = invocation._execute_plans(
                sort.children,
                context=replace(key_context, final_output=False),
            )
        atomics = atomize_key_sequence(value)
        if len(atomics) > 1:
            compatibility = invocation.profile_for(sort.xslt_version).compatibility_mode
            if compatibility:
                atomics = atomics[:1]
            else:
                raise XsltDynamicError(
                    "a sort key value must contain at most one item after atomization",
                    code="XTTE1020",
                    location=sort.instruction.location,
                )
        if not atomics:
            values.append(())
            continue
        atomic = atomics[0]
        if key.data_type == "text":
            values.append((XdmAtomic.string(atomic.lexical),))
        else:
            values.append((atomic,))
    return tuple(values)


def sorted_items(
    invocation: _Invocation,
    items: XdmSequence,
    sorts: tuple[SortKey, ...],
    context: EvaluationContext,
) -> tuple[XdmItem, ...]:
    if not sorts:
        return tuple(items)
    resolved = resolved_sort_keys(invocation, sorts, context)
    key_values = tuple(
        sort_values_for(
            invocation,
            item,
            position,
            len(items),
            sorts,
            resolved,
            context,
        )
        for position, item in enumerate(items, start=1)
    )
    return sort_records(tuple(items), resolved, key_values)


def perform_sort(
    plan: _PerformSortPlan,
    invocation: _Invocation,
    context: EvaluationContext,
) -> XdmSequence:
    if plan.expression is not None:
        items = invocation._evaluate(plan.expression, context)
    else:
        items = invocation._execute_plans(
            plan.children,
            context=replace(context, final_output=False),
        )
    return sorted_items(invocation, items, plan.sorts, context)


def group_collation(
    invocation: _Invocation,
    plan: _ForEachGroupPlan,
    context: EvaluationContext,
) -> Any:
    uri = (
        None
        if plan.collation is None
        else invocation._value_source(plan.collation, context).strip()
    )
    return resolve_collation(
        uri,
        code="XTDE1110",
        default=plan.default_collation,
        base_uri=plan.base_uri,
        registry=_extension_collations(invocation),
    )


def _extension_collations(invocation: _Invocation) -> Mapping[str, Collation] | None:
    """Custom collations captured by the compiled stylesheet, if any."""
    return invocation.stylesheet.extensions.collations or None


def grouped_items(
    invocation: _Invocation,
    plan: _ForEachGroupPlan,
    frame: Frame,
) -> tuple[Any, ...]:
    context = frame.context
    population = invocation._evaluate(plan.expression, context)
    collation = group_collation(invocation, plan, context)
    if plan.algorithm == GROUP_BY:
        assert plan.key_expression is not None
        key_sets = tuple(
            grouping_key_set(
                invocation._evaluate(
                    plan.key_expression,
                    replace(
                        context,
                        focus=Focus.singleton(
                            item,
                            position=position,
                            size=len(population),
                        ),
                    ),
                ),
                composite=plan.composite,
                collation=collation,
            )
            for position, item in enumerate(population, start=1)
        )
        groups = group_by(population, key_sets, collation)
    elif plan.algorithm == GROUP_ADJACENT:
        assert plan.key_expression is not None
        keys = tuple(
            adjacent_grouping_key(
                invocation._evaluate(
                    plan.key_expression,
                    replace(
                        context,
                        focus=Focus.singleton(
                            item,
                            position=position,
                            size=len(population),
                        ),
                    ),
                ),
                composite=plan.composite,
            )
            for position, item in enumerate(population, start=1)
        )
        groups = group_adjacent(population, keys, collation)
    else:
        assert plan.pattern is not None
        if invocation.profile_for().native and any(
            not isinstance(item, XdmNode) for item in population
        ):
            # XSLT 2.0 §14.2: a positional grouping whose population contains
            # an atomic value is a type error.  XSLT 3.0 permits it.
            raise XsltDynamicError(
                "a positional grouping requires a population of nodes",
                code="XTTE1120",
            )
        matcher = PatternMatcher(plan.pattern)
        environment = invocation._pattern_environment_for(
            plan.pattern,
            context.package_name,
            locals_=context.locals if plan.pattern.variable_names else None,
        )
        with invocation._group_scope(None, None):
            matches = tuple(matcher.matches(item, environment) for item in population)
        if plan.algorithm == GROUP_STARTING_WITH:
            groups = group_starting_with(population, matches)
        else:
            groups = group_ending_with(population, matches)
    if not plan.sorts:
        return groups
    resolved = resolved_sort_keys(invocation, plan.sorts, context)
    key_values: list[tuple[XdmSequence, ...]] = []
    for position, group in enumerate(groups, start=1):
        with invocation._group_scope(group.items, group.grouping_key):
            key_values.append(
                sort_values_for(
                    invocation,
                    group.initial_item,
                    position,
                    len(groups),
                    plan.sorts,
                    resolved,
                    context,
                )
            )
    return sort_records(groups, resolved, tuple(key_values))


def execute_iteration(
    plan: _ForEachPlan | _IteratePlan | _ForEachGroupPlan,
    invocation: _Invocation,
    *,
    context: EvaluationContext,
) -> XdmSequence:
    component = (
        _TemplateContext(cast("Component", None))
        if context.rule_context is not None or context.original_functions
        else None
    )
    parent = Frame(
        kind="instruction",
        component=component,
        program=(plan,),
        context=context,
        group=NO_GROUP if invocation.current_group is None else invocation.current_group,
        grouping_key=(
            NO_GROUP if invocation.current_grouping_key is None else invocation.current_grouping_key
        ),
    )
    return cast(XdmSequence, invocation.execute_frame(parent))


def _merge_source_names(sources: tuple[_MergeSourcePlan, ...]) -> tuple[str, ...]:
    """Name each merge source in stylesheet order.

    An unnamed source receives an implementation-dependent name that cannot
    collide with an authored NCName, so ``current-merge-group()`` still returns
    its items in source order while the single-argument form cannot address it.
    """
    return tuple(
        source.name if source.name is not None else f"#merge-source-{index}"
        for index, source in enumerate(sources)
    )
