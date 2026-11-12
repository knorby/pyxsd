"""Transactional in-memory construction of XSLT result sequences and trees.

A :class:`ResultBuilder` owns one document node and the stack of open elements
below it.  Every mutation is a legal XSLT construction step: element nesting,
attributes and namespaces before child content, duplicate attribute
replacement, namespace fixup, adjacent text merging, and direct XDM node
copying.  Nodes are built through :mod:`pyxsd.xdm`; nothing here serializes a
tree or reparses it to move a value across an instruction boundary.

:class:`TransactionExtent` snapshots the builder at one construction boundary.
The snapshot is the same ``rollback`` protocol the Task 17 scheduler invokes
for heap-resident frames, so an abandoned construction extent discards every
node, namespace binding, attribute, text mutation, and document-order change it
made while leaving earlier final and temporary output state intact.

The module also hosts the low-level node factory and sequence-normalization
entry point used by :mod:`pyxsd.xslt.results`.  One implementation therefore
governs both final-result-tree wrapping and intermediate-tree construction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, cast

from pyxsd.source_spans import SourceIndex
from pyxsd.xdm import XdmArray, XdmAtomic, XdmSequence
from pyxsd.xdm.nodes import NodeKind, XdmAnnotation, XdmDocument, XdmNode
from pyxsd.xdm.validation import (
    ConstructedValidationError,
    ValidationRequest,
    validate_document,
    validate_occurrence,
)
from pyxsd.xslt.errors import XsltDynamicError

__all__ = [
    "XMLNS_NS",
    "XML_NS",
    "ResultBuilder",
    "TransactionExtent",
    "build_result_tree",
    "validate_constructed",
    "validate_delivered",
    "validation_code",
]

_VALIDATION_CODES = {
    "invalid": "XTTE1510",
    "unknown": "XTTE1512",
    "lax-invalid": "XTTE1515",
    "type": "XTTE1540",
    "qname": "XTTE1545",
    "document-shape": "XTTE1550",
    "document-constraint": "XTTE1555",
    "copy-type": "XTTE1535",
}

XML_NS = "http://www.w3.org/XML/1998/namespace"
XMLNS_NS = "http://www.w3.org/2000/xmlns/"

_RESULT_SOURCE = "<result>"
_RESULT_INDEX = SourceIndex(_RESULT_SOURCE, b"<result/>")
_DEFAULT_ANNOTATION = XdmAnnotation(None, None)
_CONSTRUCTED_ANNOTATIONS = {
    NodeKind.ELEMENT: XdmAnnotation("{http://www.w3.org/2001/XMLSchema}untyped", False),
    NodeKind.ATTRIBUTE: XdmAnnotation("{http://www.w3.org/2001/XMLSchema}untypedAtomic", None),
    NodeKind.TEXT: XdmAnnotation("{http://www.w3.org/2001/XMLSchema}untypedAtomic", None),
}
_NCNAME = re.compile(r"^[^\W\d][\w.\-]*$", re.UNICODE)


def _dynamic(message: str, code: str) -> XsltDynamicError:
    return XsltDynamicError(message, code=code)


def validation_code(error: ConstructedValidationError) -> str:
    """The exact XSLT error code for one constructed-validation failure."""
    return _VALIDATION_CODES[error.reason]


def _raise_validation_error(error: ConstructedValidationError) -> None:
    raise XsltDynamicError(
        str(error),
        code=validation_code(error),
        cause=error,
    ) from error


def validate_constructed(
    node: XdmNode,
    *,
    validation: str = "strip",
    type_name: str | None = None,
    schema_pool: Any = None,
    operation: str = "element",
) -> XdmNode:
    """Validate one constructed node and return it unchanged.

    This is the construction boundary used by the builder, the instruction
    layer, and the processor.  The node is mutated in place with its schema
    annotation and any defaulted attributes/nodes; no serialized result is
    validated.
    """
    request = ValidationRequest(validation, type_name)
    try:
        if node.kind is NodeKind.DOCUMENT:
            validate_document(cast(XdmDocument, node), request, schema_pool=schema_pool)
        else:
            validate_occurrence(
                node,
                request,
                schema_pool=schema_pool,
                operation=operation,  # type: ignore[arg-type]
            )
    except ConstructedValidationError as error:
        _raise_validation_error(error)
    return node


def validate_delivered(
    document: XdmDocument,
    *,
    validation: str = "strip",
    type_name: str | None = None,
    schema_pool: Any = None,
) -> XdmDocument:
    """Apply delivery-boundary validation to a normalized document."""
    request = ValidationRequest(validation, type_name)
    try:
        validate_document(document, request, schema_pool=schema_pool)
    except ConstructedValidationError as error:
        _raise_validation_error(error)
    return document


def _is_ncname(value: str) -> bool:
    return bool(value) and _NCNAME.fullmatch(value) is not None


def _expanded_name(name: str, *, code: str) -> tuple[str | None, str]:
    """Split a Clark name or lexical QName, rejecting malformed names."""
    if not isinstance(name, str) or not name:
        raise _dynamic("a constructed name must be a non-empty string", code)
    if name.startswith("{"):
        uri, separator, local = name[1:].partition("}")
        if not separator or not _is_ncname(local):
            raise _dynamic(f"the constructed name {name!r} is not a valid QName", code)
        return (uri or None, local)
    if not _is_ncname(name):
        raise _dynamic(f"the constructed name {name!r} is not a valid QName", code)
    return (None, name)


def _normalize_prefix(prefix: str) -> str:
    return "" if prefix in ("", "#default") else prefix


def _prefix_for(namespace: str | None, namespaces: Mapping[str, str]) -> str:
    if namespace is None:
        return ""
    default = namespaces.get("")
    if default == namespace:
        return ""
    for prefix, uri in namespaces.items():
        if uri == namespace and prefix:
            return prefix
    return ""


def _attribute_prefix(namespace: str | None, namespaces: Mapping[str, str]) -> str:
    if namespace is None:
        return ""
    for prefix, uri in namespaces.items():
        if uri == namespace and prefix:
            return prefix
    return ""


def _new_prefix(namespaces: dict[str, str], namespace: str) -> str:
    index = 1
    while f"ns{index}" in namespaces:
        index += 1
    prefix = f"ns{index}"
    namespaces[prefix] = namespace
    return prefix


def _aliased_namespaces(
    namespaces: Mapping[str, str] | None,
    aliases: Mapping[str, str] | None,
) -> dict[str, str]:
    """Apply ``xsl:namespace-alias`` prefix remapping to one literal scope."""
    result: dict[str, str] = {}
    alias_map = {
        _normalize_prefix(prefix): _normalize_prefix(target)
        for prefix, target in (aliases or {}).items()
    }
    for prefix, uri in (namespaces or {}).items():
        target_prefix = alias_map.get(_normalize_prefix(prefix), _normalize_prefix(prefix))
        if target_prefix == "xmlns" or uri == XMLNS_NS:
            raise _dynamic(
                "the xmlns namespace cannot be constructed as a namespace node",
                "XTDE0905",
            )
        if target_prefix == "xml" and uri != XML_NS:
            raise _dynamic(
                "the xml prefix must be bound to the XML namespace",
                "XTDE0925",
            )
        if uri == XML_NS and target_prefix != "xml":
            raise _dynamic(
                "the XML namespace must use the xml prefix",
                "XTDE0925",
            )
        existing = result.get(target_prefix)
        if existing is not None and existing != uri:
            raise _dynamic(
                f"namespace prefix {target_prefix!r} is bound to two different URIs",
                "XTDE0430",
            )
        result[target_prefix] = uri
    return result


def _prepare_scope(
    namespaces: Mapping[str, str] | None,
    aliases: Mapping[str, str] | None,
    element_namespace: str | None,
) -> dict[str, str]:
    scope = _aliased_namespaces(namespaces, aliases)
    scope.setdefault("xml", XML_NS)
    if element_namespace is None:
        scope.pop("", None)
    elif not any(uri == element_namespace for uri in scope.values()):
        if "" not in scope:
            scope[""] = element_namespace
        else:
            _new_prefix(scope, element_namespace)
    return scope


def _declarations(scope: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple((prefix, uri) for prefix, uri in scope.items() if prefix != "xml")


def _sanitize_comment(value: str) -> str:
    result: list[str] = []
    for index, character in enumerate(value):
        result.append(character)
        if character == "-" and (index + 1 == len(value) or value[index + 1] == "-"):
            result.append(" ")
    return "".join(result)


def _sanitize_pi(value: str) -> str:
    return value.lstrip(" ").replace("?>", "? >")


def _validate_namespace(prefix: str, uri: str, *, element_namespace: str | None) -> None:
    if prefix not in ("", "xml") and not _is_ncname(prefix):
        raise _dynamic(f"the namespace prefix {prefix!r} is not an NCName", "XTDE0920")
    if prefix == "xmlns":
        raise _dynamic("the xmlns prefix cannot be used for a namespace node", "XTDE0920")
    if not isinstance(uri, str) or not uri:
        raise _dynamic("a namespace node requires a non-empty URI", "XTDE0930")
    if uri == XMLNS_NS:
        raise _dynamic("the xmlns namespace cannot be bound", "XTDE0905")
    if prefix == "xml" and uri != XML_NS:
        raise _dynamic("the xml prefix must be bound to the XML namespace", "XTDE0925")
    if uri == XML_NS and prefix != "xml":
        raise _dynamic("the XML namespace must use the xml prefix", "XTDE0925")
    if prefix == "" and element_namespace is None:
        raise _dynamic(
            "a default namespace cannot be declared on an element in no namespace",
            "XTDE0440",
        )


class _NodeFactory:
    """One constructed document and the nodes that belong to it."""

    def __init__(
        self,
        *,
        document_uri: str | None = None,
        base_uri: str | None = None,
        schema_pool: object = None,
        document: XdmDocument | None = None,
    ) -> None:
        if document is None:
            document = XdmDocument(
                source_name=_RESULT_SOURCE,
                source_index=_RESULT_INDEX,
                document_uri=document_uri,
                base_uri=base_uri if base_uri is not None else document_uri,
                schema_pool=schema_pool,
            )
        self.document = document
        self._next_id = 1

    def _new(
        self,
        kind: NodeKind,
        *,
        parent: XdmNode | None,
        name: str | None = None,
        value: str = "",
        target: str | None = None,
        base_uri: str | None = None,
        in_scope_namespaces: Mapping[str, str] | None = None,
        namespace_declarations: tuple[tuple[str, str | None], ...] = (),
        prefix: str = "",
        namespace_uri: str | None = None,
        annotations: XdmAnnotation | None = None,
    ) -> XdmNode:
        if annotations is None:
            annotations = _CONSTRUCTED_ANNOTATIONS.get(kind, _DEFAULT_ANNOTATION)
        node = XdmNode(
            kind,
            document=self.document,
            node_id=self._next_id,
            source_name=_RESULT_SOURCE,
            source_index=_RESULT_INDEX,
            parent=parent,
            name=name,
            value=value,
            target=target,
            annotations=annotations,
            base_uri=base_uri if base_uri is not None else self.document.base_uri,
            document_uri=self.document.document_uri,
            in_scope_namespaces=in_scope_namespaces or {},
            namespace_declarations=namespace_declarations,
            prefix=prefix,
            namespace_uri=namespace_uri,
        )
        self._next_id += 1
        return node

    def element(
        self,
        name: str,
        *,
        attributes: Iterable[tuple[str, str]] = (),
        namespaces: Mapping[str, str] | None = None,
        base_uri: str | None = None,
        namespace_aliases: Mapping[str, str] | None = None,
        annotation: XdmAnnotation | None = None,
        prefix: str | None = None,
    ) -> XdmNode:
        """Create one detached element with namespace fixup applied."""
        element_namespace, _ = _expanded_name(name, code="XTDE0820")
        attribute_list = tuple(attributes)
        scope = _prepare_scope(namespaces, namespace_aliases, element_namespace)
        for attribute_name, _ in attribute_list:
            attribute_namespace, _ = _expanded_name(attribute_name, code="XTDE0850")
            if attribute_namespace is None:
                continue
            if attribute_namespace == XMLNS_NS:
                raise _dynamic(
                    "an attribute cannot be in the xmlns namespace",
                    "XTDE0850",
                )
            if (
                attribute_namespace != XML_NS
                and _attribute_prefix(attribute_namespace, scope) == ""
            ):
                _new_prefix(scope, attribute_namespace)
        selected_prefix = prefix if prefix is not None else _prefix_for(element_namespace, scope)
        if selected_prefix and scope.get(selected_prefix) != element_namespace:
            selected_prefix = _prefix_for(element_namespace, scope)
        declarations = _declarations(scope)
        element = self._new(
            NodeKind.ELEMENT,
            parent=None,
            name=name,
            base_uri=base_uri,
            in_scope_namespaces=scope,
            namespace_declarations=declarations,
            prefix=selected_prefix,
            namespace_uri=element_namespace,
            annotations=annotation,
        )
        for attribute_name, value in attribute_list:
            attribute_namespace, _ = _expanded_name(attribute_name, code="XTDE0850")
            attribute_prefix = _attribute_prefix(attribute_namespace, scope)
            self.attach_attribute(
                element,
                self.attribute(
                    attribute_name,
                    value,
                    parent=element,
                    prefix=attribute_prefix,
                    in_scope_namespaces=scope,
                ),
            )
        for declared_prefix, uri in declarations:
            element._append_namespace_node(
                self.namespace(declared_prefix, uri, parent=element, scope=scope)
            )
        element._append_namespace_node(self.namespace("xml", XML_NS, parent=element, scope=scope))
        return element

    def attribute(
        self,
        name: str,
        value: str,
        *,
        parent: XdmNode | None,
        prefix: str = "",
        base_uri: str | None = None,
        in_scope_namespaces: Mapping[str, str] | None = None,
        annotations: XdmAnnotation | None = None,
    ) -> XdmNode:
        namespace, _ = _expanded_name(name, code="XTDE0850")
        if namespace is None and name == "xmlns":
            raise _dynamic("an attribute cannot be named xmlns", "XTDE0855")
        if namespace == XMLNS_NS:
            raise _dynamic("an attribute cannot be in the xmlns namespace", "XTDE0850")
        if namespace is None:
            prefix = ""
        return self._new(
            NodeKind.ATTRIBUTE,
            parent=parent,
            name=name,
            value=value,
            base_uri=base_uri,
            in_scope_namespaces=in_scope_namespaces,
            prefix=prefix,
            namespace_uri=namespace,
            annotations=annotations,
        )

    def namespace(
        self,
        prefix: str,
        uri: str,
        *,
        parent: XdmNode | None,
        scope: Mapping[str, str] | None = None,
        annotations: XdmAnnotation | None = None,
    ) -> XdmNode:
        return self._new(
            NodeKind.NAMESPACE,
            parent=parent,
            name=prefix or None,
            value=uri,
            in_scope_namespaces=scope,
            prefix=prefix,
            annotations=annotations,
        )

    def text(
        self,
        value: str,
        *,
        parent: XdmNode | None = None,
        base_uri: str | None = None,
        annotations: XdmAnnotation | None = None,
    ) -> XdmNode:
        return self._new(
            NodeKind.TEXT,
            parent=parent,
            value=value,
            base_uri=base_uri,
            annotations=annotations,
        )

    def comment(
        self,
        value: str,
        *,
        parent: XdmNode | None = None,
        base_uri: str | None = None,
    ) -> XdmNode:
        return self._new(
            NodeKind.COMMENT,
            parent=parent,
            value=_sanitize_comment(value),
            base_uri=base_uri,
        )

    def processing_instruction(
        self,
        target: str,
        value: str,
        *,
        parent: XdmNode | None = None,
        base_uri: str | None = None,
    ) -> XdmNode:
        return self._new(
            NodeKind.PROCESSING_INSTRUCTION,
            parent=parent,
            name=target,
            value=_sanitize_pi(value),
            target=target,
            base_uri=base_uri,
        )

    def clone(
        self,
        source: XdmNode,
        *,
        parent: XdmNode | None,
        deep: bool = True,
        copy_namespaces: bool = True,
    ) -> XdmNode:
        """Copy one node (and its subtree) into this factory's document."""
        if source.kind is NodeKind.DOCUMENT:
            return self._clone_document(
                source,
                deep=deep,
                copy_namespaces=copy_namespaces,
            )
        if source.kind is NodeKind.ELEMENT:
            scope, declarations = self._copy_scope(
                source,
                copy_namespaces=copy_namespaces,
            )
            node = self._new(
                NodeKind.ELEMENT,
                parent=parent,
                name=source.name,
                base_uri=source.base_uri,
                in_scope_namespaces=scope,
                namespace_declarations=declarations,
                prefix=source.prefix,
                namespace_uri=source.namespace_uri,
                annotations=source.annotation,
            )
            for attribute in source.attributes:
                cloned_attribute = self.clone(attribute, parent=node)
                if not copy_namespaces and cloned_attribute.namespace_uri is not None:
                    cloned_attribute._prefix = _attribute_prefix(
                        cloned_attribute.namespace_uri,
                        scope,
                    )
                cloned_attribute._in_scope_namespaces = MappingProxyType(scope)
                node._append_attribute(cloned_attribute)
            if copy_namespaces:
                for namespace in source.namespace_nodes:
                    node._append_namespace_node(self.clone(namespace, parent=node))
            else:
                for declared_prefix, uri in _declarations(scope):
                    node._append_namespace_node(
                        self.namespace(declared_prefix, uri, parent=node, scope=scope)
                    )
                node._append_namespace_node(self.namespace("xml", XML_NS, parent=node, scope=scope))
            if deep:
                for child in source.children:
                    node._append_child(
                        self.clone(
                            child,
                            parent=node,
                            deep=True,
                            copy_namespaces=copy_namespaces,
                        )
                    )
            return node
        value = source.string_value
        return self._new(
            source.kind,
            parent=parent,
            name=source.name,
            value=value,
            target=source.target,
            base_uri=source.base_uri,
            in_scope_namespaces=source.in_scope_namespaces,
            prefix=source.prefix,
            namespace_uri=source.namespace_uri,
            annotations=source.annotation,
        )

    def _clone_document(
        self,
        source: XdmNode,
        *,
        deep: bool,
        copy_namespaces: bool,
    ) -> XdmDocument:
        document = XdmDocument(
            source_name=_RESULT_SOURCE,
            source_index=_RESULT_INDEX,
            document_uri=source.document_uri,
            base_uri=source.base_uri,
            schema_pool=getattr(source, "schema_pool", None),
        )
        factory = _NodeFactory(document=document)
        if deep:
            for child in source.children:
                clone = factory.clone(
                    child,
                    parent=document,
                    deep=True,
                    copy_namespaces=copy_namespaces,
                )
                document._append_child(clone)
                if document._root_element is None and clone.kind is NodeKind.ELEMENT:
                    document._root_element = clone
            document._finalize_document_order()
        return document

    def _copy_scope(
        self,
        source: XdmNode,
        *,
        copy_namespaces: bool,
    ) -> tuple[dict[str, str], tuple[tuple[str, str | None], ...]]:
        if copy_namespaces:
            copied_scope = dict(source.in_scope_namespaces)
            copied_scope.setdefault("xml", XML_NS)
            return copied_scope, source.namespace_declarations
        reduced: dict[str, str] = {"xml": XML_NS}
        namespace = source.namespace_uri
        if namespace is not None:
            if source.prefix and source.prefix != "xml":
                reduced[source.prefix] = namespace
            else:
                reduced[""] = namespace
        for attribute in source.attributes:
            attribute_namespace = attribute.namespace_uri
            if attribute_namespace is None:
                continue
            if attribute_namespace == XML_NS:
                reduced["xml"] = XML_NS
                continue
            if _attribute_prefix(attribute_namespace, reduced):
                continue
            if attribute.prefix and attribute.prefix not in reduced:
                reduced[attribute.prefix] = attribute_namespace
            else:
                _new_prefix(reduced, attribute_namespace)
        return reduced, _declarations(reduced)

    def finalize(self) -> XdmDocument:
        self.document._finalize_document_order()
        return self.document

    def _append(self, parent: XdmNode, child: XdmNode) -> None:
        child._parent = parent
        parent._append_child(child)
        if (
            parent.kind is NodeKind.DOCUMENT
            and child.kind is NodeKind.ELEMENT
            and self.document._root_element is None
        ):
            self.document._root_element = child

    def attach_attribute(self, parent: XdmNode, attribute: XdmNode) -> None:
        if parent.kind is NodeKind.DOCUMENT:
            raise _dynamic(
                "a document result tree cannot contain an attribute node",
                "XTDE0420",
            )
        if parent.children:
            raise _dynamic(
                "an attribute node must precede other child content",
                "XTDE0410",
            )
        attribute._parent = parent
        attribute._prefix = _ensure_attribute_namespace(
            self,
            parent,
            attribute.namespace_uri,
            attribute.prefix,
        )
        attribute._in_scope_namespaces = parent.in_scope_namespaces
        for index, existing in enumerate(parent._attributes):
            if existing.name == attribute.name:
                parent._attributes[index] = attribute
                return
        parent._append_attribute(attribute)

    def attach_namespace(self, parent: XdmNode, namespace: XdmNode) -> XdmNode | None:
        if parent.kind is NodeKind.DOCUMENT:
            raise _dynamic(
                "a document result tree cannot contain a namespace node",
                "XTDE0420",
            )
        if parent.children:
            raise _dynamic(
                "a namespace node must precede other child content",
                "XTDE0410",
            )
        prefix = namespace.prefix
        uri = namespace.namespace_binding
        assert uri is not None
        _validate_namespace(prefix, uri, element_namespace=parent.namespace_uri)
        namespace._parent = parent
        existing_uri = parent.in_scope_namespaces.get(prefix)
        if existing_uri is not None:
            if existing_uri == uri:
                existing_node = parent.namespace_node(prefix)
                assert existing_node is not None
                return existing_node
            if parent.prefix == prefix and parent.namespace_uri is not None:
                self._fixup_element_prefix(parent)
                return self._declare_namespace(parent, prefix, uri)
            raise _dynamic(
                f"namespace prefix {prefix!r} is bound to two different URIs",
                "XTDE0430",
            )
        return self._declare_namespace(parent, prefix, uri, node=namespace)

    def _fixup_element_prefix(self, parent: XdmNode) -> None:
        namespace = parent.namespace_uri
        assert namespace is not None
        old_prefix = parent.prefix
        replacement = ""
        for prefix, uri in parent.in_scope_namespaces.items():
            if prefix and prefix != "xml" and prefix != old_prefix and uri == namespace:
                replacement = prefix
                break
        scope = dict(parent.in_scope_namespaces)
        replacement_existed = bool(replacement)
        if not replacement:
            replacement = _new_prefix(scope, namespace)
        scope.pop(old_prefix, None)
        survivors: list[XdmNode] = []
        renamed = False
        for namespace_node in parent._namespace_nodes:
            if namespace_node.prefix != old_prefix:
                survivors.append(namespace_node)
                continue
            if replacement_existed or renamed:
                continue
            namespace_node._prefix = replacement
            namespace_node._name = replacement or None
            survivors.append(namespace_node)
            renamed = True
        parent._namespace_nodes[:] = survivors
        for attribute in parent._attributes:
            if attribute.prefix == old_prefix and attribute.namespace_uri == namespace:
                attribute._prefix = replacement
        parent._prefix = replacement
        parent._in_scope_namespaces = MappingProxyType(scope)
        parent._namespace_declarations = _declarations(scope)

    def _declare_namespace(
        self,
        parent: XdmNode,
        prefix: str,
        uri: str,
        *,
        node: XdmNode | None = None,
    ) -> XdmNode:
        scope = dict(parent.in_scope_namespaces)
        scope.setdefault("xml", XML_NS)
        scope[prefix] = uri
        parent._in_scope_namespaces = MappingProxyType(scope)
        parent._namespace_declarations = _declarations(scope)
        if node is None:
            node = self.namespace(prefix, uri, parent=parent, scope=scope)
        node._parent = parent
        parent._append_namespace_node(node)
        return node


def _append_text(factory: _NodeFactory, parent: XdmNode, value: str) -> XdmNode | None:
    if not value:
        return None
    children = parent.children
    if children and children[-1].kind is NodeKind.TEXT:
        children[-1]._append_text(value)
        return children[-1]
    text = factory.text(value, parent=parent, base_uri=parent.base_uri)
    factory._append(parent, text)
    return text


def _flatten_arrays(sequence: Iterable[object]) -> Iterable[object]:
    """Recursively replace arrays by their members, as XSLT 5.7.1 requires."""
    for item in sequence:
        if isinstance(item, XdmArray):
            for member in item.members:
                yield from _flatten_arrays(member)
        else:
            yield item


def _inherit_namespaces(
    factory: _NodeFactory,
    element: XdmNode,
    inherited: Mapping[str, str],
) -> None:
    """Copy inherited namespace nodes down one constructed subtree."""
    scope = dict(element.in_scope_namespaces)
    changed = False
    for prefix, uri in inherited.items():
        if prefix == "xml" or prefix in scope:
            continue
        if prefix == "" and element.namespace_uri is None:
            continue
        scope[prefix] = uri
        element._append_namespace_node(factory.namespace(prefix, uri, parent=element, scope=scope))
        changed = True
    if changed:
        element._in_scope_namespaces = MappingProxyType(scope)
        element._namespace_declarations = _declarations(scope)
    child_scope = element.in_scope_namespaces
    for child in element.children:
        if child.kind is NodeKind.ELEMENT:
            _inherit_namespaces(factory, child, child_scope)


def _attach_element(
    factory: _NodeFactory,
    parent: XdmNode,
    element: XdmNode,
    *,
    inherit_namespaces: bool = True,
) -> None:
    if inherit_namespaces and parent.kind is NodeKind.ELEMENT:
        _inherit_namespaces(factory, element, parent.in_scope_namespaces)
    factory._append(parent, element)


def _clone_for_content(
    factory: _NodeFactory,
    item: XdmNode,
    parent: XdmNode,
    *,
    copy_namespaces: bool = True,
) -> XdmNode:
    return factory.clone(
        item,
        parent=parent,
        deep=True,
        copy_namespaces=copy_namespaces,
    )


def _append_complex_content(
    factory: _NodeFactory,
    parent: XdmNode,
    sequence: Iterable[object],
    *,
    allow_attributes: bool,
    inherit_namespaces: bool = True,
) -> None:
    """Apply XSLT complex-content construction to *sequence* under *parent*."""
    last_atomic = False
    for item in _flatten_arrays(sequence):
        if isinstance(item, XdmAtomic):
            value = item.lexical
            if last_atomic:
                value = " " + value
            _append_text(factory, parent, value)
            last_atomic = True
            continue
        if not isinstance(item, XdmNode):
            raise _dynamic(
                "a result sequence cannot contain a function item",
                "XTDE0450",
            )
        if item.kind is NodeKind.DOCUMENT:
            _append_complex_content(
                factory,
                parent,
                item.children,
                allow_attributes=allow_attributes,
                inherit_namespaces=inherit_namespaces,
            )
            last_atomic = False
            continue
        if item.kind in (NodeKind.ATTRIBUTE, NodeKind.NAMESPACE):
            if not allow_attributes or parent.kind is NodeKind.DOCUMENT:
                raise _dynamic(
                    "a document result tree cannot contain attribute or namespace nodes",
                    "XTDE0420",
                )
            if parent.children:
                raise _dynamic(
                    "an attribute or namespace node must precede other child content",
                    "XTDE0410",
                )
            if item.kind is NodeKind.ATTRIBUTE:
                clone = factory.clone(item, parent=parent)
                factory.attach_attribute(parent, clone)
            else:
                clone = factory.clone(item, parent=parent)
                factory.attach_namespace(parent, clone)
            last_atomic = False
            continue
        if item.kind is NodeKind.TEXT:
            _append_text(factory, parent, item.string_value)
            last_atomic = False
            continue
        clone = _clone_for_content(factory, item, parent)
        if clone.kind is NodeKind.ELEMENT:
            _attach_element(factory, parent, clone, inherit_namespaces=inherit_namespaces)
        else:
            factory._append(parent, clone)
        last_atomic = False


def _ensure_attribute_namespace(
    factory: _NodeFactory,
    parent: XdmNode,
    namespace: str | None,
    prefix: str,
) -> str:
    if namespace is None:
        return ""
    if prefix:
        if prefix == "xmlns" or not _is_ncname(prefix):
            raise _dynamic(f"the attribute prefix {prefix!r} is not an NCName", "XTDE0850")
        if prefix == "xml" and namespace != XML_NS:
            raise _dynamic("the xml prefix must be bound to the XML namespace", "XTDE0925")
    if namespace == XML_NS:
        return "xml"
    if prefix and parent.in_scope_namespaces.get(prefix) == namespace:
        return prefix
    existing = _attribute_prefix(namespace, parent.in_scope_namespaces)
    if existing:
        return existing
    selected = prefix
    if not selected or selected in parent.in_scope_namespaces:
        scope = dict(parent.in_scope_namespaces)
        selected = _new_prefix(scope, namespace)
    factory._declare_namespace(parent, selected, namespace)
    return selected


class TransactionExtent:
    """One result-builder snapshot and its rollback boundary.

    The extent is deliberately compatible with the Task 17 frame protocol:
    ``rollback`` is callable and idempotent, so assigning the extent to
    ``Frame.extent`` makes scheduler unwinding discard abandoned construction.
    """

    __slots__ = ("_builder", "_snapshot", "_state")

    def __init__(self, builder: ResultBuilder) -> None:
        self._builder = builder
        self._snapshot = builder._snapshot()
        self._state = "open"

    @property
    def committed(self) -> bool:
        return self._state == "committed"

    @property
    def rolled_back(self) -> bool:
        return self._state == "rolled_back"

    def commit(self) -> None:
        if self._state == "open":
            self._state = "committed"

    def rollback(self) -> None:
        if self._state != "open":
            return
        self._builder._restore(self._snapshot)
        self._state = "rolled_back"

    def __enter__(self) -> TransactionExtent:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        return False


@dataclass(frozen=True, slots=True)
class _ParentSnapshot:
    node: XdmNode
    children: int
    attributes: tuple[XdmNode, ...]
    attribute_prefixes: tuple[str, ...]
    namespace_nodes: tuple[XdmNode, ...]
    namespace_prefixes: tuple[str, ...]
    namespace_names: tuple[str | None, ...]
    scope: Mapping[str, str]
    declarations: tuple[tuple[str, str | None], ...]
    prefix: str
    trailing_text: XdmNode | None
    trailing_text_value: str


@dataclass(frozen=True, slots=True)
class _BuilderSnapshot:
    stack: tuple[XdmNode, ...]
    next_id: int
    root_element: XdmNode | None
    finalized: bool
    parents: tuple[_ParentSnapshot, ...]


class ResultBuilder:
    """One document's transactional XSLT construction state."""

    __slots__ = ("_factory", "_final_output", "_finalized", "_stack")

    def __init__(self, factory: _NodeFactory, *, final_output: bool = True) -> None:
        if not isinstance(final_output, bool):
            raise TypeError("final_output must be a boolean")
        self._factory = factory
        self._final_output = final_output
        self._finalized = False
        self._stack: list[XdmNode] = []

    @classmethod
    def document(
        cls,
        *,
        document_uri: str | None = None,
        base_uri: str | None = None,
        schema_pool: object = None,
        final_output: bool = True,
    ) -> ResultBuilder:
        """Create a builder for one constructed document node."""
        factory = _NodeFactory(
            document_uri=document_uri,
            base_uri=base_uri,
            schema_pool=schema_pool,
        )
        return cls(factory, final_output=final_output)

    @classmethod
    def temporary(
        cls,
        *,
        document_uri: str | None = None,
        base_uri: str | None = None,
        schema_pool: object = None,
    ) -> ResultBuilder:
        """Create a builder whose document is a temporary tree."""
        return cls.document(
            document_uri=document_uri,
            base_uri=base_uri,
            schema_pool=schema_pool,
            final_output=False,
        )

    @property
    def root(self) -> XdmDocument:
        return self._factory.document

    @property
    def current(self) -> XdmNode:
        return self._stack[-1] if self._stack else self.root

    @property
    def final_output(self) -> bool:
        return self._final_output

    @property
    def finalized(self) -> bool:
        return self._finalized

    @property
    def document_uri(self) -> str | None:
        return self.root.document_uri

    @property
    def base_uri(self) -> str | None:
        return self.root.base_uri

    @property
    def schema_pool(self) -> object:
        return self.root.schema_pool

    # -- constructors ------------------------------------------------------

    def element(
        self,
        name: str,
        *,
        attributes: Iterable[tuple[str, str]] = (),
        namespaces: Mapping[str, str] | None = None,
        namespace_aliases: Mapping[str, str] | None = None,
        base_uri: str | None = None,
        annotation: XdmAnnotation | None = None,
        prefix: str | None = None,
        inherit_namespaces: bool = True,
    ) -> XdmNode:
        """Construct an element under the current parent and open it."""
        parent = self.current
        inherited: dict[str, str] = {}
        if inherit_namespaces and parent.kind is NodeKind.ELEMENT:
            inherited = {
                declared_prefix: uri
                for declared_prefix, uri in parent.in_scope_namespaces.items()
                if declared_prefix != "xml"
            }
        inherited.update(namespaces or {})
        element = self._factory.element(
            name,
            attributes=attributes,
            namespaces=inherited,
            base_uri=base_uri,
            namespace_aliases=namespace_aliases,
            annotation=annotation,
            prefix=prefix,
        )
        self._attach_existing(parent, element, inherit_namespaces=inherit_namespaces)
        self._stack.append(element)
        return element

    def end_element(self) -> XdmNode:
        if not self._stack:
            raise XsltDynamicError("no element construction is open", code=None)
        return self._stack.pop()

    def attribute(
        self,
        name: str,
        value: str,
        *,
        prefix: str | None = None,
        annotation: XdmAnnotation | None = None,
    ) -> XdmNode:
        parent = self.current
        if parent.kind is NodeKind.DOCUMENT:
            raise _dynamic(
                "an attribute can only be added to an open element",
                "XTDE0410",
            )
        if parent.children:
            raise _dynamic(
                "an attribute must precede other child content",
                "XTDE0410",
            )
        namespace, _ = _expanded_name(name, code="XTDE0850")
        if namespace is None and name == "xmlns":
            raise _dynamic("an attribute cannot be named xmlns", "XTDE0855")
        selected = _ensure_attribute_namespace(self._factory, parent, namespace, prefix or "")
        attribute = self._factory.attribute(
            name,
            value,
            parent=parent,
            prefix=selected,
            base_uri=parent.base_uri,
            in_scope_namespaces=parent.in_scope_namespaces,
            annotations=annotation,
        )
        self._factory.attach_attribute(parent, attribute)
        return attribute

    def namespace(
        self,
        prefix: str,
        uri: str,
        *,
        annotation: XdmAnnotation | None = None,
    ) -> XdmNode:
        parent = self.current
        if parent.kind is NodeKind.DOCUMENT:
            raise _dynamic(
                "a namespace can only be added to an open element",
                "XTDE0410",
            )
        if parent.children:
            raise _dynamic(
                "a namespace node must precede other child content",
                "XTDE0410",
            )
        prefix = _normalize_prefix(prefix)
        _validate_namespace(prefix, uri, element_namespace=parent.namespace_uri)
        existing = parent.in_scope_namespaces.get(prefix)
        if existing is not None and existing != uri:
            raise _dynamic(
                f"namespace prefix {prefix!r} is bound to two different URIs",
                "XTDE0430",
            )
        if existing is not None:
            node = parent.namespace_node(prefix)
            assert node is not None
            return node
        node = self._factory.namespace(
            prefix,
            uri,
            parent=parent,
            scope=parent.in_scope_namespaces,
            annotations=annotation,
        )
        return self._factory.attach_namespace(parent, node) or node

    def text(
        self,
        value: str,
        *,
        annotation: XdmAnnotation | None = None,
    ) -> XdmNode | None:
        text = _append_text(self._factory, self.current, value)
        if text is not None and annotation is not None:
            text._annotations = annotation
        return text

    def comment(self, value: str) -> XdmNode:
        parent = self.current
        node = self._factory.comment(
            value,
            parent=parent,
            base_uri=parent.base_uri,
        )
        self._factory._append(parent, node)
        return node

    def processing_instruction(self, target: str, value: str) -> XdmNode:
        if not _is_ncname(target) or target.lower() == "xml":
            raise _dynamic(
                f"the processing-instruction target {target!r} is not a valid PITarget",
                "XTDE0890",
            )
        parent = self.current
        node = self._factory.processing_instruction(
            target,
            value,
            parent=parent,
            base_uri=parent.base_uri,
        )
        self._factory._append(parent, node)
        return node

    def append(
        self,
        sequence: Iterable[object],
        *,
        inherit_namespaces: bool = True,
    ) -> None:
        """Attach one sequence as complex content of the current parent."""
        parent = self.current
        _append_complex_content(
            self._factory,
            parent,
            sequence,
            allow_attributes=parent.kind is not NodeKind.DOCUMENT,
            inherit_namespaces=inherit_namespaces,
        )

    def clone(
        self,
        node: XdmNode,
        *,
        deep: bool = True,
        copy_namespaces: bool = True,
    ) -> XdmNode:
        """Copy *node* into this builder's document without attaching it."""
        return self._factory.clone(
            node,
            parent=None,
            deep=deep,
            copy_namespaces=copy_namespaces,
        )

    def copy(
        self,
        node: XdmNode,
        *,
        deep: bool = True,
        copy_namespaces: bool = True,
    ) -> XdmNode:
        """Copy *node* and attach the copy to the current parent."""
        if node.kind is NodeKind.DOCUMENT:
            clone = self.clone(node, deep=deep, copy_namespaces=copy_namespaces)
            if deep:
                self.append(clone.children)
            return clone
        parent = self.current
        clone = self._factory.clone(
            node,
            parent=parent,
            deep=deep,
            copy_namespaces=copy_namespaces,
        )
        self._attach_existing(parent, clone)
        return clone

    # -- detached simple-content nodes ------------------------------------

    def attribute_node(
        self,
        name: str,
        value: str,
        *,
        prefix: str | None = None,
        annotation: XdmAnnotation | None = None,
    ) -> XdmNode:
        namespace, _ = _expanded_name(name, code="XTDE0850")
        if namespace is None and name == "xmlns":
            raise _dynamic("an attribute cannot be named xmlns", "XTDE0855")
        if prefix is not None and prefix and not _is_ncname(prefix):
            raise _dynamic(f"the attribute prefix {prefix!r} is not an NCName", "XTDE0850")
        return self._factory.attribute(
            name,
            value,
            parent=None,
            prefix=prefix or "",
            base_uri=self.root.base_uri,
            annotations=annotation,
        )

    def namespace_node(
        self,
        prefix: str,
        uri: str,
        *,
        annotation: XdmAnnotation | None = None,
    ) -> XdmNode:
        prefix = _normalize_prefix(prefix)
        _validate_namespace(prefix, uri, element_namespace="urn:temporary")
        return self._factory.namespace(
            prefix,
            uri,
            parent=None,
            scope={"xml": XML_NS},
            annotations=annotation,
        )

    def text_node(
        self,
        value: str,
        *,
        annotation: XdmAnnotation | None = None,
    ) -> XdmNode:
        return self._factory.text(
            value,
            parent=None,
            base_uri=self.root.base_uri,
            annotations=annotation,
        )

    def comment_node(self, value: str) -> XdmNode:
        return self._factory.comment(
            value,
            parent=None,
            base_uri=self.root.base_uri,
        )

    def processing_instruction_node(self, target: str, value: str) -> XdmNode:
        if not _is_ncname(target) or target.lower() == "xml":
            raise _dynamic(
                f"the processing-instruction target {target!r} is not a valid PITarget",
                "XTDE0890",
            )
        return self._factory.processing_instruction(
            target,
            value,
            parent=None,
            base_uri=self.root.base_uri,
        )

    # -- results -----------------------------------------------------------

    def to_sequence(self) -> XdmSequence:
        """The constructed top-level content as an XDM sequence."""
        return tuple(self.root.children)

    def to_tree(self) -> XdmDocument:
        """Finalize and return the constructed document node."""
        self._factory.finalize()
        self._finalized = True
        return self.root

    # -- transactions ------------------------------------------------------

    def transaction(self) -> TransactionExtent:
        return TransactionExtent(self)

    def _snapshot(self) -> _BuilderSnapshot:
        parents = [self._snapshot_parent(self.root)]
        parents.extend(self._snapshot_parent(node) for node in self._stack)
        return _BuilderSnapshot(
            stack=tuple(self._stack),
            next_id=self._factory._next_id,
            root_element=self.root._root_element,
            finalized=self._finalized,
            parents=tuple(parents),
        )

    @staticmethod
    def _snapshot_parent(node: XdmNode) -> _ParentSnapshot:
        trailing = None
        if node._children and node._children[-1].kind is NodeKind.TEXT:
            trailing = node._children[-1]
        return _ParentSnapshot(
            node=node,
            children=len(node._children),
            attributes=tuple(node._attributes),
            attribute_prefixes=tuple(item._prefix for item in node._attributes),
            namespace_nodes=tuple(node._namespace_nodes),
            namespace_prefixes=tuple(item._prefix for item in node._namespace_nodes),
            namespace_names=tuple(item._name for item in node._namespace_nodes),
            scope=node._in_scope_namespaces,
            declarations=node._namespace_declarations,
            prefix=node._prefix,
            trailing_text=trailing,
            trailing_text_value="" if trailing is None else trailing._value,
        )

    def _restore(self, snapshot: _BuilderSnapshot) -> None:
        for parent in snapshot.parents:
            node = parent.node
            del node._children[parent.children :]
            node._attributes[:] = parent.attributes
            for attribute, prefix in zip(
                parent.attributes,
                parent.attribute_prefixes,
                strict=True,
            ):
                attribute._prefix = prefix
            node._namespace_nodes[:] = parent.namespace_nodes
            for namespace_node, prefix, name in zip(
                parent.namespace_nodes,
                parent.namespace_prefixes,
                parent.namespace_names,
                strict=True,
            ):
                namespace_node._prefix = prefix
                namespace_node._name = name
            node._in_scope_namespaces = parent.scope
            node._namespace_declarations = parent.declarations
            node._prefix = parent.prefix
            if parent.trailing_text is not None:
                parent.trailing_text._value = parent.trailing_text_value
        self.root._root_element = snapshot.root_element
        self._factory._next_id = snapshot.next_id
        self._finalized = snapshot.finalized
        self._stack[:] = snapshot.stack

    # -- attachment --------------------------------------------------------

    def _attach_existing(
        self,
        parent: XdmNode,
        node: XdmNode,
        *,
        inherit_namespaces: bool = True,
    ) -> None:
        if node.kind is NodeKind.ATTRIBUTE:
            self._factory.attach_attribute(parent, node)
            return
        if node.kind is NodeKind.NAMESPACE:
            self._factory.attach_namespace(parent, node)
            return
        if node.kind is NodeKind.TEXT:
            _append_text(self._factory, parent, node.string_value)
            return
        if node.kind is NodeKind.ELEMENT:
            _attach_element(
                self._factory,
                parent,
                node,
                inherit_namespaces=inherit_namespaces,
            )
            return
        self._factory._append(parent, node)

    # -- final-result-tree wrapping ---------------------------------------

    @classmethod
    def wrap(
        cls,
        sequence: Iterable[object],
        *,
        output_uri: str | None = None,
        base_uri: str | None = None,
        schema_pool: object = None,
        item_separator: str | None = None,
    ) -> XdmDocument:
        """Apply sequence normalization to raw items without reparsing them."""
        builder = cls.document(
            document_uri=output_uri,
            base_uri=base_uri,
            schema_pool=schema_pool,
        )
        items = tuple(_flatten_arrays(sequence))
        if item_separator is None:
            builder.append(items)
        else:
            for index, item in enumerate(items):
                if index:
                    builder.text(item_separator)
                builder.append((item,))
        return builder.to_tree()


def build_result_tree(
    sequence: Iterable[object],
    *,
    output_uri: str | None = None,
    base_uri: str | None = None,
    schema_pool: object = None,
    item_separator: str | None = None,
) -> XdmDocument:
    """Build one final document directly from XDM content, without reparsing."""
    return ResultBuilder.wrap(
        sequence,
        output_uri=output_uri,
        base_uri=base_uri,
        schema_pool=schema_pool,
        item_separator=item_separator,
    )
