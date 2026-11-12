"""Source-tree whitespace policy from stylesheet space declarations.

An XSLT source tree is conceptually copied with unwanted whitespace-only text
nodes removed.  This module applies the package's effective ``xsl:strip-space``
and ``xsl:preserve-space`` declarations to one captured document: a
whitespace-only text node is removed only when its parent element is selected
for stripping and no enclosing ``xml:space="preserve"`` protects it.

The caller never loses ownership.  A document that requires no removal is
returned unchanged, so node identity survives; otherwise a structurally
identical copy is returned and the supplied tree is left untouched.  Constructed
result trees and ``xsl:text`` content are outside this policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode

if TYPE_CHECKING:
    from pyxsd.xslt.compiler.static import SpaceRule

__all__ = ["apply_source_space_rules"]

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_XML_WHITESPACE = frozenset(" \t\r\n")

# XPath 3.0 NameTest default priorities (XSLT 3.0 section 4.4).
_ANY_PRIORITY = -0.5
_PARTIAL_PRIORITY = -0.25
_EXACT_PRIORITY = 0.0


@dataclass(frozen=True, slots=True)
class _NameTest:
    """One expanded NameTest from an ``elements`` attribute."""

    namespace: str | None
    local: str | None
    any_namespace: bool
    any_local: bool
    priority: float

    def matches(self, name: str) -> bool:
        if self.any_namespace and self.any_local:
            return True
        if name.startswith("{"):
            namespace, _, local = name[1:].partition("}")
            expanded_namespace: str | None = namespace
        else:
            expanded_namespace, local = None, name
        if not self.any_namespace and expanded_namespace != self.namespace:
            return False
        return self.any_local or local == self.local


@dataclass(frozen=True, slots=True)
class _Rule:
    """One declaration's tests plus the precedence needed to rank matches."""

    tests: tuple[_NameTest, ...]
    preserve: bool
    precedence: tuple[int, ...]
    order: int

    def priority_for(self, name: str) -> float | None:
        priorities = [test.priority for test in self.tests if test.matches(name)]
        if not priorities:
            return None
        return max(priorities)

    @property
    def rank(self) -> tuple[int, tuple[int, ...]]:
        # A shorter precedence prefix is higher; later imports compare larger.
        return (-len(self.precedence), self.precedence)


def _compile_name_test(
    token: str,
    namespaces: Mapping[str, str],
) -> _NameTest | None:
    """Expand one NameTest token, or return ``None`` when its prefix is unbound."""
    if token == "*":
        return _NameTest(None, None, True, True, _ANY_PRIORITY)
    if token.startswith("Q{"):
        raw_namespace, _, local = token[2:].partition("}")
        if not local:
            return None
        expanded_namespace = raw_namespace or None
        if local == "*":
            return _NameTest(expanded_namespace, None, False, True, _PARTIAL_PRIORITY)
        return _NameTest(expanded_namespace, local, False, False, _EXACT_PRIORITY)
    prefix, separator, local = token.partition(":")
    if not separator:
        return _NameTest(None, token, False, False, _EXACT_PRIORITY)
    if prefix == "*":
        if local == "*":
            return _NameTest(None, None, True, True, _ANY_PRIORITY)
        return _NameTest(None, local, True, False, _PARTIAL_PRIORITY)
    namespace = namespaces.get(prefix)
    if namespace is None:
        return None
    if local == "*":
        return _NameTest(namespace, None, False, True, _PARTIAL_PRIORITY)
    return _NameTest(namespace, local, False, False, _EXACT_PRIORITY)


def _compile_rule(rule: SpaceRule, order: int) -> _Rule:
    tests: list[_NameTest] = []
    for token in rule.elements:
        compiled = _compile_name_test(token, rule.namespaces)
        if compiled is not None:
            tests.append(compiled)
    return _Rule(
        tests=tuple(tests),
        preserve=rule.preserve,
        precedence=rule.precedence,
        order=order,
    )


def _best_match(name: str, rules: Sequence[_Rule]) -> _Rule | None:
    """Return the declaration selected by precedence, priority, then order."""
    best: _Rule | None = None
    best_key: tuple[int, tuple[int, ...], float, int] | None = None
    for rule in rules:
        priority = rule.priority_for(name)
        if priority is None:
            continue
        key = (*rule.rank, priority, rule.order)
        if best_key is None or key > best_key:
            best = rule
            best_key = key
    return best


def _preserves_whitespace(name: str, rules: Sequence[_Rule]) -> bool:
    best = _best_match(name, rules)
    return best is None or best.preserve


def _xml_space_preserve(element: XdmNode, inherited: bool) -> bool:
    attribute = element.attribute(_XML_SPACE)
    if attribute is None:
        return inherited
    value = attribute.string_value
    if value == "preserve":
        return True
    if value == "default":
        return False
    return inherited


def _is_xml_whitespace(value: str) -> bool:
    return all(character in _XML_WHITESPACE for character in value)


def _selected_text(document: XdmDocument, rules: Sequence[_Rule]) -> set[int]:
    """Return the identities of whitespace-only text nodes selected for removal."""
    selected: set[int] = set()
    stack: list[tuple[XdmNode, bool]] = [(document, False)]
    while stack:
        node, inherited = stack.pop()
        if node.kind is NodeKind.ELEMENT:
            xml_preserve = _xml_space_preserve(node, inherited)
            preserve = xml_preserve or _preserves_whitespace(node.name or "", rules)
            for child in node.children:
                if child.kind is NodeKind.TEXT and _is_xml_whitespace(child.string_value):
                    if not preserve:
                        selected.add(id(child))
                    continue
                stack.append((child, xml_preserve))
        else:
            # A text node directly under the document node has no parent
            # element name to select, so the declaration policy leaves it.
            stack.extend((child, inherited) for child in node.children)
    return selected


def _clone_node(
    source: XdmNode,
    *,
    document: XdmDocument,
    parent: XdmNode,
    selected: set[int],
) -> XdmNode:
    """Clone one node subtree, omitting the text nodes in *selected*."""
    clone = XdmNode(
        source.kind,
        document=document,
        node_id=source.node_id,
        source_name=source.document.source_name,
        source_index=source.document.source_index,
        parent=parent,
        name=source.name,
        value=source._value,
        target=source.target,
        annotations=source.annotation,
        base_uri=source.base_uri,
        document_uri=source.document_uri,
        in_scope_namespaces=source.in_scope_namespaces,
        namespace_declarations=source.namespace_declarations,
        path=source._path,
        prefix=source.prefix,
        namespace_uri=source.namespace_uri,
    )
    for attribute in source.attributes:
        clone._append_attribute(
            _clone_node(attribute, document=document, parent=clone, selected=selected)
        )
    for namespace in source.namespace_nodes:
        clone._append_namespace_node(
            _clone_node(namespace, document=document, parent=clone, selected=selected)
        )
    for child in source.children:
        if id(child) in selected:
            continue
        clone._append_child(_clone_node(child, document=document, parent=clone, selected=selected))
    return clone


def _filtered_clone(document: XdmDocument, selected: set[int]) -> XdmDocument:
    clone = XdmDocument(
        source_name=document.source_name,
        source_index=document.source_index,
        document_uri=document.document_uri,
        base_uri=document.base_uri,
        schema_pool=document.schema_pool,
    )
    for child in document.children:
        cloned = _clone_node(child, document=clone, parent=clone, selected=selected)
        clone._append_child(cloned)
        if cloned.kind is NodeKind.ELEMENT and clone._root_element is None:
            clone._root_element = cloned
    clone._finalize_document_order()
    return clone


def apply_source_space_rules(
    document: XdmDocument,
    *,
    rules: Sequence[SpaceRule],
) -> XdmDocument:
    """Apply *rules* to *document* and return the invocation's source tree.

    Only whitespace-only text nodes whose parent element is selected for
    stripping, and that no enclosing ``xml:space="preserve"`` protects, are
    removed.  The supplied document is never mutated: when removal is required
    a copy with identical non-removed node content is returned, and otherwise
    the original object is returned unchanged.
    """
    if not isinstance(document, XdmDocument):
        raise TypeError("source space rules require an XdmDocument")
    compiled = tuple(_compile_rule(rule, order) for order, rule in enumerate(rules))
    if not compiled:
        return document
    selected = _selected_text(document, compiled)
    if not selected:
        return document
    return _filtered_clone(document, selected)
