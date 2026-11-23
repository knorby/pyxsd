"""The schemaless family of lexical nodes and revisions."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

from pyxsd._lexical import LexicalElement, LexicalRevision
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import XML_NS
from pyxsd.schemaless.errors import ScalarTypeError
from pyxsd.schemaless.types import _PARSE_ERRORS, ScalarAdapter, TypePolicy, _infer_adapter
from pyxsd.xsd_data_types import _NCNAME, Integer, qname_context

_STEP = re.compile(rf"/(@)?((?:\{{[^}}]+\}})?{_NCNAME})(?:\[([1-9][0-9]*)\])?")


@dataclass(frozen=True, slots=True)
class _XmlPath:
    steps: tuple[tuple[str, int | None], ...]
    attribute: str | None = None

    @classmethod
    def parse(cls, path: str) -> _XmlPath:
        """Read an absolute occurrence path without splitting namespace URIs."""
        steps: list[tuple[str, int | None]] = []
        offset = 0
        while offset < len(path):
            match = _STEP.match(path, offset)
            if match is None:
                break
            attribute, name, index = match.groups()
            offset = match.end()
            if attribute:
                if steps and offset == len(path) and index is None:
                    return cls(_occurrence_steps(steps), name)
                break
            steps.append((name, Integer(index) if index is not None else None))
        else:
            if steps:
                return cls(_occurrence_steps(steps))
        raise ValueError(f"invalid XML scalar path {path!r}")

    @property
    def indexed(self) -> bool:
        return self.steps[0][1] is not None

    def matches(self, path: tuple[PathStep, ...], attribute: str | None) -> bool:
        return (
            self.attribute == attribute
            and len(path) == len(self.steps)
            and all(
                name == actual.name and index in (None, actual.index)
                for (name, index), actual in zip(self.steps, path, strict=True)
            )
        )

    def values(
        self, root: SchemalessElement
    ) -> Iterator[tuple[SchemalessElement, tuple[PathStep, ...], str | None]]:
        """Find the matching lexical values and their exact paths in document order."""
        name, index = self.steps[0]
        if name != root._name_ or index not in (None, 1):
            return
        level: list[tuple[SchemalessElement, tuple[PathStep, ...]]] = [(root, (PathStep(name, 1),))]
        for name, index in self.steps[1:]:
            level = [
                (child, (*path, PathStep(name, position)))
                for parent, path in level
                for position, child in enumerate(_named_children(parent, name), start=1)
                if index in (None, position)
            ]
        for node, path in level:
            value = _lexical_value(node, self.attribute)
            if self.attribute is None or value is not None:
                yield node, path, value


def _occurrence_steps(steps: list[tuple[str, int | None]]) -> tuple[tuple[str, int | None], ...]:
    # An indexed path addresses one occurrence; omitted indexes default to one.
    if any(index is not None for _, index in steps):
        return tuple((name, index or 1) for name, index in steps)
    return tuple(steps)


def _named_children(node: SchemalessElement, name: str) -> Iterator[SchemalessElement]:
    return (child for child in node._children_ if child._name_ == name)


def _current_path(node: SchemalessElement) -> tuple[SchemalessElement, tuple[PathStep, ...]]:
    """Build an occurrence path from the editable graph, not recorded provenance."""
    steps: list[PathStep] = []
    while (parent := node.parent) is not None:
        index = next(
            index
            for index, sibling in enumerate(_named_children(parent, node._name_), start=1)
            if sibling is node
        )
        steps.append(PathStep(node._name_, index))
        node = parent
    steps.append(PathStep(node._name_, 1))
    return node, tuple(reversed(steps))


def _lexical_value(node: SchemalessElement, attribute: str | None) -> str | None:
    return node.text_value() if attribute is None else node.attribute_lexical(attribute)


def _pinned_value(
    node: SchemalessElement,
    path: tuple[PathStep, ...],
    attribute: str | None,
    lexical: str | None,
    adapter: ScalarAdapter,
) -> Any:
    try:
        with qname_context({"xml": XML_NS, **(node.namespaces or {})}):
            return adapter.parse(lexical or "")
    except _PARSE_ERRORS as exc:
        source = node.source_location
        location = IssueLocation(source.source if source is not None else None, path, attribute)
        raise ScalarTypeError(str(exc), location=location) from exc


class SchemalessRevision(LexicalRevision):
    """One authoritative lexical revision with a replaceable immutable policy."""

    __slots__ = ("_types", "_xml_pins")

    def __init__(self) -> None:
        super().__init__()
        self._types = TypePolicy.lexical()
        self._xml_pins: tuple[tuple[_XmlPath, ScalarAdapter], ...] = ()
        self._attach_typed_snapshot(self)

    @property
    def types(self) -> TypePolicy:
        """The current immutable scalar policy."""
        return self._types

    def _set_types(self, policy: TypePolicy) -> None:
        pins = [(_XmlPath.parse(path), adapter) for path, adapter in policy.pins.items()]
        # An occurrence pin wins over a path-wide pin; the last change wins ties.
        ranked = sorted(
            enumerate(pins), key=lambda item: (item[1][0].indexed, item[0]), reverse=True
        )
        self._xml_pins = tuple(pin for _, pin in ranked)
        self._types = policy

    def scalar_value(self, node: SchemalessElement) -> Any:
        return self._value(node, None, node.text_value())

    def attribute_value(self, node: SchemalessElement, expanded_name: str) -> Any:
        lexical = node.attribute_lexical(expanded_name)
        if lexical is None:
            return None
        return self._value(node, expanded_name, lexical)

    def _value(self, node: SchemalessElement, attribute: str | None, lexical: str | None) -> Any:
        infer = self.types.mode == "infer"
        if not infer and not self._xml_pins:
            return lexical
        root, path = _current_path(node)
        if root is self.root:
            for selector, adapter in self._xml_pins:
                if selector.matches(path, attribute):
                    return _pinned_value(node, path, attribute, lexical, adapter)
        if infer and lexical is not None:
            group = _XmlPath(tuple((step.name, None) for step in path), attribute)
            inferred = _infer_adapter(tuple(value or "" for _, _, value in group.values(root)))
            if inferred is not None:
                return inferred.parse(lexical)
        return lexical


class SchemalessElement(LexicalElement):
    """An editable XML element with no schema binding."""

    __slots__ = ()

    @property
    def revision(self) -> SchemalessRevision:
        """The lexical revision and scalar policy that own this element."""
        return cast(SchemalessRevision, self._revision)

    @classmethod
    def _create_revision(cls) -> SchemalessRevision:
        return SchemalessRevision()
