"""Document-local Python mixins selected by lexical XML occurrences."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, overload

from pyxsd._lexical import LexicalElement
from pyxsd.locations import PathStep
from pyxsd.schemaless.tree import SchemalessElement, _project_class, _XmlPath

_Mixin = TypeVar("_Mixin", bound=type)
_Predicate = Callable[[SchemalessElement], bool]
_Rule = tuple[type, _XmlPath | _Predicate]


class NodeMapping:
    """Register Python mixins for XML element occurrences.

    Each parse uses the current rules as a snapshot; later registrations do
    not change existing documents. Later matching rules take precedence,
    and a mixin matched more than once appears only at its highest priority.
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
            target = _mixin_class((cls,))
            compiled = _CompiledNodeMapping(((cls, selector), *self._compiled.rules))
            compiled.classes[(id(cls),)] = target
            self._compiled = compiled
            return cls

        if mixin is None:
            return register_class
        register_class(mixin)
        return None


class _CompiledNodeMapping:
    """An immutable rule sequence with its own generated class cache."""

    def __init__(self, rules: tuple[_Rule, ...]) -> None:
        self.rules = rules
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


def _mixin_class(mixins: tuple[type, ...]) -> type[SchemalessElement]:
    name = "SchemalessElement_" + "_".join(mixin.__name__ for mixin in mixins)
    try:
        generated = type(
            name, (*mixins, SchemalessElement), {"__slots__": (), "__module__": __name__}
        )
    except TypeError as exc:
        raise TypeError(f"incompatible node mapping mixin bases: {exc}") from exc
    if generated.__hash__ is None:
        raise TypeError("a node mapping mixin must preserve node hashability")
    return generated
