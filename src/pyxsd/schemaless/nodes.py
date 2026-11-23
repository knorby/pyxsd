"""Document-local Python mixins selected by lexical XML occurrences.

A :class:`NodeMapping` compiles the supplied mixin classes into
:class:`~pyxsd.schemaless.tree.SchemalessElement` subclasses. The descriptor
factories in this module declare data-centric fields on those mixins:
:func:`attribute` and :func:`text` read and write one scalar through the
document's current :class:`~pyxsd.schemaless.types.TypePolicy`, while
:func:`child` and :func:`children` navigate the authoritative child sequence
in document order. A descriptor never keeps a second copy of lexical data and
never invents an interpretation that ordinary typed node access cannot see.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, overload

from pyxsd._lexical import LexicalElement
from pyxsd._lexical.tree import _check_name
from pyxsd.locations import PathStep
from pyxsd.schemaless.tree import (
    SchemalessElement,
    SchemalessRevision,
    _current_path,
    _project_class,
    _XmlPath,
)
from pyxsd.schemaless.types import ScalarAdapter, TypePolicy, TypeRef

_Mixin = TypeVar("_Mixin", bound=type)
_Predicate = Callable[[SchemalessElement], bool]
_Rule = tuple[type, _XmlPath | _Predicate]
_Reader = Callable[[SchemalessElement], Any]
_AdapterPin = tuple[str, ScalarAdapter]
_DescriptorField = tuple["_NodeDescriptor", type]


def attribute(name: str, *, type: TypeRef | None = None) -> Any:
    """Declare one expanded-name attribute field.

    Reads call :meth:`~pyxsd.schemaless.tree.SchemalessElement.attribute_value`
    and therefore follow the document's effective scalar policy; a missing
    attribute reads as ``None``. Writes format the value through that same
    adapter and replace only that attribute. *name* is an expanded (Clark)
    name, not a serialization prefix.
    """
    _check_name(name, "attribute")
    return _AttributeDescriptor(name, _resolve_adapter(type))


def text(*, type: TypeRef | None = None, default: Any = None) -> Any:
    """Declare the element's leading character data as a scalar field.

    Reads call :meth:`~pyxsd.schemaless.tree.SchemalessElement.scalar_value`
    for present leading text and return *default* when there is none. Mixed
    content stays mixed content: child tails are never concatenated into this
    value. Writes replace only the leading text.
    """
    return _TextDescriptor(_resolve_adapter(type), default)


def child(name: str) -> Any:
    """Declare one child element field by expanded name.

    The field reads ``None`` when no child has *name*. More than one matching
    child is ambiguous and raises ``ValueError``, unless the generated mapped
    class keeps a :func:`children` view for that name declared by the same
    class as this singular field: the list view then makes the repetition
    explicit, and the singular field addresses its first element. An inherited
    pair from one declaring class stays lenient; a list view from another
    mixin, or one shadowed or replaced in the generated MRO, does not suppress
    the ambiguity. Navigation fields are read-only.
    """
    _check_name(name, "child element")
    return _ChildDescriptor(name)


def children(name: str) -> Any:
    """Declare all child elements with one expanded name as a tuple.

    Every read builds a new tuple from the current authoritative child
    sequence, so the result follows document order after any edit.
    """
    _check_name(name, "child element")
    return _ChildrenDescriptor(name)


def _resolve_adapter(type_ref: TypeRef | None) -> ScalarAdapter | None:
    return None if type_ref is None else TypePolicy.resolve(type_ref)


class _NodeDescriptor:
    """One field declaration owned by a node mapping mixin."""

    __slots__ = ("_adapter", "_read")

    def __init__(self, adapter: ScalarAdapter | None, read: _Reader) -> None:
        self._adapter = adapter
        self._read = read

    def __get__(self, instance: SchemalessElement | None, owner: type[Any] | None = None) -> Any:
        if instance is None:
            return self
        return self._read(instance)

    def __set__(self, instance: SchemalessElement, value: Any) -> None:
        raise AttributeError("this node mapping descriptor is read-only")

    def _pin_path(self, path: str) -> str:
        """Return the occurrence path this descriptor's declared type pins."""
        return path


class _AttributeDescriptor(_NodeDescriptor):
    __slots__ = ("_expanded_name",)

    def __init__(self, expanded_name: str, adapter: ScalarAdapter | None) -> None:
        super().__init__(adapter, lambda node: node.attribute_value(expanded_name))
        self._expanded_name = expanded_name

    def __set__(self, instance: SchemalessElement, value: Any) -> None:
        adapter = _effective_adapter(instance, self._expanded_name)
        instance.set_attribute(self._expanded_name, _lexical_write(adapter, value))

    def _pin_path(self, path: str) -> str:
        return f"{path}/@{self._expanded_name}"


class _TextDescriptor(_NodeDescriptor):
    __slots__ = ()

    def __init__(self, adapter: ScalarAdapter | None, default: Any) -> None:
        super().__init__(
            adapter,
            lambda node: default if node.text_value() is None else node.scalar_value(),
        )

    def __set__(self, instance: SchemalessElement, value: Any) -> None:
        instance.set_text(_lexical_write(_effective_adapter(instance, None), value))


class _ChildDescriptor(_NodeDescriptor):
    __slots__ = ("_expanded_name",)

    def __init__(self, expanded_name: str) -> None:
        super().__init__(None, lambda node: _single_child(node, expanded_name))
        self._expanded_name = expanded_name


class _ChildrenDescriptor(_NodeDescriptor):
    __slots__ = ("_expanded_name",)

    def __init__(self, expanded_name: str) -> None:
        super().__init__(
            None,
            lambda node: tuple(child for child in node._children_ if child._name_ == expanded_name),
        )
        self._expanded_name = expanded_name


def _single_child(node: SchemalessElement, name: str) -> SchemalessElement | None:
    matches = [child for child in node._children_ if child._name_ == name]
    if not matches:
        return None
    if len(matches) > 1 and name not in getattr(type(node), "_child_pair_names_", frozenset()):
        raise ValueError(f"multiple child elements named {name!r}")
    return matches[0]


def _paired_child_names(effective: dict[str, _DescriptorField]) -> frozenset[str]:
    """Return singular child names paired with a list view on the same class.

    *effective* is the descriptor map of the final generated class, so a list
    view shadowed or replaced anywhere in its MRO is already gone. Leniency is
    deliberately local: a ``children(name)`` descriptor only makes
    ``child(name)`` address the first occurrence when one declaring class
    supplies both effective descriptors. A list view from another mixin or a
    shadowed declaration does not hide ambiguity.
    """
    lists: dict[type, set[str]] = {}
    for descriptor, owner in effective.values():
        if isinstance(descriptor, _ChildrenDescriptor):
            lists.setdefault(owner, set()).add(descriptor._expanded_name)
    paired: set[str] = set()
    for descriptor, owner in effective.values():
        if isinstance(descriptor, _ChildDescriptor) and (
            descriptor._expanded_name in lists.get(owner, ())
        ):
            paired.add(descriptor._expanded_name)
    return frozenset(paired)


def _effective_adapter(instance: SchemalessElement, attribute: str | None) -> ScalarAdapter | None:
    """Return the adapter the ordinary typed readers use for this occurrence.

    Descriptor reads delegate to the revision's typed snapshot, so a write must
    resolve the same occurrence pin, and the same inference fallback, rather
    than a private copy of the declared type. A generic lexical clone has no
    scalar policy and keeps lexical writes.
    """
    revision = instance._revision
    if not isinstance(revision, SchemalessRevision):
        return None
    infer = revision.types.mode == "infer"
    if not infer and not revision._xml_pins:
        return None
    lexical = instance.text_value() if attribute is None else instance.attribute_lexical(attribute)
    root, path = _current_path(instance)
    pinned = revision._pin_for(root, path, attribute)
    if pinned is not None:
        return pinned
    if infer and lexical is not None:
        return revision._inferred_adapter(root, path, attribute)
    return None


def _lexical_write(adapter: ScalarAdapter | None, value: Any) -> str:
    """Serialize one assignment through the effective adapter, if any."""
    if adapter is None:
        if not isinstance(value, str):
            raise TypeError("a lexical node mapping write requires a string")
        return value
    return adapter.format(value)


class NodeMapping:
    """Register Python mixins for XML element occurrences.

    Each parse uses the current rules as a snapshot; later registrations do
    not change existing documents. Later matching rules take precedence,
    and a mixin matched more than once appears only at its highest priority.
    Descriptor ``type=`` entries compile into occurrence pins on the same
    document policy; a later :meth:`~pyxsd.schemaless.document.SchemalessDocument.pin_type`
    at that path replaces them for descriptor and ordinary reads alike.
    """

    def __init__(self) -> None:
        self._compiled = _CompiledNodeMapping(())

    @overload
    def register(
        self,
        mixin: None = None,
        *,
        path: str | None = None,
        predicate: _Predicate | None = None,
    ) -> Callable[[_Mixin], _Mixin]: ...

    @overload
    def register(
        self,
        mixin: type,
        *,
        path: str | None = None,
        predicate: _Predicate | None = None,
    ) -> None: ...

    def register(
        self,
        mixin: type | None = None,
        *,
        path: str | None = None,
        predicate: _Predicate | None = None,
    ) -> Callable[[_Mixin], _Mixin] | None:
        """Register a mixin directly or return a decorator for its class.

        Supply exactly one selector. Paths start at the document root, use
        Clark names for namespaces, and can specify one-based same-name
        indexes. If any step is indexed, omitted indexes default to one.
        A predicate sees the unprojected lexical node and must not edit it.
        A descriptor ``type=`` requires a path selector because its pin has
        to name the occurrences it applies to.
        """
        selector: _XmlPath | _Predicate
        if path is not None and predicate is None:
            if not isinstance(path, str):
                raise TypeError("a node mapping path must be a string")
            try:
                xml_path = _XmlPath.parse(path)
            except ValueError as exc:
                raise ValueError(f"invalid XML element path {path!r}") from exc
            if xml_path.attribute is not None:
                raise ValueError(f"invalid XML element path {path!r}: attributes are not nodes")
            selector = xml_path
        elif path is None and callable(predicate):
            selector = predicate
        else:
            raise TypeError(
                "register requires exactly one selector: a path or a callable predicate"
            )

        def register_class(cls: _Mixin) -> _Mixin:
            if not isinstance(cls, type) or issubclass(cls, LexicalElement):
                raise TypeError("a node mapping mixin must be a class, not a lexical element type")
            pins = _descriptor_pins(cls, path)
            target = _mixin_class((cls,))
            compiled = _CompiledNodeMapping(
                ((cls, selector), *self._compiled.rules),
                (*self._compiled.pins, *pins),
            )
            compiled.classes[(id(cls),)] = target
            self._compiled = compiled
            return cls

        if mixin is None:
            return register_class
        register_class(mixin)
        return None


class _CompiledNodeMapping:
    """An immutable rule sequence with its own generated class cache."""

    def __init__(self, rules: tuple[_Rule, ...], pins: tuple[_AdapterPin, ...] = ()) -> None:
        self.rules = rules
        self.pins = pins
        self.classes: dict[tuple[int, ...], type[SchemalessElement]] = {}

    def apply(self, root: SchemalessElement) -> SchemalessElement:
        """Resolve all selectors and classes before changing any lexical node."""
        selected: list[tuple[SchemalessElement, int, type[SchemalessElement]]] = []
        stack: list[tuple[SchemalessElement, int, tuple[PathStep, ...]]] = [
            (root, 0, (PathStep(root._name_, 1),))
        ]
        while stack:
            node, position, path = stack.pop()
            mixins: dict[int, type] = {}
            for mixin, selector in self.rules:
                matches = (
                    selector.matches(path, None)
                    if isinstance(selector, _XmlPath)
                    else selector(node)
                )
                if matches:
                    mixins.setdefault(id(mixin), mixin)
            if mixins:
                key = tuple(mixins)
                target = self.classes.get(key)
                if target is None:
                    target = self.classes.setdefault(key, _mixin_class(tuple(mixins.values())))
                selected.append((node, position, target))

            counts: dict[str, int] = {}
            children = []
            for index, child in enumerate(node._children_):
                name = child._name_
                counts[name] = occurrence = counts.get(name, 0) + 1
                children.append((child, index, (*path, PathStep(name, occurrence))))
            stack.extend(reversed(children))

        for node, position, target in selected:
            projected = _project_class(node, target, position)
            if node is root:
                root = projected
        return root


def _declared_descriptors(mixin: type) -> dict[str, _DescriptorField]:
    """Return one class's effective descriptors, with its own MRO deciding overrides.

    A subclass name shadows a base name even when the subclass does not supply
    a descriptor, exactly as normal attribute lookup would. Only the first
    class in the MRO that defines a name can contribute it.
    """
    found: dict[str, _DescriptorField] = {}
    seen: set[str] = set()
    for base in mixin.__mro__:
        for name, value in vars(base).items():
            if name in seen:
                continue
            seen.add(name)
            if isinstance(value, _NodeDescriptor):
                found[name] = (value, base)
    return found


def _descriptor_fields(mixins: tuple[type, ...]) -> dict[str, _DescriptorField]:
    """Combine the matching mixins' descriptors or reject incompatible ones.

    A repeated declaration is compatible when it is the same descriptor object
    or when one declaring class derives from the other, because the generated
    MRO then has one deterministic winner. Base/derived mixins in an impossible
    order are rejected separately by :func:`_check_mixin_order`.
    """
    fields: dict[str, _DescriptorField] = {}
    for mixin in mixins:
        for name, declaration in _declared_descriptors(mixin).items():
            previous = fields.get(name)
            if previous is None:
                fields[name] = declaration
            elif not _same_declaration(previous, declaration):
                raise TypeError(f"conflicting node mapping descriptors for field {name!r}")
    return fields


def _same_declaration(first: _DescriptorField, second: _DescriptorField) -> bool:
    left, left_owner = first
    right, right_owner = second
    return (
        left is right or issubclass(left_owner, right_owner) or issubclass(right_owner, left_owner)
    )


def _check_mixin_order(mixins: tuple[type, ...]) -> None:
    """Reject a base class placed before one of its own subclasses."""
    for index, mixin in enumerate(mixins):
        if any(issubclass(candidate, mixin) for candidate in mixins[index + 1 :]):
            raise TypeError(
                "incompatible node mapping mixin order: register a base class before its subclasses"
            )


def _descriptor_pins(mixin: type, path: str | None) -> tuple[_AdapterPin, ...]:
    """Compile one registration's descriptor types into occurrence pins."""
    pins: list[_AdapterPin] = []
    for descriptor, _owner in _declared_descriptors(mixin).values():
        adapter = descriptor._adapter
        if adapter is None:
            continue
        if path is None:
            raise TypeError("a descriptor type pin requires a path selector")
        pin_path = descriptor._pin_path(path)
        try:
            _XmlPath.parse(pin_path)
        except ValueError as exc:
            raise ValueError(f"invalid descriptor type path {pin_path!r}") from exc
        pins.append((pin_path, adapter))
    return tuple(pins)


def _mixin_class(mixins: tuple[type, ...]) -> type[SchemalessElement]:
    _descriptor_fields(mixins)  # Reject incompatible same-name declarations.
    _check_mixin_order(mixins)
    name = "SchemalessElement_" + "_".join(mixin.__name__ for mixin in mixins)
    try:
        generated = type(
            name,
            (*mixins, SchemalessElement),
            {"__slots__": (), "__module__": __name__},
        )
    except TypeError as exc:
        raise TypeError(f"incompatible node mapping mixin bases: {exc}") from exc
    # Pairing must see the final MRO: a later mixin can shadow a list descriptor
    # with a plain attribute, a method, or another descriptor.
    generated._child_pair_names_ = _paired_child_names(  # type: ignore[attr-defined]
        _declared_descriptors(generated)
    )
    if generated.__hash__ is None:
        raise TypeError("a node mapping mixin must preserve node hashability")
    return generated
