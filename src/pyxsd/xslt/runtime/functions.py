"""XSLT-defined XPath functions and the per-evaluation context bridge.

An :class:`XsltContextBridge` is an immutable view of the dynamic state that
the XSLT layer adds to one XPath evaluation.  It captures the instruction's
current item, current group/grouping key, current merge group/key, regex
captures, static namespaces, package scope, expression base URI, and the
invocation-local caches owned by a :class:`Transformation`.

The bridge never stores state on a parser class or in a process-global
"current transformation" slot.  Each expression evaluation builds a fresh
bridge, and every stylesheet-style function call starts with the context
components cleared as XSLT 3.0 section 5.3.4 requires.  Features whose runtime
family lands in a later task are represented by their genuine absent state:
``current-group`` outside a grouping instruction raises ``XTDE1061``,
``current-merge-group`` raises ``XTDE3480``, and an accumulator name that is
not declared raises ``XTDE3340`` rather than returning a fabricated value.

The module also owns the static surface shared with the compiler: the XSLT
function declarations legal for an effective profile, the supported system
properties, and the names of XSLT elements for ``element-available``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import lru_cache
from types import MappingProxyType
from typing import Any
from urllib.parse import urljoin, urlsplit

from elementpath.datatypes import QName
from elementpath.helpers import is_ncname
from elementpath.xpath2 import XPath2Parser
from elementpath.xpath3 import XPath31Parser

from pyxsd import __version__
from pyxsd.xdm import XdmAtomic, XdmItem, XdmSequence, as_xdm
from pyxsd.xdm.annotations import builtin_type_classes
from pyxsd.xdm.nodes import XdmDocument, XdmNode
from pyxsd.xpath.environment import Focus, FunctionArity
from pyxsd.xpath.functions import ContextFunction
from pyxsd.xslt.errors import XsltDynamicError

__all__ = [
    "ABSENT",
    "IMPLEMENTED_XSLT_ELEMENTS",
    "SYSTEM_PROPERTIES",
    "XSLT_FUNCTION_ARITIES",
    "XsltContextBridge",
    "xslt_function_declarations",
]

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
FN_NS = "http://www.w3.org/2005/xpath-functions"


#: Marker for an absent dynamic component.  An absent component is never the
#: same thing as an empty sequence.
class _Absent:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic convenience
        return "ABSENT"


ABSENT = _Absent()

#: System properties required by XSLT 3.0 plus the processor's XPath profile.
#: Keys are the canonical lexical ``xsl:*`` names used by static preprocessing.
SYSTEM_PROPERTIES: Mapping[str, str] = MappingProxyType(
    {
        "xsl:version": "3.0",
        "xsl:vendor": "pyxsd",
        "xsl:vendor-url": "https://pyxsd.knorby.com",
        "xsl:product-name": "pyxsd",
        "xsl:product-version": __version__,
        "xsl:is-schema-aware": "yes",
        "xsl:supports-serialization": "yes",
        "xsl:supports-backwards-compatibility": "yes",
        "xsl:supports-namespace-axis": "yes",
        "xsl:supports-streaming": "no",
        "xsl:supports-dynamic-evaluation": "yes",
        "xsl:supports-higher-order-functions": "yes",
        "xsl:xpath-version": "3.1",
        "xsl:xsd-version": "1.1",
    }
)

#: Elements whose implementation exists in this execution slice.  XSLT 3.0
#: section 24.2.2 defines element-available() as true only for implemented
#: elements, so specification-only names stay out of this set.
IMPLEMENTED_XSLT_ELEMENTS = frozenset(
    {
        "accept",
        "apply-imports",
        "apply-templates",
        "assert",
        "attribute",
        "attribute-set",
        "break",
        "call-template",
        "choose",
        "decimal-format",
        "evaluate",
        "expose",
        "fallback",
        "for-each",
        "for-each-group",
        "fork",
        "function",
        "if",
        "import",
        "include",
        "iterate",
        "key",
        "map",
        "map-entry",
        "merge",
        "merge-action",
        "merge-key",
        "merge-source",
        "mode",
        "next-iteration",
        "next-match",
        "on-completion",
        "on-empty",
        "on-non-empty",
        "otherwise",
        "output",
        "override",
        "package",
        "param",
        "perform-sort",
        "preserve-space",
        "result-document",
        "sequence",
        "sort",
        "source-document",
        "strip-space",
        "stylesheet",
        "template",
        "text",
        "transform",
        "use-package",
        "value-of",
        "variable",
        "when",
        "where-populated",
        "with-param",
    }
)

#: XSLT 3.0 additions unavailable to a 2.0-versioned module.
_XSLT_3_ONLY_ELEMENTS = frozenset(
    {
        "accept",
        "accumulator",
        "accumulator-rule",
        "assert",
        "break",
        "context-item",
        "evaluate",
        "expose",
        "fork",
        "global-context-item",
        "iterate",
        "map",
        "map-entry",
        "merge",
        "merge-action",
        "merge-key",
        "merge-source",
        "mode",
        "next-iteration",
        "on-completion",
        "on-empty",
        "on-non-empty",
        "override",
        "package",
        "source-document",
        "stream",
        "try",
        "catch",
        "use-package",
        "where-populated",
    }
)

#: XSLT 2.0 additions unavailable to a 1.0-versioned module.
_XSLT_2_ONLY_ELEMENTS = frozenset(
    {
        "analyze-string",
        "character-map",
        "document",
        "for-each-group",
        "function",
        "import-schema",
        "matching-substring",
        "next-match",
        "non-matching-substring",
        "perform-sort",
        "result-document",
        "sequence",
        "sort",
    }
)


def xslt_element_available(name: str, version: str) -> bool:
    """Whether the XSLT-namespace element *name* is implemented for *version*."""
    if name not in IMPLEMENTED_XSLT_ELEMENTS:
        return False
    if _version_at_least(version, "3.0"):
        return True
    if name in _XSLT_3_ONLY_ELEMENTS:
        return False
    if _version_at_least(version, "2.0"):
        return True
    return name not in _XSLT_2_ONLY_ELEMENTS


#: XSLT-defined function signatures used by static preprocessing availability.
XSLT_FUNCTION_ARITIES: Mapping[str, FunctionArity] = MappingProxyType(
    {
        "available-system-properties": 0,
        "accumulator-after": 1,
        "accumulator-before": 1,
        "current": 0,
        "current-group": 0,
        "current-grouping-key": 0,
        "current-merge-group": (0, 1),
        "current-merge-key": 0,
        "document": (1, 2),
        "element-available": 1,
        "function-available": (1, 2),
        "key": (2, 3),
        "regex-group": 1,
        "system-property": 1,
        "type-available": 1,
        "unparsed-entity-uri": (1, 2),
    }
)

_FUNCTION_MINIMUM_VERSION: Mapping[str, str] = MappingProxyType(
    {
        "available-system-properties": "3.0",
        "accumulator-after": "3.0",
        "accumulator-before": "3.0",
        "current": "1.0",
        "current-group": "2.0",
        "current-grouping-key": "2.0",
        "current-merge-group": "3.0",
        "current-merge-key": "3.0",
        "document": "1.0",
        "element-available": "1.0",
        "function-available": "1.0",
        "regex-group": "2.0",
        "system-property": "1.0",
        "type-available": "2.0",
        "unparsed-entity-uri": "1.0",
    }
)

_PROFILE_VERSIONS = frozenset({"1.0", "2.0", "3.0", "3.1"})

#: Lexical names of functions that need the XSLT bridge rather than the
#: continuation machine's static host-function table.
XSLT_CONTEXT_FUNCTION_NAMES = frozenset(f"fn:{local}" for local in _FUNCTION_MINIMUM_VERSION)


def _profile_version(version: str) -> str:
    if not isinstance(version, str) or version not in _PROFILE_VERSIONS:
        raise ValueError(f"unsupported XSLT version {version!r}")
    return version


def _version_at_least(version: str, minimum: str) -> bool:
    return Decimal(version) >= Decimal(minimum)


def _declaration_arity(local: str, version: str) -> FunctionArity:
    if local == "document":
        return 1 if version == "1.0" else (1, 2)
    if local == "function-available":
        return 1 if version == "1.0" else (1, 2)
    if local == "unparsed-entity-uri":
        return (1, 2) if _version_at_least(version, "3.0") else 1
    return XSLT_FUNCTION_ARITIES[local]


def xslt_function_declarations(version: str) -> Mapping[str, FunctionArity]:
    """Return the XSLT function declarations legal for *version*.

    Keys are lexical QNames in the standard function namespace because every
    static context binds ``fn``.  Functions native to the XPath 3.1 parser
    (``key``, ``generate-id``, ``format-*``) are deliberately absent in the
    3.0 profile; in 1.0/2.0 profiles the XPath 2.0 parser has no native symbol,
    so the bridge declares them explicitly.
    """
    effective = _profile_version(version)
    declarations: dict[str, FunctionArity] = {}
    for local, minimum in _FUNCTION_MINIMUM_VERSION.items():
        if not _version_at_least(effective, minimum):
            continue
        declarations[f"fn:{local}"] = _declaration_arity(local, effective)
    if not _version_at_least(effective, "3.0"):
        # XSLT 1.0/2.0 require these functions but the XPath 2.0 parser has no
        # native symbol for them; the bridge supplies both implementations.
        declarations["fn:key"] = 2 if effective == "1.0" else (2, 3)
        declarations["fn:generate-id"] = (0, 1)
    return MappingProxyType(declarations)


# ---------------------------------------------------------------------------
# Name expansion and availability tables


def _expand_name(
    value: str,
    namespaces: Mapping[str, str],
    *,
    default_namespace: str | None,
    code: str,
    label: str,
) -> str:
    """Expand one EQName or lexical QName, reporting XSLT's dynamic error."""
    if not isinstance(value, str) or not value:
        raise XsltDynamicError(f"the {label} must be a non-empty EQName", code=code)
    if value.startswith("Q{"):
        close = value.find("}")
        if close < 0:
            raise XsltDynamicError(f"invalid EQName {value!r} for {label}", code=code)
        namespace_uri = value[2:close]
        local = value[close + 1 :]
        if not is_ncname(local):
            raise XsltDynamicError(f"invalid EQName {value!r} for {label}", code=code)
        return f"{{{namespace_uri}}}{local}" if namespace_uri else local
    prefix, separator, local = value.partition(":")
    if separator:
        if not is_ncname(prefix) or not is_ncname(local):
            raise XsltDynamicError(f"invalid QName {value!r} for {label}", code=code)
        uri = namespaces.get(prefix)
        if uri is None:
            raise XsltDynamicError(
                f"the namespace prefix {prefix!r} is not bound for {label}",
                code=code,
            )
        return f"{{{uri}}}{local}"
    if not is_ncname(value):
        raise XsltDynamicError(f"invalid QName {value!r} for {label}", code=code)
    return f"{{{default_namespace}}}{value}" if default_namespace else value


def _local_name(name: str) -> str:
    return name.partition("}")[2] if name.startswith("{") else name


def _namespace_of(name: str) -> str | None:
    return name[1:].partition("}")[0] if name.startswith("{") else None


def _arity_matches(spec: FunctionArity | None, arity: int | None) -> bool:
    if spec is None:
        return False
    if arity is None:
        return True
    if isinstance(spec, int):
        return arity == spec
    minimum, maximum = spec
    return minimum <= arity and (maximum is None or arity <= maximum)


@lru_cache(maxsize=2)
def _native_function_arities(version: str) -> Mapping[str, FunctionArity]:
    """One parser surface's statically known function signatures."""
    parser = (XPath2Parser if version == "2.0" else XPath31Parser)()
    table: dict[str, FunctionArity] = {}
    for symbol, token in parser.symbol_table.items():
        nargs = getattr(token, "nargs", None)
        if nargs is None:
            continue
        label = str(getattr(token, "label", ""))
        if "function" not in label:
            continue
        namespace = getattr(token, "namespace", None)
        if namespace is None:
            if label == "constructor function":
                continue
            namespace = FN_NS
        expanded = symbol if symbol.startswith("{") else f"{{{namespace}}}{symbol}"
        table[expanded] = nargs
    return MappingProxyType(table)


def xslt_function_arities(version: str) -> Mapping[str, FunctionArity]:
    """Expanded-name arity map for XSLT functions available in *version*."""
    effective = _profile_version(version)
    table = {
        f"{{{FN_NS}}}{name.partition(':')[2]}": arity
        for name, arity in xslt_function_declarations(effective).items()
    }
    if _version_at_least(effective, "3.0"):
        # Native XPath functions that XSLT defines additional semantics for.
        table[f"{{{FN_NS}}}key"] = (2, 3)
        table[f"{{{FN_NS}}}generate-id"] = (0, 1)
    return MappingProxyType(table)


@lru_cache(maxsize=4)
def _declared_arities(version: str) -> Mapping[str, FunctionArity]:
    return xslt_function_arities(version)


# ---------------------------------------------------------------------------
# The bridge


def _freeze_sequence(value: object, label: str) -> XdmSequence:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be an XDM sequence")
    if not all(isinstance(item, XdmItem) for item in value):
        raise TypeError(f"{label} must contain XDM items")
    return tuple(value)


def _freeze_merge_group(value: object) -> Mapping[str, XdmSequence]:
    if not isinstance(value, Mapping):
        raise TypeError("current_merge_group must be a mapping of source names")
    group: dict[str, XdmSequence] = {}
    for source, nodes in value.items():
        if not isinstance(source, str):
            raise TypeError("current_merge_group source names must be strings")
        group[source] = _freeze_sequence(nodes, "current_merge_group")
    return MappingProxyType(group)


def _freeze_strings(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence of strings")
    if not all(isinstance(item, str) for item in value):
        raise TypeError(f"{label} must contain strings")
    return tuple(value)


@dataclass(frozen=True)
class XsltContextBridge:
    """One evaluation's immutable view of the XSLT dynamic context."""

    transformation: Any
    package_name: str | None = None
    focus: Focus = field(default_factory=Focus.absent)
    current_item: XdmItem | None = None
    group: XdmSequence | _Absent = ABSENT
    grouping_key: XdmSequence | _Absent = ABSENT
    merge_group: Mapping[str, XdmSequence] | _Absent = ABSENT
    merge_sources: tuple[str, ...] = ()
    merge_key: XdmSequence | _Absent = ABSENT
    regex_captures: tuple[str, ...] | _Absent = ABSENT
    namespaces: Mapping[str, str] = field(default_factory=dict)
    default_namespace: str | None = None
    base_uri: str | None = None
    xpath_version: str = "3.1"
    xslt_version: str | None = None
    key_lookup: Callable[[XdmItem | None, tuple[XdmSequence, ...]], XdmSequence] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.focus, Focus):
            raise TypeError("focus must be a Focus")
        if self.current_item is not None and not isinstance(self.current_item, XdmItem):
            raise TypeError("current_item must be an XDM item or None")
        if not isinstance(self.group, _Absent):
            object.__setattr__(self, "group", _freeze_sequence(self.group, "current_group"))
        if not isinstance(self.grouping_key, _Absent):
            object.__setattr__(
                self,
                "grouping_key",
                _freeze_sequence(self.grouping_key, "current_grouping_key"),
            )
        if not isinstance(self.merge_group, _Absent):
            object.__setattr__(
                self,
                "merge_group",
                _freeze_merge_group(self.merge_group),
            )
        if not isinstance(self.merge_key, _Absent):
            object.__setattr__(
                self,
                "merge_key",
                _freeze_sequence(self.merge_key, "current_merge_key"),
            )
        if not isinstance(self.regex_captures, _Absent):
            object.__setattr__(
                self, "regex_captures", _freeze_strings(self.regex_captures, "regex_groups")
            )
        if not isinstance(self.namespaces, Mapping):
            raise TypeError("namespaces must be a mapping")
        namespaces: dict[str, str] = {}
        for prefix, uri in self.namespaces.items():
            if not isinstance(prefix, str) or not isinstance(uri, str):
                raise TypeError("namespaces must map strings to strings")
            namespaces[prefix] = uri
        object.__setattr__(self, "namespaces", MappingProxyType(namespaces))
        if self.merge_sources:
            object.__setattr__(
                self,
                "merge_sources",
                _freeze_strings(self.merge_sources, "current_merge_sources"),
            )
        elif not isinstance(self.merge_group, _Absent):
            object.__setattr__(
                self,
                "merge_sources",
                tuple(self.merge_group),
            )
        if self.base_uri is not None and not isinstance(self.base_uri, str):
            raise TypeError("base_uri must be a string or None")
        if self.package_name is not None and not isinstance(self.package_name, str):
            raise TypeError("package_name must be a string or None")
        if self.key_lookup is not None and not callable(self.key_lookup):
            raise TypeError("key_lookup must be callable or None")
        object.__setattr__(self, "xpath_version", self._effective_xpath_version())
        if self.xslt_version is None:
            object.__setattr__(
                self,
                "xslt_version",
                "2.0" if self.xpath_version == "2.0" else "3.0",
            )
        else:
            object.__setattr__(self, "xslt_version", _profile_version(self.xslt_version))

    def _effective_xpath_version(self) -> str:
        # 3.0 and 3.1 profiles share XPath 3.1's function surface here; the
        # native parser table still carries the exact version's signatures.
        if self.xpath_version not in ("2.0", "3.0", "3.1"):
            raise ValueError(f"unsupported XPath version {self.xpath_version!r}")
        return self.xpath_version

    @property
    def version(self) -> str:
        """The effective XSLT profile used for availability decisions."""
        assert self.xslt_version is not None
        return self.xslt_version

    # -- immutable state transitions ---------------------------------------

    def cleared_for_call(self) -> XsltContextBridge:
        """Clear every XSLT context component for a function invocation."""
        return replace(
            self,
            focus=Focus.absent(),
            current_item=None,
            group=ABSENT,
            grouping_key=ABSENT,
            merge_group=ABSENT,
            merge_sources=(),
            merge_key=ABSENT,
            regex_captures=ABSENT,
        )

    def with_group(
        self,
        group: XdmSequence,
        grouping_key: XdmSequence | _Absent = ABSENT,
    ) -> XsltContextBridge:
        return replace(self, group=group, grouping_key=grouping_key)

    def with_merge_group(
        self,
        group: Mapping[str, XdmSequence],
        key: XdmSequence | _Absent,
        *,
        sources: tuple[str, ...] = (),
    ) -> XsltContextBridge:
        return replace(
            self,
            merge_group=group,
            merge_sources=sources,
            merge_key=key,
        )

    def with_regex_groups(self, groups: tuple[str, ...]) -> XsltContextBridge:
        return replace(self, regex_captures=groups)

    # -- XSLT-defined functions --------------------------------------------

    def function_overrides(self) -> Mapping[str, ContextFunction]:
        """Function implementations legal for this evaluation's profile."""
        legal = {name.partition(":")[2] for name in xslt_function_declarations(self.version)}
        table: dict[str, ContextFunction] = {
            f"{{{FN_NS}}}current": ContextFunction(self._direct_current, self._dynamic_current),
            f"{{{FN_NS}}}document": ContextFunction(self._document_function),
            f"{{{FN_NS}}}system-property": ContextFunction(self._system_property_function),
            f"{{{FN_NS}}}function-available": ContextFunction(self._function_available_function),
            f"{{{FN_NS}}}element-available": ContextFunction(self._element_available_function),
            f"{{{FN_NS}}}unparsed-entity-uri": ContextFunction(self._unparsed_entity_uri_function),
            f"{{{FN_NS}}}generate-id": ContextFunction(self._generate_id_function),
        }
        if "type-available" in legal:
            table[f"{{{FN_NS}}}type-available"] = ContextFunction(self._type_available_function)
        if "available-system-properties" in legal:
            table[f"{{{FN_NS}}}available-system-properties"] = ContextFunction(
                self._available_system_properties_function
            )
        if "current-group" in legal:
            table[f"{{{FN_NS}}}current-group"] = ContextFunction(
                self._direct_current_group, self._dynamic_current_group
            )
        if "current-grouping-key" in legal:
            table[f"{{{FN_NS}}}current-grouping-key"] = ContextFunction(
                self._direct_current_grouping_key, self._dynamic_current_grouping_key
            )
        if "regex-group" in legal:
            table[f"{{{FN_NS}}}regex-group"] = ContextFunction(
                self._direct_regex_group, self._dynamic_regex_group
            )
        if "current-merge-group" in legal:
            table[f"{{{FN_NS}}}current-merge-group"] = ContextFunction(
                self._direct_current_merge_group, self._dynamic_current_merge_group
            )
        if "current-merge-key" in legal:
            table[f"{{{FN_NS}}}current-merge-key"] = ContextFunction(
                self._direct_current_merge_key, self._dynamic_current_merge_key
            )
        if "accumulator-before" in legal:
            table[f"{{{FN_NS}}}accumulator-before"] = ContextFunction(
                self._direct_accumulator_before
            )
        if "accumulator-after" in legal:
            table[f"{{{FN_NS}}}accumulator-after"] = ContextFunction(self._direct_accumulator_after)
        if self.key_lookup is not None:
            table[f"{{{FN_NS}}}key"] = ContextFunction(self._direct_key, self._dynamic_key)
        return MappingProxyType(table)

    def current(self) -> XdmItem:
        """The instruction-level current item, never a predicate focus item."""
        if self.current_item is None:
            raise XsltDynamicError(
                "current() requires a context item for the containing expression",
                code="XTDE1360",
            )
        return self.current_item

    def current_group(self) -> XdmSequence:
        if isinstance(self.group, _Absent):
            raise XsltDynamicError(
                "current-group() is not inside an xsl:for-each-group instruction",
                code="XTDE1061",
            )
        return tuple(self.group)

    def current_grouping_key(self) -> XdmSequence:
        if isinstance(self.grouping_key, _Absent):
            raise XsltDynamicError(
                "current-grouping-key() is not inside a grouped xsl:for-each-group",
                code="XTDE1071",
            )
        return tuple(self.grouping_key)

    def current_merge_group(self, source: str | None = None) -> XdmSequence:
        if isinstance(self.merge_group, _Absent):
            raise XsltDynamicError(
                "current-merge-group() is not inside an xsl:merge instruction",
                code="XTDE3480",
            )
        if source is None:
            return tuple(
                item for name in self.merge_sources for item in self.merge_group.get(name, ())
            )
        if source not in self.merge_group:
            raise XsltDynamicError(
                f"the merge source {source!r} is not part of the current merge group",
                code="XTDE3490",
            )
        return tuple(self.merge_group[source])

    def current_merge_key(self) -> XdmSequence:
        if isinstance(self.merge_key, _Absent):
            raise XsltDynamicError(
                "current-merge-key() is not inside an xsl:merge instruction",
                code="XTDE3510",
            )
        return tuple(self.merge_key)

    def regex_group(self, number: int) -> str:
        if (
            isinstance(self.regex_captures, _Absent)
            or number < 0
            or number >= len(self.regex_captures)
        ):
            return ""
        return self.regex_captures[number]

    def generate_id(self, item: XdmItem) -> str:
        return self.transformation.generated_id(item)

    def system_property(self, name: str) -> str:
        expanded = _expand_name(
            name,
            self.namespaces,
            default_namespace=None,
            code="XTDE1390",
            label="system property name",
        )
        if _namespace_of(expanded) != XSLT_NS:
            return ""
        local = _local_name(expanded)
        if local == "supports-dynamic-evaluation":
            return "yes" if self._dynamic_evaluation_enabled() else "no"
        return SYSTEM_PROPERTIES.get(f"xsl:{local}", "")

    def _dynamic_evaluation_enabled(self) -> bool:
        stylesheet = getattr(self.transformation, "stylesheet", None)
        processor = getattr(stylesheet, "processor", None)
        options = getattr(processor, "static_options", {})
        if not isinstance(options, Mapping):
            return True
        value = options.get("dynamic-evaluation", True)
        if not isinstance(value, bool):
            raise XsltDynamicError(
                "the dynamic-evaluation static option must be a boolean",
                code="XTDE0030",
            )
        return value

    def function_available(self, name: str, arity: int | None = None) -> bool:
        expanded = _expand_name(
            name,
            self.namespaces,
            default_namespace=FN_NS,
            code="XTDE1400",
            label="function name",
        )
        namespace = _namespace_of(expanded)
        local = _local_name(expanded)
        if namespace == XSD_NS:
            return local in builtin_type_classes() and arity in (None, 1)
        declared = _declared_arities(self.version).get(expanded)
        if declared is None:
            # Availability remains keyed to the declared profile surface.  A
            # 3.0 processor still evaluates these expressions with XPath 3.1,
            # but the compatibility profile keeps its historical availability
            # answer (for example, no random-number-generator under 2.0).
            native_version = "3.1" if _version_at_least(self.version, "3.0") else "2.0"
            declared = _native_function_arities(native_version).get(expanded)
        if _arity_matches(declared, arity):
            return True
        package = self._package()
        if package is not None:
            for component in package.components.values():
                if (
                    component.kind == "function"
                    and component.name == expanded
                    and not component.hidden
                    and not component.abstract
                    and (arity is None or component.arity == arity)
                ):
                    return True
        return False

    def element_available(self, name: str) -> bool:
        expanded = _expand_name(
            name,
            self.namespaces,
            default_namespace=self.default_namespace,
            code="XTDE1440",
            label="element name",
        )
        if _namespace_of(expanded) != XSLT_NS:
            return False
        local = _local_name(expanded)
        if local == "evaluate" and not self._dynamic_evaluation_enabled():
            return False
        return xslt_element_available(local, self.version)

    def type_available(self, name: str) -> bool:
        expanded = _expand_name(
            name,
            self.namespaces,
            default_namespace=self.default_namespace,
            code="XTDE1428",
            label="type name",
        )
        namespace = _namespace_of(expanded)
        local = _local_name(expanded)
        pool = self._schema_pool()
        if pool is not None and pool.get_type(expanded) is not None:
            return True
        if namespace == XSD_NS:
            return local in builtin_type_classes()
        return False

    def available_system_properties(self) -> XdmSequence:
        return tuple(as_xdm(QName(XSLT_NS, name))[0] for name in SYSTEM_PROPERTIES)

    def document(
        self,
        uri_sequence: XdmSequence | str,
        node: XdmNode | None = None,
    ) -> XdmSequence:
        """Resolve URI references using the XSLT document() argument rules."""
        if isinstance(uri_sequence, str):
            uri_sequence = (XdmAtomic.string(uri_sequence),)
        base_node_uri = None if node is None else node.base_uri
        resolved: dict[int, XdmDocument] = {}
        for item in uri_sequence:
            if isinstance(item, XdmNode):
                base = base_node_uri if node is not None else item.base_uri
                for atomic in item.typed_value:
                    if not isinstance(atomic, XdmAtomic):  # pragma: no cover - XDM invariant
                        raise XsltDynamicError(
                            "document() node atomization must yield atomic values",
                            code="XPTY0004",
                        )
                    document = self._resolve_document_or_recover(_document_uri(atomic), base)
                    if document is not None:
                        resolved[id(document)] = document
                continue
            if not isinstance(item, XdmAtomic):
                raise XsltDynamicError(
                    "document() requires atomic values or nodes",
                    code="XPTY0004",
                )
            base = base_node_uri if node is not None else self.base_uri
            document = self._resolve_document_or_recover(_document_uri(item), base)
            if document is not None:
                resolved[id(document)] = document
        return tuple(sorted(resolved.values(), key=lambda document: document.document_order))

    def _resolve_document_or_recover(self, uri: str, base: str | None) -> XdmDocument | None:
        """Resolve one URI, applying only an explicitly selected recovery rule."""
        try:
            return self._resolve_document(uri, base)
        except XsltDynamicError as exc:
            recover = getattr(self.transformation, "recover", None)
            if not callable(recover) or exc.code is None or not recover(exc, exc.code):
                raise
            if str(exc.code).rsplit(":", 1)[-1] == "XTDE1160":
                return self._resolve_document(uri.split("#", 1)[0], base)
            return None

    def _resolve_document(self, uri: str, base: str | None) -> XdmDocument:
        if "#" in uri:
            raise XsltDynamicError(
                f"document fragment identifiers are not resolved for {uri!r}",
                code="XTDE1160",
            )
        if urlsplit(uri).scheme:
            target = uri
        elif base is None:
            raise XsltDynamicError(
                f"the relative document URI {uri!r} has no base URI",
                code="XTDE1162",
            )
        else:
            target = urljoin(base, uri)
        documents = getattr(self.transformation, "documents", {})
        document = documents.get(target)
        if isinstance(document, XdmDocument):
            return document
        module_document = self._stylesheet_document(target)
        if module_document is not None:
            return module_document
        raise XsltDynamicError(
            f"the document {uri!r} is not available to this transformation",
            code="FODC0002",
        )

    def unparsed_entity_uri(self, name: str, node: XdmItem | None = None) -> str:
        del name
        if node is None:
            raise XsltDynamicError(
                "unparsed-entity-uri requires a context node",
                code="XPDY0002",
            )
        if not isinstance(node, XdmNode):
            raise XsltDynamicError(
                "unparsed-entity-uri requires a node",
                code="XPTY0004",
            )
        if not isinstance(node.document, XdmDocument):  # pragma: no cover - XDM invariant
            raise XsltDynamicError(
                "unparsed-entity-uri requires a node in a document",
                code="XTDE1370",
            )
        # The XDM data model exposes no unparsed entities, so every name maps to
        # the zero-length xs:anyURI rather than an invented system identifier.
        return ""

    def accumulator_before(self, name: str) -> XdmSequence:
        self._accumulator(name)
        raise XsltDynamicError(  # pragma: no cover - Task 25 accumulator runtime
            "accumulator evaluation is not available in this build",
            code=None,
        )

    def accumulator_after(self, name: str) -> XdmSequence:
        self._accumulator(name)
        raise XsltDynamicError(  # pragma: no cover - Task 25 accumulator runtime
            "accumulator evaluation is not available in this build",
            code=None,
        )

    # -- package and resource helpers --------------------------------------

    def _package(self) -> Any:
        stylesheet = getattr(self.transformation, "stylesheet", None)
        packages = getattr(stylesheet, "_packages", {})
        return packages.get(self.package_name) or getattr(stylesheet, "package", None)

    def _schema_pool(self) -> Any:
        return getattr(getattr(self.transformation, "stylesheet", None), "schema_pool", None)

    def _stylesheet_document(self, uri: str) -> XdmDocument | None:
        package = self._package()
        modules = () if package is None else package.modules
        for module in modules:
            if module.uri == uri or module.raw.base_uri == uri:
                return module.document
        return None

    def _accumulator(self, name: str) -> None:
        package = self._package()
        component = None if package is None else package.accumulator(name)
        if component is None:
            raise XsltDynamicError(
                f"the accumulator {name!r} is not declared in this package",
                code="XTDE3340",
            )

    # -- callback adapters --------------------------------------------------

    def _direct_current(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item, arguments
        return (self.current(),)

    def _dynamic_current(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item, arguments
        raise XsltDynamicError(
            "current() called as a function item has no current item",
            code="XTDE1360",
        )

    def _direct_current_group(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item, arguments
        return self.current_group()

    def _dynamic_current_group(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item, arguments
        raise XsltDynamicError(
            "current-group() called as a function item has no current group",
            code="XTDE1061",
        )

    def _direct_current_grouping_key(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item, arguments
        return self.current_grouping_key()

    def _dynamic_current_grouping_key(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item, arguments
        raise XsltDynamicError(
            "current-grouping-key() called as a function item has no current group",
            code="XTDE1071",
        )

    def _direct_current_merge_group(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item
        source = _single_string(arguments, 0) if arguments else None
        return self.current_merge_group(source)

    def _dynamic_current_merge_group(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item, arguments
        raise XsltDynamicError(
            "current-merge-group() called as a function item has no merge group",
            code="XTDE3480",
        )

    def _direct_current_merge_key(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item, arguments
        return self.current_merge_key()

    def _dynamic_current_merge_key(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item, arguments
        raise XsltDynamicError(
            "current-merge-key() called as a function item has no merge key",
            code="XTDE3510",
        )

    def _direct_regex_group(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item
        number = _single_integer(arguments, 0)
        return (XdmAtomic.string(self.regex_group(number)),)

    def _dynamic_regex_group(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item
        _single_integer(arguments, 0)
        return (XdmAtomic.string(""),)

    def _document_function(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item
        uri_sequence = arguments[0] if arguments else ()
        node: XdmNode | None = None
        if len(arguments) > 1:
            node = _single_node(arguments[1], "document() second argument")
        return self.document(uri_sequence, node)

    def _system_property_function(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item
        return (XdmAtomic.string(self.system_property(_single_string(arguments, 0))),)

    def _function_available_function(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item
        name = _single_string(arguments, 0)
        arity = _single_integer(arguments, 1) if len(arguments) > 1 else None
        return (XdmAtomic.boolean(self.function_available(name, arity)),)

    def _element_available_function(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item
        return (XdmAtomic.boolean(self.element_available(_single_string(arguments, 0))),)

    def _type_available_function(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item
        return (XdmAtomic.boolean(self.type_available(_single_string(arguments, 0))),)

    def _generate_id_function(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        if not arguments:
            item = context_item
            if item is None:
                raise XsltDynamicError(
                    "generate-id() requires a context node",
                    code="XPDY0002",
                )
        else:
            value = arguments[0]
            if len(value) > 1:
                raise XsltDynamicError(
                    "generate-id requires at most one node",
                    code="XPTY0004",
                )
            if not value:
                return (XdmAtomic.string(""),)
            item = value[0]
        if not isinstance(item, XdmNode):
            raise XsltDynamicError(
                "generate-id requires a node",
                code="XPTY0004",
            )
        return (XdmAtomic.string(self.generate_id(item)),)

    def _available_system_properties_function(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item, arguments
        return self.available_system_properties()

    def _unparsed_entity_uri_function(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        name = _single_string(arguments, 0)
        target: XdmItem | None = context_item
        if len(arguments) > 1:
            target = _single_node(arguments[1], "unparsed-entity-uri() second argument")
        return (XdmAtomic.any_uri(self.unparsed_entity_uri(name, target)),)

    def _direct_accumulator_before(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item
        return self.accumulator_before(_single_string(arguments, 0))

    def _direct_accumulator_after(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        del context_item
        return self.accumulator_after(_single_string(arguments, 0))

    def _direct_key(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        assert self.key_lookup is not None
        return self.key_lookup(context_item, arguments)

    def _dynamic_key(
        self,
        context_item: XdmItem | None,
        arguments: tuple[XdmSequence, ...],
    ) -> XdmSequence:
        assert self.key_lookup is not None
        del context_item
        return self.key_lookup(None, arguments)


def _document_uri(item: XdmAtomic) -> str:
    local = item.type_name.partition("}")[2] if item.type_name.startswith("{") else item.type_name
    if local not in ("string", "anyURI", "untypedAtomic"):
        raise XsltDynamicError(
            "document() argument items must be strings, anyURIs, or nodes",
            code="XPTY0004",
        )
    return item.lexical


def _single_node(sequence: XdmSequence, label: str) -> XdmNode:
    if len(sequence) != 1 or not isinstance(sequence[0], XdmNode):
        raise XsltDynamicError(
            f"{label} must be a single node",
            code="XPTY0004",
        )
    return sequence[0]


def _single_string(arguments: tuple[XdmSequence, ...], index: int) -> str:
    if index >= len(arguments) or len(arguments[index]) != 1:
        raise XsltDynamicError(
            "the function argument must be a single atomic value",
            code="XPTY0004",
        )
    item = arguments[index][0]
    if not isinstance(item, XdmAtomic):
        raise XsltDynamicError(
            "the function argument must be an atomic value",
            code="XPTY0004",
        )
    return item.lexical


def _single_integer(arguments: tuple[XdmSequence, ...], index: int) -> int:
    lexical = _single_string(arguments, index).strip()
    try:
        return int(lexical)
    except ValueError as exc:
        raise XsltDynamicError(
            "the function argument must be an integer",
            code="XPTY0004",
        ) from exc
