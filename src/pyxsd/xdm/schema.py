"""Immutable schema pool and elementpath adapters over pyxsd declarations.

A compiled package's :class:`SchemaPool` owns the schemas imported by a
stylesheet: their target namespaces, global types/elements/attributes,
substitution groups, and atomic-type constructors.  It is a composition of
already-compiled :class:`pyxsd.schema.Schema` objects, not a second schema
model; no ``xmlschema`` object, generated Python source, network access, or
streaming is involved.

:class:`PyxsdSchemaProxy` adapts pyxsd's element/type representatives to
elementpath's schema protocols.  The adapters expose the facts XPath static
analysis needs (type variety, primitive root type, list item type, union
members, derivation, attributes, and model groups) while constructors and
values stay pyxsd objects.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, cast

from pyxsd.namespaces import XML_NS, XSD_NS, XSI_NS, clark, local_name, namespace_of
from pyxsd.xdm.annotations import id_kind_for, schema_type_name
from pyxsd.xdm.values import XdmAtomic, XdmFunction, XdmSequence, as_xdm

__all__ = [
    "AtomicConstructor",
    "PyxsdAttribute",
    "PyxsdAttributeGroup",
    "PyxsdElement",
    "PyxsdGroup",
    "PyxsdSchemaProxy",
    "PyxsdType",
    "SchemaPool",
]

_XSD_VERSION_NAMES = frozenset({"1.0", "1.1"})
_INJECTED_NAMESPACES = frozenset({XML_NS, XSI_NS, "http://www.w3.org/1999/xlink"})
_BUILTIN_CLASSES: dict[str, type[Any]] | None = None
_ELEMENT_KINDS = frozenset({"Element"})
_TYPE_KINDS = frozenset({"SimpleType", "ComplexType"})
_ATTRIBUTE_KINDS = frozenset({"Attribute"})


@dataclass(frozen=True, slots=True)
class AtomicConstructor:
    """A one-argument constructor for an atomic schema type.

    ``arity`` is one; :meth:`invoke` follows the XDM function-item call
    convention (an iterable of argument sequences) and atomizes through the
    same explicit seam as the value layer.
    """

    name: str
    type: PyxsdType

    @property
    def arity(self) -> int:
        return 1

    def __call__(self, value: Any) -> Any:
        return self.type.cast(value)

    def cast(self, value: Any) -> Any:
        return self.type.cast(value)

    def invoke(self, arguments: Iterable[Any], environment: Any = None) -> XdmSequence:
        """Invoke with XDM argument sequences."""
        del environment
        if isinstance(arguments, (str, bytes, bytearray, memoryview)) or not isinstance(
            arguments, Iterable
        ):
            raise TypeError("constructor arguments must be an iterable of XDM sequences")
        values = tuple(as_xdm(argument) for argument in arguments)
        if len(values) != 1:
            raise TypeError(f"constructor {self.name} expects exactly one argument")
        item = values[0]
        if len(item) != 1 or not isinstance(item[0], XdmAtomic):
            raise TypeError("an atomic constructor requires one atomic value")
        return as_xdm(self.cast(item[0].lexical))

    def as_function(self) -> XdmFunction:
        """Return this constructor as an XDM function item."""
        return XdmFunction(
            lambda arguments, environment, closure: self.invoke(arguments),
            arity=1,
            name=self.name,
        )


class PyxsdType:
    """One pyxsd generated or built-in XSD type under elementpath protocols."""

    __slots__ = (
        "_attributes",
        "_er",
        "_is_builtin",
        "_model_group",
        "_name",
        "_pool",
        "_proxy",
        "_type_class",
    )

    def __init__(
        self,
        pool: SchemaPool,
        type_class: Any,
        *,
        er: Any = None,
        proxy: PyxsdSchemaProxy | None = None,
        name: str | None = None,
        is_builtin: bool = False,
    ) -> None:
        self._pool = pool
        self._proxy = proxy
        self._type_class = type_class
        self._er = er
        self._name = schema_type_name(type_class) if name is None else name
        self._is_builtin = is_builtin
        self._attributes: PyxsdAttributeGroup | None = None
        self._model_group: PyxsdGroup | None = None

    @property
    def schema(self) -> PyxsdSchemaProxy | None:
        return self._proxy

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def local_name(self) -> str | None:
        return None if self._name is None else local_name(self._name)

    @property
    def is_builtin(self) -> bool:
        return self._is_builtin

    @property
    def parent(self) -> PyxsdSchemaProxy | None:
        return self._proxy

    @property
    def ref(self) -> None:
        return None

    @property
    def xsd_version(self) -> str:
        return self._pool.xsd_version

    @property
    def maps(self) -> SchemaPool:
        return self._pool

    def is_matching(self, name: str | None, default_namespace: str | None = None) -> bool:
        if name is None or self._name is None:
            return False
        if name == self._name:
            return True
        if name.startswith("{"):
            return False
        if ":" in name:
            expanded = self._proxy.expand_name(name) if self._proxy is not None else None
            return expanded == self._name
        if name != local_name(self._name):
            return False
        namespace = namespace_of(self._name)
        return default_namespace is None or default_namespace == namespace

    # -- variety and content ---------------------------------------------------

    @property
    def _variety(self) -> str:
        type_class = self._type_class
        from pyxsd import xsd_data_types as xsd

        if type_class is xsd.AnyType:
            return "complex"
        if getattr(type_class, "_unionMembers", None):
            return "union"
        class_names = {base.__name__ for base in getattr(type_class, "__mro__", ())}
        if (
            "XsdList" in class_names
            or "TypeList" in class_names
            or getattr(type_class, "__name__", None) in {"IDREFS", "ENTITIES", "NMTOKENS"}
        ):
            return "list"
        if getattr(type_class, "_contentKind_", None) == "complex":
            return "complex"
        return "atomic"

    def is_simple(self) -> bool:
        return self._variety != "complex"

    def _simple_content_class(self) -> Any:
        content = getattr(self._type_class, "_simpleContentType_", None)
        return content if isinstance(content, type) else None

    def is_empty(self) -> bool:
        if self.is_simple():
            return True
        if self._simple_content_class() is not None:
            return False
        model = getattr(self._type_class, "_contentModel_", None)
        if model is None:
            return bool(getattr(self._type_class, "_elementOnly_", False))
        return not getattr(model, "children", ())

    def has_simple_content(self) -> bool:
        if self.is_simple():
            return True
        return self._simple_content_class() is not None

    def has_mixed_content(self) -> bool:
        return (
            not self.is_simple()
            and not getattr(self._type_class, "_elementOnly_", False)
            and self._simple_content_class() is None
        )

    def is_element_only(self) -> bool:
        return (
            not self.is_simple()
            and bool(getattr(self._type_class, "_elementOnly_", False))
            and self._simple_content_class() is None
        )

    def is_atomic(self) -> bool:
        return self._variety == "atomic"

    def is_list(self) -> bool:
        return self._variety == "list"

    def is_union(self) -> bool:
        return self._variety == "union"

    def is_key(self) -> bool:
        return id_kind_for(self._type_class) == "ID"

    def is_qname(self) -> bool:
        return self._has_builtin_base("QName")

    def is_notation(self) -> bool:
        return self._has_builtin_base("NOTATION")

    def _has_builtin_base(self, name: str) -> bool:
        return any(
            base.__dict__.get("name") == name or base.__name__ == name
            for base in self._type_class.__mro__
        )

    # -- derivation ------------------------------------------------------------

    @property
    def root_type(self) -> PyxsdType:
        variety = self._variety
        if variety == "complex":
            return self._pool.get_type(clark(XSD_NS, "anyType"))  # type: ignore[return-value]
        if variety == "union":
            return self
        if variety == "list":
            item_class = self._list_item_class()
            if item_class is not None:
                return self._pool.type_for_class(item_class)
            return self
        for base in self._type_class.__mro__[1:]:
            if base in _builtin_class_map().values():
                return self._pool.type_for_class(base)
        return self

    def _list_item_class(self) -> Any:
        item = getattr(self._type_class, "itemType", None)
        return item if isinstance(item, type) else None

    @property
    def simple_type(self) -> PyxsdType | None:
        if self.is_simple():
            return self
        content = self._simple_content_class()
        if content is None:
            return None
        return self._pool.type_for_class(content)

    @property
    def model_group(self) -> PyxsdGroup | None:
        if self.is_simple() or getattr(self._type_class, "_contentModel_", None) is None:
            return None
        if self._model_group is None:
            self._model_group = PyxsdGroup(self)
        return self._model_group

    @property
    def attributes(self) -> PyxsdAttributeGroup:
        if self._attributes is None:
            self._attributes = PyxsdAttributeGroup(self)
        return self._attributes

    # -- validation and construction ------------------------------------------

    def is_valid(
        self,
        obj: Any,
        use_defaults: bool = True,
        namespaces: Mapping[str, str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        del use_defaults, args, kwargs
        if self.is_simple():
            try:
                self._cast(obj, namespaces)
            except (TypeError, ValueError):
                return False
            return True
        return isinstance(obj, self._type_class)

    def validate(
        self,
        obj: Any,
        use_defaults: bool = True,
        namespaces: Mapping[str, str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if not self.is_valid(obj, use_defaults, namespaces, *args, **kwargs):
            raise ValueError(f"{obj!r} is not valid for {self._name or 'the anonymous type'}")

    def decode(
        self,
        obj: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        del args
        if self.is_simple():
            return self._cast(obj, kwargs.get("namespaces"))
        return obj

    def cast(self, value: Any, namespaces: Mapping[str, str] | None = None) -> Any:
        if self.is_simple():
            return self._cast(value, namespaces)
        return value

    def _cast(self, value: Any, namespaces: Mapping[str, str] | None = None) -> Any:
        del namespaces
        if isinstance(value, self._type_class) and not isinstance(value, list):
            return value
        constructor: Any = self._type_class
        return constructor(value)

    def __repr__(self) -> str:
        return f"PyxsdType({self._name!r})"


class PyxsdAttribute:
    """One pyxsd attribute declaration under elementpath protocols."""

    __slots__ = ("_er", "_name", "_parent", "_pool", "_proxy", "_type")

    def __init__(
        self,
        pool: SchemaPool,
        er: Any,
        proxy: PyxsdSchemaProxy | None,
        *,
        name: str | None = None,
        parent: Any = None,
    ) -> None:
        self._pool = pool
        self._er = er
        self._proxy = proxy
        self._name = name if name is not None else _instance_name(er, proxy, is_attribute=True)
        self._parent = parent
        self._type = pool.type_for_declaration(er)

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def tag(self) -> str | None:
        return self._name

    @property
    def attrib(self) -> Mapping[Any, Any]:
        return MappingProxyType({})

    @property
    def text(self) -> None:
        return None

    @property
    def tail(self) -> None:
        return None

    @property
    def type(self) -> PyxsdType | None:
        return self._type

    @property
    def parent(self) -> Any:
        return self._parent

    @property
    def ref(self) -> None:
        return None

    @property
    def xsd_version(self) -> str:
        return self._pool.xsd_version

    @property
    def maps(self) -> SchemaPool:
        return self._pool

    def is_matching(self, name: str | None, default_namespace: str | None = None) -> bool:
        if name is None or self._name is None:
            return False
        if name == self._name:
            return True
        if name.startswith("{"):
            return False
        return name == local_name(self._name)

    def get(self, key: str, default: Any = None) -> Any:
        return self.attrib.get(key, default)

    def __iter__(self) -> Iterator[Any]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def __hash__(self) -> int:
        return id(self)


class PyxsdAttributeGroup:
    """The declared attribute uses of one complex type."""

    __slots__ = ("_by_expanded", "_entries", "_type")

    def __init__(self, type_adapter: PyxsdType) -> None:
        self._type = type_adapter
        entries: dict[str, PyxsdAttribute] = {}
        expanded: dict[str, PyxsdAttribute] = {}
        for descriptor in _attribute_descriptors(type_adapter._type_class):
            adapter = PyxsdAttribute(
                type_adapter._pool,
                descriptor,
                type_adapter.schema,
                name=descriptor.name,
                parent=type_adapter,
            )
            entries[descriptor.name] = adapter
            expanded[
                _instance_name(descriptor, type_adapter.schema, is_attribute=True)
                or descriptor.name
            ] = adapter
        self._entries = MappingProxyType(entries)
        self._by_expanded = MappingProxyType(expanded)

    def get(self, key: str | None, default: Any = None) -> Any:
        if key is None:
            return default
        found = self._entries.get(key)
        if found is None:
            found = self._by_expanded.get(key)
        return default if found is None else found

    def items(self):
        return self._entries.items()

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and (key in self._entries or key in self._by_expanded)

    def __getitem__(self, key: str) -> PyxsdAttribute:
        adapter = self.get(key)
        if adapter is None:
            raise KeyError(key)
        return adapter

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __hash__(self) -> int:
        return id(self)

    @property
    def parent(self) -> PyxsdType:
        return self._type

    @property
    def xsd_version(self) -> str:
        return self._type.xsd_version

    @property
    def maps(self) -> SchemaPool:
        return self._type.maps

    def is_matching(self, name: str | None, default_namespace: str | None = None) -> bool:
        return self.get(name) is not None


class PyxsdGroup:
    """The element particles of one complex type under elementpath protocols."""

    __slots__ = ("_elements", "_type")

    def __init__(self, type_adapter: PyxsdType) -> None:
        self._type = type_adapter
        self._elements = tuple(
            PyxsdElement(
                type_adapter._pool,
                descriptor,
                type_adapter.schema,
                name=_instance_name(descriptor, type_adapter.schema),
            )
            for descriptor in _element_descriptors(type_adapter._type_class)
        )

    def iter_elements(self) -> Iterator[PyxsdElement]:
        return iter(self._elements)

    @property
    def name(self) -> None:
        return None

    @property
    def parent(self) -> PyxsdType:
        return self._type

    @property
    def ref(self) -> None:
        return None

    @property
    def xsd_version(self) -> str:
        return self._type.xsd_version

    @property
    def maps(self) -> SchemaPool:
        return self._type.maps

    def is_matching(self, name: str | None, default_namespace: str | None = None) -> bool:
        return any(element.is_matching(name, default_namespace) for element in self._elements)


class PyxsdElement:
    """One pyxsd element declaration under elementpath's element protocol."""

    __slots__ = ("_attrib", "_children", "_er", "_name", "_pool", "_proxy", "_type")

    def __init__(
        self,
        pool: SchemaPool,
        er: Any,
        proxy: PyxsdSchemaProxy | None,
        *,
        name: str | None = None,
    ) -> None:
        self._pool = pool
        self._er = er
        self._proxy = proxy
        self._name = name if name is not None else _instance_name(er, proxy)
        self._type = pool.type_for_declaration(er)
        self._children = tuple(
            PyxsdElement(
                pool,
                descriptor,
                proxy,
                name=_instance_name(descriptor, proxy),
            )
            for descriptor in (
                _element_descriptors(self._type._type_class) if self._type is not None else ()
            )
        )
        self._attrib = self._type.attributes if self._type is not None else None

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def tag(self) -> str | None:
        return self._name

    @property
    def attrib(self) -> Any:
        if self._attrib is not None:
            return self._attrib
        return MappingProxyType({})

    @property
    def text(self) -> None:
        return None

    @property
    def tail(self) -> None:
        return None

    @property
    def type(self) -> PyxsdType | None:
        return self._type

    @property
    def parent(self) -> PyxsdSchemaProxy | None:
        return self._proxy

    @property
    def ref(self) -> None:
        return None

    @property
    def xsd_version(self) -> str:
        return self._pool.xsd_version

    @property
    def maps(self) -> SchemaPool:
        return self._pool

    def is_matching(self, name: str | None, default_namespace: str | None = None) -> bool:
        if self._name is None or name is None:
            return False
        if name == self._name:
            return True
        if name.startswith("{"):
            return False
        if ":" in name:
            expanded = self._proxy.expand_name(name) if self._proxy is not None else None
            return expanded == self._name
        if name != local_name(self._name):
            return False
        namespace = namespace_of(self._name)
        return default_namespace is None or namespace == default_namespace

    def get(self, key: str, default: Any = None) -> Any:
        return self.attrib.get(key, default)

    def __iter__(self) -> Iterator[PyxsdElement]:
        return iter(self._children)

    def iter(self, tag: str | None = None) -> Iterator[PyxsdElement]:
        if tag is None or tag == "*" or tag == self._name:
            yield self
        for child in self._children:
            yield from child.iter(tag)

    def find(self, path: str, namespaces: Mapping[str, str] | None = None) -> PyxsdElement | None:
        del namespaces
        wanted = path[2:] if path.startswith("./") else path
        for child in self._children:
            if child.is_matching(wanted):
                return child
        return None

    def __len__(self) -> int:
        return len(self._children)

    def __hash__(self) -> int:
        return id(self)

    def is_valid(self, obj: Any, *args: Any, **kwargs: Any) -> bool:
        if self._type is None:
            return False
        return self._type.is_valid(obj, *args, **kwargs)

    def validate(self, obj: Any, *args: Any, **kwargs: Any) -> None:
        if self._type is None:
            raise ValueError("the element declaration has no resolved type")
        self._type.validate(obj, *args, **kwargs)

    def decode(self, obj: Any, *args: Any, **kwargs: Any) -> Any:
        if self._type is None:
            return obj
        return self._type.decode(obj, *args, **kwargs)


class PyxsdSchemaProxy:
    """One compiled pyxsd schema under elementpath's schema protocol."""

    __slots__ = ("_base_element", "_namespaces", "_pool", "_schema")

    def __init__(
        self,
        schema: Any,
        pool: SchemaPool,
        *,
        xsd_version: str | None = None,
        base_element: PyxsdElement | None = None,
    ) -> None:
        del xsd_version
        self._schema = schema
        self._pool = pool
        self._base_element = base_element
        self._namespaces: dict[str, str] = {}

    @property
    def schema(self) -> PyxsdSchemaProxy:
        return self

    @property
    def pool(self) -> SchemaPool:
        return self._pool

    @property
    def target_namespace(self) -> str | None:
        return getattr(self._schema, "target_namespace", None)

    @property
    def base_element(self) -> PyxsdElement | None:
        return self._base_element

    @property
    def name(self) -> str | None:
        return self.target_namespace

    @property
    def tag(self) -> str:
        return clark(XSD_NS, "schema")

    @property
    def attrib(self) -> Mapping[Any, Any]:
        return MappingProxyType({})

    @property
    def text(self) -> None:
        return None

    @property
    def tail(self) -> None:
        return None

    @property
    def maps(self) -> SchemaPool:
        return self._pool

    @property
    def validity(self) -> str:
        report = getattr(self._schema, "report", None)
        return "invalid" if report is not None and report.has_errors else "valid"

    @property
    def validation_attempted(self) -> str:
        return "full" if self.validity == "valid" else "partial"

    def is_fully_valid(self) -> bool:
        return self.validity == "valid" and self.validation_attempted == "full"

    @property
    def xsd_version(self) -> str:
        return getattr(self._schema, "xsd_version", self._pool.xsd_version)

    def is_assertion_based(self) -> bool:
        return False

    def is_matching(self, name: str | None, default_namespace: str | None = None) -> bool:
        del default_namespace
        return name is None or name == "" or name == self.target_namespace

    def __iter__(self) -> Iterator[PyxsdElement]:
        return iter(self._pool.elements_for(self))

    def iter(self, tag: str | None = None) -> Iterator[PyxsdElement]:
        for element in self:
            if tag is None or tag == "*" or tag == element.name:
                yield element

    def get(self, key: str, default: Any = None) -> Any:
        return self.attrib.get(key, default)

    def __len__(self) -> int:
        return len(self._pool.elements_for(self))

    def __hash__(self) -> int:
        return id(self)

    def find(
        self, path: str, namespaces: Mapping[str, str] | None = None
    ) -> PyxsdElement | PyxsdSchemaProxy | None:
        if path in (".", "./"):
            return self
        wanted = path[2:] if path.startswith("./") else path
        expanded = self.expand_name(wanted, namespaces)
        element = self._pool.get_element(expanded or wanted)
        if element is not None and element.parent is self:
            return element
        return None

    def get_type(self, qname: str) -> PyxsdType | None:
        return self._pool.get_type(self.expand_name(qname))

    def get_attribute(self, qname: str) -> PyxsdAttribute | None:
        return self._pool.get_attribute(self.expand_name(qname))

    def get_element(self, qname: str) -> PyxsdElement | None:
        return self._pool.get_element(self.expand_name(qname))

    def get_substitution_group(self, qname: str) -> frozenset[PyxsdElement] | None:
        return self._pool.get_substitution_group(self.expand_name(qname))

    def iter_atomic_types(self) -> Iterator[PyxsdType]:
        for type_adapter in self._pool.imported_types_for(self):
            if type_adapter.is_atomic() and type_adapter.name is not None:
                yield type_adapter

    def bind_parser(self, parser: Any) -> None:
        """Bind *parser* to this proxy and register imported constructors."""
        self._namespaces = dict(getattr(parser, "namespaces", None) or {})
        if getattr(parser, "schema", None) is not self:
            parser.schema = self
        from elementpath.namespaces import get_prefixed_name

        for type_adapter in self.iter_atomic_types():
            assert type_adapter.name is not None
            symbol = get_prefixed_name(type_adapter.name, self._namespaces)
            if ":" not in symbol or symbol.startswith("{"):
                continue
            if symbol not in parser.symbol_table:
                parser.schema_constructor(type_adapter.name)

    def get_context(self) -> Any:
        from elementpath.xpath_context import XPathSchemaContext

        return XPathSchemaContext(
            root=cast(Any, self),
            item=cast(Any, self._base_element),
            schema=cast(Any, self),
        )

    def cast_as(self, obj: Any, type_qname: str) -> Any:
        type_adapter = self.get_type(type_qname)
        if type_adapter is None:
            raise KeyError(type_qname)
        return type_adapter.cast(obj, self._namespaces)

    def is_instance(self, obj: Any, type_qname: str) -> bool:
        type_adapter = self.get_type(type_qname)
        if type_adapter is None:
            raise KeyError(type_qname)
        return type_adapter.is_valid(obj, namespaces=self._namespaces)

    def expand_name(self, name: str, namespaces: Mapping[str, str] | None = None) -> str:
        """Expand a Clark name or prefixed QName using the bound namespaces."""
        if name.startswith("{"):
            return name
        if ":" not in name:
            return name
        prefix, local = name.split(":", 1)
        bindings = self._namespaces if namespaces is None else namespaces
        uri = bindings.get(prefix)
        if uri is None:
            raise KeyError(f"unknown namespace prefix {prefix!r}")
        return clark(uri, local)

    def __repr__(self) -> str:
        return f"PyxsdSchemaProxy({self.target_namespace!r})"


class SchemaPool:
    """An immutable composition of compiled pyxsd schemas.

    ``types``, ``elements``, ``attributes``, ``substitution_groups``,
    ``constructors``, and ``schemas`` are read-only mappings built once in
    ``__init__``.  Built-in XSD types are always available; imported named
    atomic types gain constructor functions.
    """

    _schemas: dict[str | None, PyxsdSchemaProxy]
    _types: dict[str, PyxsdType]
    _elements: dict[str, PyxsdElement]
    _attributes: dict[str, PyxsdAttribute]
    _substitution_groups: dict[str, frozenset[PyxsdElement]]
    _constructors: dict[str, AtomicConstructor]

    __slots__ = (
        "_attribute_cache",
        "_attributes",
        "_constructors",
        "_element_cache",
        "_elements",
        "_owners",
        "_schemas",
        "_substitution_groups",
        "_type_cache",
        "_types",
        "_xsd_version",
    )

    def __init__(self, schemas: Iterable[Any] = (), *, xsd_version: str = "1.1") -> None:
        if xsd_version not in _XSD_VERSION_NAMES:
            raise ValueError(f"unsupported xsd_version {xsd_version!r}")
        self._xsd_version = xsd_version
        self._type_cache: dict[int, PyxsdType] = {}
        self._element_cache: dict[int, PyxsdElement] = {}
        self._attribute_cache: dict[int, PyxsdAttribute] = {}
        self._owners: dict[int, PyxsdSchemaProxy] = {}
        self._schemas: dict[str | None, PyxsdSchemaProxy] = {}
        self._types: dict[str, PyxsdType] = {}
        self._elements: dict[str, PyxsdElement] = {}
        self._attributes: dict[str, PyxsdAttribute] = {}
        self._substitution_groups: dict[str, frozenset[PyxsdElement]] = {}
        self._constructors: dict[str, AtomicConstructor] = {}

        for name, type_class in _builtin_class_map().items():
            adapter = PyxsdType(
                self,
                type_class,
                name=clark(XSD_NS, name),
                is_builtin=True,
            )
            assert adapter.name is not None
            self._types[adapter.name] = adapter
            self._type_cache[id(type_class)] = adapter
            if adapter.is_atomic():
                self._constructors[adapter.name] = AtomicConstructor(adapter.name, adapter)

        proxies: list[PyxsdSchemaProxy] = []
        for schema in schemas:
            proxy = PyxsdSchemaProxy(schema, self, xsd_version=xsd_version)
            namespace = proxy.target_namespace
            if namespace in self._schemas:
                raise ValueError(f"duplicate schema namespace {namespace!r}")
            self._schemas[namespace] = proxy
            proxies.append(proxy)
            self._register_schema(proxy)

        # The slots stay dict-typed for the private registration helpers;
        # after construction they hold read-only mapping proxies.
        self._schemas = cast(
            dict[str | None, PyxsdSchemaProxy], MappingProxyType(dict(self._schemas))
        )
        self._types = cast(dict[str, PyxsdType], MappingProxyType(dict(self._types)))
        self._elements = cast(dict[str, PyxsdElement], MappingProxyType(dict(self._elements)))
        self._attributes = cast(dict[str, PyxsdAttribute], MappingProxyType(dict(self._attributes)))
        self._substitution_groups = cast(
            dict[str, frozenset[PyxsdElement]],
            MappingProxyType(dict(self._substitution_groups)),
        )
        self._constructors = cast(
            dict[str, AtomicConstructor], MappingProxyType(dict(self._constructors))
        )

    @classmethod
    def from_imports(
        cls,
        paths: Iterable[str | Any],
        *,
        xsd_version: str = "1.1",
    ) -> SchemaPool:
        """Compile each imported schema path and compose them."""
        if xsd_version not in _XSD_VERSION_NAMES:
            raise ValueError(f"unsupported xsd_version {xsd_version!r}")
        from pyxsd.schema import Schema

        version = cast(Literal["1.0", "1.1"], xsd_version)
        schemas = [Schema.compile(path, xsd_version=version) for path in paths]
        return cls(schemas, xsd_version=xsd_version)

    @classmethod
    def from_schema(cls, schema: Any) -> SchemaPool:
        """Compose one already-compiled schema."""
        return cls([schema], xsd_version=getattr(schema, "xsd_version", "1.1"))

    @property
    def xsd_version(self) -> str:
        return self._xsd_version

    @property
    def schemas(self) -> Mapping[str | None, PyxsdSchemaProxy]:
        return self._schemas

    @property
    def types(self) -> Mapping[str, PyxsdType]:
        return self._types

    @property
    def elements(self) -> Mapping[str, PyxsdElement]:
        return self._elements

    @property
    def attributes(self) -> Mapping[str, PyxsdAttribute]:
        return self._attributes

    @property
    def substitution_groups(self) -> Mapping[str, frozenset[PyxsdElement]]:
        return self._substitution_groups

    @property
    def constructors(self) -> Mapping[str, AtomicConstructor]:
        return self._constructors

    def proxy(self, namespace: str | None = None) -> PyxsdSchemaProxy | None:
        return self._schemas.get(namespace)

    def get_type(self, qname: str | None) -> PyxsdType | None:
        return _lookup(self._types, qname)

    def get_element(self, qname: str | None) -> PyxsdElement | None:
        return _lookup(self._elements, qname)

    def get_attribute(self, qname: str | None) -> PyxsdAttribute | None:
        return _lookup(self._attributes, qname)

    def get_substitution_group(self, qname: str | None) -> frozenset[PyxsdElement] | None:
        if qname is None:
            return None
        return self._substitution_groups.get(qname)

    def constructor(self, qname: str) -> AtomicConstructor:
        constructor = self._constructors.get(qname)
        if constructor is None:
            resolved = self.get_type(qname)
            if resolved is not None:
                constructor = self._constructors.get(resolved.name or "")
        if constructor is None:
            raise KeyError(qname)
        return constructor

    # -- registration ----------------------------------------------------------

    def type_for_class(self, type_class: Any) -> PyxsdType:
        adapter = self._type_cache.get(id(type_class))
        if adapter is None:
            adapter = PyxsdType(self, type_class)
            self._type_cache[id(type_class)] = adapter
        return adapter

    def type_for_declaration(self, declaration: Any) -> PyxsdType | None:
        getter = getattr(declaration, "getType", None)
        if getter is None:
            return None
        try:
            type_class = getter()
        except Exception:
            return None
        if not isinstance(type_class, type):
            return None
        return self.type_for_class(type_class)

    def elements_for(self, proxy: PyxsdSchemaProxy) -> tuple[PyxsdElement, ...]:
        return tuple(element for element in self._elements.values() if element.parent is proxy)

    def imported_types_for(self, proxy: PyxsdSchemaProxy) -> tuple[PyxsdType, ...]:
        return tuple(
            type_adapter
            for type_adapter in self._types.values()
            if not type_adapter.is_builtin and type_adapter.schema is proxy
        )

    def _register_schema(self, proxy: PyxsdSchemaProxy) -> None:
        schema = proxy._schema
        namespace = proxy.target_namespace
        for entries in getattr(schema, "components", {}).values():
            for entry in entries:
                kind = type(entry).__name__
                if kind in _TYPE_KINDS and entry.isGlobalDeclaration():
                    self._register_type(proxy, entry)
                elif kind in _ELEMENT_KINDS and entry.isGlobalDeclaration():
                    self._register_element(proxy, entry)
                elif kind in _ATTRIBUTE_KINDS and entry.isGlobalDeclaration():
                    if entry.getNamespace() in _INJECTED_NAMESPACES:
                        continue
                    if entry.getNamespace() != namespace:
                        continue
                    self._register_attribute(proxy, entry)
        schema_entries = getattr(schema, "components", {}).get("schema") or ()
        schema_er = next(iter(schema_entries), None)
        substitution_groups = getattr(schema_er, "substitutionGroups", {}) or {}
        for head_name, members in substitution_groups.items():
            head = self._elements.get(head_name)
            if head is None or head.parent is not proxy:
                continue
            adapters = frozenset(
                element
                for member in members
                if (element := self._element_for_er(proxy, member, global_declaration=True))
                is not None
            )
            if adapters:
                self._substitution_groups[head_name] = adapters

    def _register_type(self, proxy: PyxsdSchemaProxy, er: Any) -> None:
        name = schema_type_name_for_er(er, proxy._schema)
        if name is None:
            return
        type_class = self._class_for_er(proxy._schema, er)
        if type_class is None:
            return
        adapter = PyxsdType(self, type_class, er=er, proxy=proxy, name=name)
        self._types.setdefault(name, adapter)
        if adapter.is_atomic():
            self._constructors.setdefault(name, AtomicConstructor(name, adapter))

    def _register_element(self, proxy: PyxsdSchemaProxy, er: Any) -> None:
        adapter = self._element_for_er(proxy, er, global_declaration=True)
        if adapter is not None and adapter.name is not None:
            self._elements.setdefault(adapter.name, adapter)

    def _register_attribute(self, proxy: PyxsdSchemaProxy, er: Any) -> None:
        name = er.expandedName or er.name
        if name is None:
            return
        adapter = self._attribute_cache.get(id(er))
        if adapter is None:
            adapter = PyxsdAttribute(self, er, proxy, name=name)
            self._attribute_cache[id(er)] = adapter
        self._attributes.setdefault(name, adapter)

    def _element_for_er(
        self, proxy: PyxsdSchemaProxy, er: Any, *, global_declaration: bool
    ) -> PyxsdElement | None:
        adapter = self._element_cache.get(id(er))
        if adapter is not None:
            return adapter
        name = er.expandedName or er.name if global_declaration else _instance_name(er, proxy)
        if name is None:
            return None
        adapter = PyxsdElement(self, er, proxy, name=name)
        self._element_cache[id(er)] = adapter
        return adapter

    @staticmethod
    def _class_for_er(schema: Any, er: Any) -> Any:
        classes = getattr(schema, "classes", {})
        for key in (getattr(er, "expandedName", None), getattr(er, "name", None)):
            if key is not None and key in classes:
                return classes[key]
        return None


# ---------------------------------------------------------------------------
# Helpers


def _lookup(mapping: Mapping[str, Any], qname: str | None) -> Any:
    if qname is None:
        return None
    found = mapping.get(qname)
    if found is not None:
        return found
    if not qname.startswith("{") and ":" not in qname:
        matches = [value for key, value in mapping.items() if local_name(key) == qname]
        if len(matches) == 1:
            return matches[0]
    return None


def schema_type_name_for_er(er: Any, schema: Any) -> str | None:
    """Return the global type's Clark name, or ``None`` for an anonymous type."""
    name = getattr(er, "name", None)
    if not isinstance(name, str) or not name or "|" in name:
        return None
    namespace = getattr(er, "getNamespace", lambda: None)()
    if namespace is None:
        namespace = getattr(schema, "target_namespace", None)
    return clark(namespace, name)


def _instance_name(
    er: Any, proxy: PyxsdSchemaProxy | None, *, is_attribute: bool = False
) -> str | None:
    method = getattr(er, "expandedInstanceName", None)
    parser = getattr(proxy._schema, "_host", None) if proxy is not None else None
    if method is not None:
        try:
            return method(is_attribute=is_attribute, parser=parser)
        except Exception:
            pass
    return getattr(er, "expandedName", None) or getattr(er, "name", None)


def _element_descriptors(type_class: Any) -> tuple[Any, ...]:
    if not isinstance(type_class, type):
        return ()
    try:
        instance: Any = object.__new__(type_class)
        collector = cast(Any, type_class)._getElements
        return tuple(collector(instance))
    except Exception:
        descriptors: dict[str, Any] = {}
        for klass in reversed(type_class.__mro__):
            for key in klass.__dict__.get("_elementNames_", ()):
                descriptor = klass.__dict__[key]
                descriptors[descriptor.name] = descriptor
        return tuple(descriptors.values())


def _attribute_descriptors(type_class: Any) -> tuple[Any, ...]:
    if not isinstance(type_class, type):
        return ()
    descriptors: dict[str, Any] = {}
    for klass in reversed(type_class.__mro__):
        for key in klass.__dict__.get("_attributeNames_", ()):
            descriptor = klass.__dict__[key]
            descriptors[descriptor.name] = descriptor
    return tuple(descriptors.values())


def _builtin_class_map() -> dict[str, type[Any]]:
    global _BUILTIN_CLASSES
    if _BUILTIN_CLASSES is None:
        from pyxsd import xsd_data_types as xsd

        builtins: dict[str, type[Any]] = {}
        for export in xsd.__all__:
            datatype = getattr(xsd, export)
            if (
                isinstance(datatype, type)
                and issubclass(datatype, xsd.XsdDataType)
                and "name" in datatype.__dict__
            ):
                builtins[datatype.name] = datatype
        _BUILTIN_CLASSES = builtins
    return _BUILTIN_CLASSES
