"""Immutable static and dynamic configuration for standalone XPath evaluation.

The environment in this module is deliberately independent of the XSLT runtime.
It owns only the values a compiled XPath expression needs: namespaces and
variable/function declarations for compilation, plus invocation-local
variables, functions, key indexes, documents, collections, text resources, and
stable dynamic values for evaluation.  No live transformation, stylesheet,
initial template, result builder, or mode is required.

Mappings are copied into read-only views at construction.  Dynamic values that
must be stable for one invocation/environment, such as the current date/time
and implicit timezone, are fixed here rather than recomputed by each
evaluation.  Resources are available only when explicitly supplied: there is no
implicit filesystem or network fallback.
"""

from __future__ import annotations

import datetime as _datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeAlias

from elementpath.datatypes.datetime import Timezone

from pyxsd.xdm.nodes import XdmDocument, XdmNode
from pyxsd.xdm.values import XdmFunction, XdmItem

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xdm.schema import SchemaPool

__all__ = [
    "ExpressionEnvironment",
    "Focus",
    "FocusKind",
    "FunctionArity",
    "XPathStaticContext",
]

FunctionArity: TypeAlias = int | tuple[int, int | None]


def _freeze_mapping(mapping: Mapping[Any, Any], label: str) -> Mapping[Any, Any]:
    if not isinstance(mapping, Mapping):
        raise TypeError(f"{label} must be a mapping")
    return MappingProxyType(dict(mapping))


def _require_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} names must be non-empty strings")
    return value


def _normalise_arity(value: object, name: str) -> int | tuple[int, int | None]:
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ValueError(f"function arity for {name!r} must be non-negative")
        return value
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], int)
        and not isinstance(value[0], bool)
        and (value[1] is None or (isinstance(value[1], int) and not isinstance(value[1], bool)))
    ):
        minimum, maximum = value
        if minimum < 0 or (maximum is not None and maximum < minimum):
            raise ValueError(f"function arity for {name!r} is invalid")
        return value
    raise ValueError(f"function arity for {name!r} must be an integer or a (min, max) pair")


def _timezone_from_value(value: object) -> Timezone:
    if isinstance(value, Timezone):
        return value
    if isinstance(value, _datetime.timedelta):
        try:
            return Timezone(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid implicit timezone: {value!r}") from exc
    if isinstance(value, _datetime.tzinfo):
        offset = value.utcoffset(None)
        if offset is None:
            raise ValueError(f"invalid implicit timezone: {value!r}")
        try:
            return Timezone(offset)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid implicit timezone: {value!r}") from exc
    if isinstance(value, str):
        try:
            return Timezone.fromstring(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid implicit timezone: {value!r}") from exc
    raise ValueError(f"invalid implicit timezone: {value!r}")


def _local_timezone() -> Timezone:
    return _timezone_from_value(_datetime.datetime.now().astimezone().tzinfo)


@dataclass(frozen=True)
class XPathStaticContext:
    """Immutable parser configuration shared by compiled expressions.

    ``functions`` declares host functions that the parser may resolve at
    compile time.  Keys are prefixed lexical QNames (``"m:f"``) or Clark names
    whose namespace has a prefix in ``namespaces``.  The declaration carries
    arity only; the callable is supplied by each :class:`ExpressionEnvironment`.
    """

    namespaces: Mapping[str, str] = field(default_factory=dict)
    default_namespace: str | None = None
    variable_types: Mapping[str, str] = field(default_factory=dict)
    functions: Mapping[str, FunctionArity] = field(default_factory=dict)
    schema: SchemaPool | None = None
    base_uri: str | None = None
    default_collation: str | None = None
    xsd_version: str | None = None

    def __post_init__(self) -> None:
        namespaces = _freeze_mapping(self.namespaces, "namespaces")
        for prefix, uri in namespaces.items():
            if not isinstance(prefix, str) or not isinstance(uri, str):
                raise TypeError("namespace prefixes and URIs must be strings")
        variable_types = _freeze_mapping(self.variable_types, "variable types")
        for name, type_name in variable_types.items():
            _require_name(name, "variable")
            if not isinstance(type_name, str) or not type_name:
                raise TypeError("variable types must be non-empty strings")

        functions: dict[str, FunctionArity] = {}
        for name, arity in self.functions.items():
            _require_name(name, "function")
            if not name.startswith("{") and ":" not in name:
                raise ValueError(f"function {name!r} requires a namespace prefix or a Clark name")
            functions[name] = _normalise_arity(arity, name)
        if self.default_namespace is not None and not isinstance(self.default_namespace, str):
            raise TypeError("default_namespace must be a string or None")
        if self.base_uri is not None and not isinstance(self.base_uri, str):
            raise TypeError("base_uri must be a string or None")
        if self.default_collation is not None and not isinstance(self.default_collation, str):
            raise TypeError("default_collation must be a string or None")
        if self.xsd_version not in (None, "1.0", "1.1"):
            raise ValueError("xsd_version must be '1.0', '1.1', or None")

        object.__setattr__(self, "namespaces", namespaces)
        object.__setattr__(self, "variable_types", variable_types)
        object.__setattr__(self, "functions", MappingProxyType(functions))

    def function_arity(self, name: str) -> FunctionArity | None:
        """Return the declared arity for *name*, trying Clark/prefix forms."""
        direct = self.functions.get(name)
        if direct is not None:
            return direct
        if not name.startswith("{"):
            expanded = self.expand_name(name)
            if expanded is not None:
                return self.functions.get(expanded)
        else:
            lexical = self.lexical_name(name)
            if lexical is not None:
                return self.functions.get(lexical)
        return None

    def expand_name(self, name: str) -> str | None:
        """Expand a prefixed lexical QName with this context's namespaces."""
        prefix, separator, local = name.partition(":")
        if not separator:
            return None
        uri = self.namespaces.get(prefix)
        return None if uri is None else f"{{{uri}}}{local}"

    def lexical_name(self, name: str) -> str | None:
        """Return a prefixed lexical form for a Clark name, if a prefix exists."""
        if not name.startswith("{"):
            return name
        namespace, _, local = name[1:].partition("}")
        for prefix, uri in self.namespaces.items():
            if uri == namespace:
                return f"{prefix}:{local}" if prefix else local
        return None


class FocusKind(StrEnum):
    """Whether a focus is absent, empty, or carries one context item."""

    ABSENT = "absent"
    EMPTY = "empty"
    ITEM = "item"


@dataclass(frozen=True)
class Focus:
    """An explicit XPath focus with the absent/empty distinction preserved.

    ``Focus.absent()`` means no context item exists; an expression that needs
    one raises ``XPDY0002``.  ``Focus.empty()`` is an explicitly empty focus and
    evaluates to the empty sequence without entering the expression.  The two
    are never converted into one another.
    """

    kind: FocusKind = FocusKind.ITEM
    item: XdmItem | None = None
    position: int = 1
    size: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FocusKind):
            raise TypeError("kind must be a FocusKind")
        if self.kind is FocusKind.ITEM:
            if not isinstance(self.item, XdmItem):
                raise TypeError("an item focus requires an XdmItem")
        elif self.item is not None:
            raise ValueError("an absent or empty focus has no context item")
        if self.kind is FocusKind.EMPTY and (self.position, self.size) != (0, 0):
            raise ValueError("an empty focus has position and size zero")
        if self.kind is not FocusKind.EMPTY and (self.position < 1 or self.size < self.position):
            raise ValueError("focus position/size are invalid")

    @classmethod
    def absent(cls) -> Focus:
        return cls(FocusKind.ABSENT)

    @classmethod
    def empty(cls) -> Focus:
        return cls(FocusKind.EMPTY, position=0, size=0)

    @classmethod
    def singleton(cls, item: XdmItem, *, position: int = 1, size: int = 1) -> Focus:
        return cls(FocusKind.ITEM, item=item, position=position, size=size)

    @property
    def is_absent(self) -> bool:
        return self.kind is FocusKind.ABSENT

    @property
    def is_empty(self) -> bool:
        return self.kind is FocusKind.EMPTY


@dataclass(frozen=True)
class ExpressionEnvironment:
    """One invocation's dynamic XPath values.

    Construction copies every mapping.  ``current_datetime`` and the implicit
    timezone are fixed here, so repeated evaluations through the same
    environment observe stable dynamic values.  Resources are exactly the
    explicitly supplied documents/collections/text resources; absence is a
    resource denial, never a silent filesystem or network read.
    """

    variables: Mapping[str, Any] = field(default_factory=dict)
    functions: Mapping[str, Any] = field(default_factory=dict)
    function_overrides: Mapping[str, Any] = field(default_factory=dict)
    key_indexes: Mapping[str, Mapping[Any, Sequence[XdmNode]]] = field(default_factory=dict)
    documents: Mapping[str, XdmDocument] = field(default_factory=dict)
    collections: Mapping[str, Sequence[XdmNode]] = field(default_factory=dict)
    default_collection: Sequence[XdmNode] = ()
    text_resources: Mapping[str, str] = field(default_factory=dict)
    decimal_formats: Mapping[str | None, Mapping[str, str]] = field(default_factory=dict)
    current_datetime: _datetime.datetime | None = None
    implicit_timezone: Timezone | str | _datetime.tzinfo | _datetime.timedelta | None = None
    base_uri: str | None = None

    def __post_init__(self) -> None:
        variables = dict(_freeze_mapping(self.variables, "variables"))
        for name in variables:
            _require_name(name, "variable")

        functions = dict(_freeze_mapping(self.functions, "functions"))
        for name, function in functions.items():
            _require_name(name, "function")
            if not callable(function) and not isinstance(function, XdmFunction):
                raise TypeError(f"function {name!r} is not callable")

        overrides = dict(_freeze_mapping(self.function_overrides, "function overrides"))
        for name, function in overrides.items():
            _require_name(name, "function override")
            if not callable(function):
                raise TypeError(f"function override {name!r} is not callable")

        key_indexes: dict[str, Mapping[Any, Sequence[XdmNode]]] = {}
        for name, index in self.key_indexes.items():
            _require_name(name, "key index")
            if not isinstance(index, Mapping):
                raise TypeError(f"key index {name!r} must be a mapping")
            normalised: dict[Any, Sequence[XdmNode]] = {}
            for value, nodes in index.items():
                if isinstance(nodes, (str, bytes)) or not isinstance(nodes, Sequence):
                    raise TypeError(f"key index {name!r} values must be node sequences")
                if not all(isinstance(node, XdmNode) for node in nodes):
                    raise TypeError(f"key index {name!r} values must contain XdmNode items")
                normalised[value] = tuple(nodes)
            key_indexes[name] = MappingProxyType(normalised)

        documents: dict[str, XdmDocument] = {}
        for uri, document in self.documents.items():
            _require_name(uri, "document")
            if not isinstance(document, XdmDocument):
                raise TypeError(f"document {uri!r} must be an XdmDocument")
            documents[uri] = document

        collections: dict[str, Sequence[XdmNode]] = {}
        for uri, nodes in self.collections.items():
            _require_name(uri, "collection")
            if isinstance(nodes, (str, bytes)) or not isinstance(nodes, Sequence):
                raise TypeError(f"collection {uri!r} must be a node sequence")
            if not all(isinstance(node, XdmNode) for node in nodes):
                raise TypeError(f"collection {uri!r} must contain XdmNode items")
            collections[uri] = tuple(nodes)

        default_collection = tuple(self.default_collection)
        if not all(isinstance(node, XdmNode) for node in default_collection):
            raise TypeError("default_collection must contain XdmNode items")

        text_resources = dict(_freeze_mapping(self.text_resources, "text resources"))
        for uri, text in text_resources.items():
            _require_name(uri, "text resource")
            if not isinstance(text, str):
                raise TypeError(f"text resource {uri!r} must be a string")

        decimal_formats: dict[str | None, Mapping[str, str]] = {}
        for name, attributes in self.decimal_formats.items():
            if name is not None and (not isinstance(name, str) or not name):
                raise TypeError("decimal format names must be non-empty strings or None")
            if not isinstance(attributes, Mapping):
                raise TypeError(f"decimal format {name!r} must be a mapping")
            values: dict[str, str] = {}
            for attribute, value in attributes.items():
                if not isinstance(attribute, str) or not isinstance(value, str):
                    raise TypeError(
                        f"decimal format {name!r} attributes must map strings to strings"
                    )
                values[attribute] = value
            decimal_formats[name] = MappingProxyType(values)

        if self.current_datetime is not None and not isinstance(
            self.current_datetime, _datetime.datetime
        ):
            raise TypeError("current_datetime must be a datetime or None")
        if self.implicit_timezone is None:
            timezone = (
                _timezone_from_value(self.current_datetime.utcoffset())
                if self.current_datetime is not None
                and self.current_datetime.utcoffset() is not None
                else _local_timezone()
            )
        else:
            timezone = _timezone_from_value(self.implicit_timezone)
        current = self.current_datetime
        if current is None:
            current = _datetime.datetime.now(tz=timezone)
        elif current.tzinfo is None:
            current = current.replace(tzinfo=timezone)

        if self.base_uri is not None and not isinstance(self.base_uri, str):
            raise TypeError("base_uri must be a string or None")

        object.__setattr__(self, "variables", MappingProxyType(variables))
        object.__setattr__(self, "functions", MappingProxyType(functions))
        object.__setattr__(self, "function_overrides", MappingProxyType(overrides))
        object.__setattr__(self, "key_indexes", MappingProxyType(key_indexes))
        object.__setattr__(self, "documents", MappingProxyType(documents))
        object.__setattr__(self, "collections", MappingProxyType(collections))
        object.__setattr__(self, "default_collection", default_collection)
        object.__setattr__(self, "text_resources", MappingProxyType(text_resources))
        object.__setattr__(self, "decimal_formats", MappingProxyType(decimal_formats))
        object.__setattr__(self, "current_datetime", current)
        object.__setattr__(self, "implicit_timezone", timezone)

    @property
    def current_date_time(self) -> _datetime.datetime:
        """Alias matching the XPath function's spelling."""
        assert self.current_datetime is not None
        return self.current_datetime

    @property
    def timezone(self) -> Timezone:
        """The normalized implicit timezone."""
        assert isinstance(self.implicit_timezone, Timezone)
        return self.implicit_timezone
