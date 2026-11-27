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
from itertools import count
from types import MappingProxyType
from typing import Any, Literal, cast
from urllib.parse import urljoin

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
    "AttributeName",
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

#: Each constructed document receives a disjoint document-order range.  The
#: stride is far larger than any constructible tree, so a temporary tree's
#: positions never meet another tree's; only their relative order matters.
_ORDER_STRIDE = 1 << 32
_ORDER_SEQUENCE = count(1)


def _next_order_base() -> int:
    """The first document-order position of one newly constructed document."""
    return next(_ORDER_SEQUENCE) * _ORDER_STRIDE + 1


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


def _prefix_for(
    namespace: str | None,
    namespaces: Mapping[str, str],
    *,
    is_attribute: bool = False,
) -> str | None:
    """Return a prefix bound to *namespace*, or ``None`` when it is unbound.

    Element names prefer a default binding, because the default namespace
    applies to unprefixed element names.  Attribute names never use it: an
    unprefixed attribute is in no namespace, so an attribute in a namespace
    needs an explicit prefix.  ``""`` is returned for a name in no namespace.
    """
    if namespace is None:
        return ""
    if not is_attribute and namespaces.get("") == namespace:
        return ""
    for prefix, uri in namespaces.items():
        if prefix and uri == namespace:
            return prefix
    return None


def _new_prefix(namespaces: dict[str, str], namespace: str) -> str:
    index = 1
    while f"ns{index}" in namespaces:
        index += 1
    prefix = f"ns{index}"
    namespaces[prefix] = namespace
    return prefix


@dataclass(frozen=True, slots=True)
class AttributeName:
    """One attribute's expanded name and its lexical prefix.

    The expanded name stays a Clark string, matching the rest of the XDM
    layer.  ``prefix`` is the lexical prefix the attribute's QName carried in
    the stylesheet (``""`` when unprefixed); construction preserves it unless
    namespace fixup has to change it for a genuine conflict.
    """

    name: str
    prefix: str = ""


@dataclass(frozen=True, slots=True)
class NamespaceAlias:
    """One compiled ``xsl:namespace-alias`` mapping.

    ``literal_uri`` is the namespace URI used in the stylesheet and
    ``target_uri`` the URI it becomes in the result tree; either may be
    ``None`` for names in no namespace.  ``result_prefix`` is the lexical
    prefix of the ``result-prefix`` declaration (``""`` for ``#default``).
    """

    literal_uri: str | None
    target_uri: str | None
    result_prefix: str


@dataclass(frozen=True, slots=True)
class _PreparedScope:
    """One literal result element's namespace preparation.

    ``copied_namespaces`` is exactly the set of namespace nodes the literal
    element copies from the stylesheet (after exclusions and namespace
    aliasing).  ``available_bindings`` retains every binding usable for naming
    or namespace fixup, including excluded and aliased-literal bindings, so a
    name's required namespace can be reintroduced without copying the rest.
    """

    copied_namespaces: tuple[tuple[str, str], ...]
    available_bindings: Mapping[str, str]
    element_namespace: str | None
    aliases: tuple[NamespaceAlias, ...]

    def alias_for(self, namespace: str | None) -> NamespaceAlias | None:
        for rule in self.aliases:
            if rule.literal_uri == namespace:
                return rule
        return None

    def resolve_namespace(self, namespace: str | None) -> str | None:
        """The result-tree namespace URI for a literal name in *namespace*."""
        rule = self.alias_for(namespace)
        return namespace if rule is None else rule.target_uri

    def preferred_prefix(self, namespace: str | None) -> str | None:
        """The lexical prefix an aliased name takes, otherwise ``None``."""
        rule = self.alias_for(namespace)
        return None if rule is None else rule.result_prefix


def _copy_namespace(copied: dict[str, str], prefix: str, uri: str) -> None:
    """Copy one namespace binding, renaming it only on a genuine conflict.

    A prefix bound to another URI is a real collision: both namespace nodes
    have to survive namespace fixup, so the later one takes a deterministic
    generated prefix instead of silently replacing the first.
    """
    if copied.get(prefix) == uri or prefix not in copied:
        copied[prefix] = uri
        return
    _new_prefix(copied, uri)


def _available_alias_binding(available: dict[str, str], prefix: str, uri: str) -> None:
    """Keep the target URI usable for naming when its result prefix is taken."""
    existing = available.get(prefix)
    if existing is None:
        available[prefix] = uri
        return
    if existing != uri and uri not in available.values():
        _new_prefix(available, uri)


def _prepare_scope(
    namespaces: Mapping[str, str] | None,
    *,
    excluded: frozenset[str] = frozenset(),
    aliases: Iterable[NamespaceAlias] = (),
    element_namespace: str | None,
) -> _PreparedScope:
    """Prepare one literal result element's copied nodes and naming bindings.

    Exclusion designates a namespace URI, never a prefix, and only affects
    namespace nodes copied from the stylesheet.  Namespace aliasing replaces a
    literal URI by its target URI for names, drops namespace nodes whose URI is
    a literal URI, and copies nodes whose URI is a target URI even when that
    URI is also excluded.  Every binding the copy rules require survives: a
    collision over one prefix renames one of the nodes rather than dropping it.
    """
    rules = tuple(aliases)
    raw = dict(namespaces or {})
    available = dict(raw)
    available.setdefault("xml", XML_NS)
    target_prefixes: dict[str, str] = {}
    for rule in rules:
        if rule.target_uri is not None:
            target_prefixes.setdefault(rule.target_uri, rule.result_prefix)
            _available_alias_binding(available, rule.result_prefix, rule.target_uri)
    literal_uris = {rule.literal_uri for rule in rules}
    copied: dict[str, str] = {}
    # Ordinary copied bindings keep their stylesheet prefix, so they are
    # established first; the alias target nodes then take their result prefix
    # or a generated alternative when that prefix is already occupied.
    for prefix, uri in raw.items():
        if prefix == "xml" or uri in target_prefixes:
            continue
        if uri in literal_uris or uri in excluded:
            continue
        _copy_namespace(copied, prefix, uri)
    for prefix, uri in raw.items():
        if prefix == "xml" or uri not in target_prefixes:
            continue
        keep = any(rule.target_uri == uri and rule.result_prefix == prefix for rule in rules)
        _copy_namespace(copied, prefix if keep else target_prefixes[uri], uri)
    effective_element = element_namespace
    for rule in rules:
        if rule.literal_uri == element_namespace:
            effective_element = rule.target_uri
            break
    if effective_element is None:
        copied.pop("", None)
    return _PreparedScope(
        copied_namespaces=tuple(copied.items()),
        available_bindings=MappingProxyType(available),
        element_namespace=effective_element,
        aliases=rules,
    )


def _clark(namespace: str | None, local: str) -> str:
    return local if namespace is None else f"{{{namespace}}}{local}"


def _bind_prefix(
    scope: dict[str, str],
    namespace: str,
    preferred: str | None,
    *,
    is_attribute: bool,
) -> str:
    """Ensure *scope* declares *namespace* and return the selected prefix.

    A preferred alias prefix wins when it is free or already names the target
    namespace.  Otherwise an element takes a free default namespace and every
    other name gets a deterministic generated prefix.
    """
    if preferred is not None and (not is_attribute or preferred):
        existing = scope.get(preferred)
        if existing is None or existing == namespace:
            scope[preferred] = namespace
            return preferred
    if not is_attribute and "" not in scope:
        scope[""] = namespace
        return ""
    return _new_prefix(scope, namespace)


def _element_prefix(
    namespace: str | None,
    scope: dict[str, str],
    available: Mapping[str, str] | None = None,
    *,
    requested: str | None,
    force_requested: bool = False,
) -> str:
    """Select one element prefix, preferring the name's lexical prefix.

    A requested prefix wins only while a binding for it still names
    *namespace*; otherwise a copied default or prefix is selected, then an
    available (excluded or aliased) binding, and a deterministic prefix is
    generated as a last resort.

    ``force_requested`` gives the prefix of an ``xsl:element`` name priority
    over a conflicting in-scope binding: the lexical prefix part of the
    expanded QName is copied, and namespace fixup rebinds it on the new
    element (XSLT 3.0 §11.2.2).  A prefix already bound to a different URI is
    a genuine fixup conflict: the other binding is a namespace node that must
    survive, so the name takes another prefix instead of replacing it.  The
    reserved ``xml``/``xmlns`` prefixes are never rebound either.
    """
    if namespace is None:
        return ""
    if namespace == XML_NS:
        scope.setdefault("xml", XML_NS)
        return "xml"
    if force_requested and requested is not None and requested not in ("xml", "xmlns"):
        existing = scope.get(requested)
        if existing is None or existing == namespace:
            scope[requested] = namespace
            return requested
    bindings = scope if available is None else available
    if requested is not None and bindings.get(requested) == namespace:
        return _bind_prefix(scope, namespace, requested, is_attribute=False)
    selected = _prefix_for(namespace, scope)
    if selected is not None:
        return selected
    selected = _prefix_for(namespace, bindings)
    if selected is not None:
        return _bind_prefix(scope, namespace, selected, is_attribute=False)
    return _bind_prefix(scope, namespace, None, is_attribute=False)


def _attribute_prefix(
    namespace: str | None,
    scope: dict[str, str],
    available: Mapping[str, str],
    *,
    requested: str | None,
    trusted: bool = False,
) -> str:
    """Select one attribute prefix; unprefixed attributes are unqualified.

    ``requested`` is the attribute's lexical prefix.  A literal result
    attribute's prefix is only reused while the stylesheet bindings still name
    *namespace*; ``trusted`` marks a prefix already carried by a constructed
    attribute node (an attribute-set member), which may be reused whenever it
    is free, even when the literal element's own scope never bound it.
    """
    if namespace is None:
        return ""
    if namespace == XML_NS:
        scope.setdefault("xml", XML_NS)
        return "xml"
    if requested and (trusted or available.get(requested) == namespace):
        return _bind_prefix(scope, namespace, requested, is_attribute=True)
    selected = _prefix_for(namespace, scope, is_attribute=True)
    if selected is not None:
        return selected
    selected = _prefix_for(namespace, available, is_attribute=True)
    if selected is not None:
        return _bind_prefix(scope, namespace, selected, is_attribute=True)
    return _bind_prefix(scope, namespace, requested or None, is_attribute=True)


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


def _declarations(scope: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple((prefix, uri) for prefix, uri in scope.items() if prefix != "xml")


def _ordered_scope(scope: Mapping[str, str], available: Mapping[str, str]) -> dict[str, str]:
    """Order declared prefixes by the stylesheet's binding order.

    Namespace fixup may reintroduce a copied binding after exclusions; placing
    it back at its in-scope position keeps declarations deterministic and
    independent of whether the binding was copied or fixed up.
    """
    ordered = {prefix: scope[prefix] for prefix in available if prefix in scope}
    for prefix, uri in scope.items():
        if prefix not in ordered:
            ordered[prefix] = uri
    return ordered


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
            document._order_base = _next_order_base()
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
        disable_output_escaping: bool = False,
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
            disable_output_escaping=disable_output_escaping,
        )
        self._next_id += 1
        return node

    def element(
        self,
        name: str,
        *,
        attributes: Iterable[tuple[AttributeName, str] | tuple[str, str]] = (),
        attribute_set_attributes: Iterable[tuple[AttributeName, str] | tuple[str, str]] = (),
        namespaces: Mapping[str, str] | None = None,
        base_uri: str | None = None,
        namespace_aliases: Mapping[str, str] | None = None,
        excluded_namespaces: frozenset[str] = frozenset(),
        namespace_alias_rules: Iterable[NamespaceAlias] = (),
        annotation: XdmAnnotation | None = None,
        prefix: str | None = None,
        force_prefix: bool = False,
    ) -> XdmNode:
        """Create one detached element with namespace fixup applied.

        ``namespaces`` are the stylesheet bindings in scope at the literal
        result element.  ``excluded_namespaces`` removes only the namespace
        nodes copied from that scope; a URI a constructed name requires is
        reintroduced by fixup.  ``namespace_alias_rules`` rewrites literal URIs
        to their result-tree targets for ``attributes`` — the attributes
        written on the literal result element itself.  ``attribute_set_attributes``
        come from ``xsl:attribute`` and keep their expanded names unchanged.
        Both attribute iterables carry each attribute's lexical prefix in an
        :class:`AttributeName`; a plain string name is accepted for the direct
        builder API and means an unprefixed attribute.  ``namespace_aliases``
        remains the low-level prefix-remap form of the direct builder API.
        ``force_prefix`` gives ``prefix`` the priority of an ``xsl:element``
        lexical name over a conflicting in-scope binding.
        """
        raw_namespaces = _aliased_namespaces(namespaces, namespace_aliases)
        element_namespace, element_local = _expanded_name(name, code="XTDE0820")
        attribute_set_list = tuple(attribute_set_attributes)
        attribute_list = tuple(attributes)
        prepared = _prepare_scope(
            raw_namespaces,
            excluded=excluded_namespaces,
            aliases=namespace_alias_rules,
            element_namespace=element_namespace,
        )
        scope = dict(prepared.copied_namespaces)
        scope.setdefault("xml", XML_NS)
        result_attributes: list[tuple[str, str, str]] = []
        for index, (head, value) in enumerate(attribute_set_list + attribute_list):
            name_spec = head if isinstance(head, AttributeName) else AttributeName(head)
            attribute_namespace, attribute_local = _expanded_name(
                name_spec.name,
                code="XTDE0850",
            )
            if attribute_namespace == XMLNS_NS:
                raise _dynamic(
                    "an attribute cannot be in the xmlns namespace",
                    "XTDE0850",
                )
            if index < len(attribute_set_list):
                effective_namespace = attribute_namespace
                requested = name_spec.prefix
                trusted_prefix = True
            else:
                effective_namespace = prepared.resolve_namespace(attribute_namespace)
                alias_prefix = prepared.preferred_prefix(attribute_namespace)
                requested = name_spec.prefix if alias_prefix is None else alias_prefix
                trusted_prefix = False
            selected = _attribute_prefix(
                effective_namespace,
                scope,
                prepared.available_bindings,
                requested=requested,
                trusted=trusted_prefix,
            )
            result_attributes.append(
                (_clark(effective_namespace, attribute_local), value, selected)
            )
        requested_element_prefix = prepared.preferred_prefix(element_namespace)
        aliased_element = requested_element_prefix is not None
        if requested_element_prefix is None:
            requested_element_prefix = prefix
        selected_prefix = _element_prefix(
            prepared.element_namespace,
            scope,
            prepared.available_bindings,
            requested=requested_element_prefix,
            force_requested=force_prefix or aliased_element,
        )
        scope = _ordered_scope(scope, prepared.available_bindings)
        declarations = _declarations(scope)
        element = self._new(
            NodeKind.ELEMENT,
            parent=None,
            name=_clark(prepared.element_namespace, element_local),
            base_uri=base_uri,
            in_scope_namespaces=scope,
            namespace_declarations=declarations,
            prefix=selected_prefix,
            namespace_uri=prepared.element_namespace,
            annotations=annotation,
        )
        for attribute_name, value, attribute_prefix in result_attributes:
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
        disable_output_escaping: bool = False,
    ) -> XdmNode:
        return self._new(
            NodeKind.TEXT,
            parent=parent,
            value=value,
            base_uri=base_uri,
            annotations=annotations,
            disable_output_escaping=disable_output_escaping,
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
        copy_attributes: bool = True,
        preserve_base: bool = False,
    ) -> XdmNode:
        """Copy one node (and its subtree) into this factory's document.

        ``copy_attributes=False`` gives a shallow ``xsl:copy`` of an element:
        the element keeps its name and namespaces but no attributes.
        ``preserve_base`` selects copy-of base rules; the default is the
        ``xsl:copy`` rule that adopts the new containing node's base.
        """
        if source.kind is NodeKind.DOCUMENT:
            return self._clone_document(
                source,
                deep=deep,
                copy_namespaces=copy_namespaces,
                preserve_base=preserve_base,
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
                base_uri=_copy_base(
                    source,
                    parent,
                    self.document.base_uri,
                    preserve=preserve_base,
                ),
                in_scope_namespaces=scope,
                namespace_declarations=declarations,
                prefix=source.prefix,
                namespace_uri=source.namespace_uri,
                annotations=source.annotation,
            )
            if copy_attributes:
                for attribute in source.attributes:
                    cloned_attribute = self.clone(
                        attribute, parent=node, preserve_base=preserve_base
                    )
                    if not copy_namespaces and cloned_attribute.namespace_uri is not None:
                        cloned_attribute._prefix = (
                            _prefix_for(
                                cloned_attribute.namespace_uri,
                                scope,
                                is_attribute=True,
                            )
                            or ""
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
                            preserve_base=preserve_base,
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
            base_uri=_copy_base(
                source,
                parent,
                self.document.base_uri,
                preserve=preserve_base,
            ),
            in_scope_namespaces=source.in_scope_namespaces,
            prefix=source.prefix,
            namespace_uri=source.namespace_uri,
            annotations=source.annotation,
            disable_output_escaping=source.disable_output_escaping,
        )

    def _clone_document(
        self,
        source: XdmNode,
        *,
        deep: bool,
        copy_namespaces: bool,
        preserve_base: bool = False,
    ) -> XdmDocument:
        document = XdmDocument(
            source_name=_RESULT_SOURCE,
            source_index=_RESULT_INDEX,
            document_uri=source.document_uri,
            base_uri=source.base_uri,
            schema_pool=getattr(source, "schema_pool", None),
        )
        document._order_base = _next_order_base()
        factory = _NodeFactory(document=document)
        if deep:
            for child in source.children:
                clone = factory.clone(
                    child,
                    parent=document,
                    deep=True,
                    copy_namespaces=copy_namespaces,
                    preserve_base=preserve_base,
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
            if _prefix_for(attribute_namespace, reduced, is_attribute=True) is not None:
                continue
            if attribute.prefix and attribute.prefix not in reduced:
                reduced[attribute.prefix] = attribute_namespace
            else:
                _new_prefix(reduced, attribute_namespace)
        return reduced, _declarations(reduced)

    def finalize(self) -> XdmDocument:
        self.document._finalize_document_order()
        return self.document

    def finalize_root(self, root: XdmNode) -> XdmNode:
        """Finalize one completed element-rooted temporary tree.

        The literal-construction and copy instructions build their element in a
        factory that never appended it to the factory's document, so the tree
        has no document node above it and no document order.  The element is
        complete when this is called: all content and validation defaults are
        present.  A parentless root keeps its parentless identity (an
        element-rooted tree's root), while the document node is marked
        finalized at order 0 so the registry can admit the tree; the element and
        its descendants are placed in this document's unique order range.
        """
        if root.kind is not NodeKind.ELEMENT:
            raise ValueError("only an element can be a temporary tree's root")
        if root.document is not self.document:
            raise ValueError("the root belongs to another document")
        self.document._finalize_document_order(root)
        return root

    def _append(self, parent: XdmNode, child: XdmNode) -> None:
        child._parent = parent
        parent._append_child(child)
        if (
            parent.kind is NodeKind.DOCUMENT
            and child.kind is NodeKind.ELEMENT
            and self.document._root_element is None
        ):
            self.document._root_element = child

    def attach_node(self, parent: XdmNode, node: XdmNode) -> None:
        """Attach an already-copied node to *parent* without copying it again.

        The copy instructions build their nodes with :meth:`clone`; a caller
        that holds the finished copy (``snapshot()`` grafting one ancestor
        chain) attaches it here so identity is the copy it keeps a reference to.
        """
        self._append(parent, node)

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
    if children and children[-1].kind is NodeKind.TEXT and not children[-1].disable_output_escaping:
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
    """Copy inherited namespace nodes down one constructed subtree.

    Only the namespace nodes of the element being attached are inherited;
    the binding is then offered unchanged to every descendant, except where
    an intermediate element already has a node with that name.  A default
    namespace node additionally stops at an intermediate element in no
    namespace (XSLT 3.0 section 5.7.1).  Carrying the intermediate element's
    own scope onward instead would give every descendant the deepest ancestry
    regardless of an ``inherit-namespaces="no"`` construction on the way down.
    """
    own = element.in_scope_namespaces
    scope = dict(own)
    added = False
    for prefix, uri in inherited.items():
        if prefix == "xml" or prefix in own:
            continue
        if prefix == "" and element.namespace_uri is None:
            continue
        scope[prefix] = uri
        element._append_namespace_node(factory.namespace(prefix, uri, parent=element, scope=scope))
        added = True
    if added:
        element._in_scope_namespaces = MappingProxyType(scope)
        element._namespace_declarations = _declarations(scope)
    propagated = {
        prefix: uri
        for prefix, uri in inherited.items()
        if prefix not in own and not (prefix == "" and element.namespace_uri is None)
    }
    if not propagated:
        return
    for child in element.children:
        if child.kind is NodeKind.ELEMENT:
            _inherit_namespaces(factory, child, propagated)


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


def _copy_base(
    source: XdmNode,
    parent: XdmNode | None,
    document_base: str | None,
    *,
    preserve: bool,
) -> str | None:
    """The base URI of one node copied into a new result tree.

    XSLT 3.0 §11.8: an ``xsl:copy`` of an element takes the base URI of its
    containing node in the result tree, while a deep copy preserves the
    original node's base.  A preserved base is rebased onto the new containing
    node when the source element declares an ``xml:base``; an absolute value
    therefore stays absolute and a relative one resolves against the result
    tree.
    """
    parent_base = (
        document_base if parent is None or parent.kind is NodeKind.DOCUMENT else parent.base_uri
    )
    if not preserve or source.kind is not NodeKind.ELEMENT:
        return parent_base if parent_base is not None else source.base_uri
    own = next(
        (
            attribute.string_value
            for attribute in source.attributes
            if attribute.name == f"{{{XML_NS}}}base"
        ),
        None,
    )
    if own is not None:
        return urljoin(parent_base, own) if parent_base is not None else own
    return parent_base if parent_base is not None else source.base_uri


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
        preserve_base=True,
    )


def _append_complex_content(
    factory: _NodeFactory,
    parent: XdmNode,
    sequence: Iterable[object],
    *,
    allow_attributes: bool,
    inherit_namespaces: bool = True,
    preserve_disable_output_escaping: bool = True,
) -> None:
    """Apply XSLT complex-content construction to *sequence* under *parent*."""
    last_atomic = False
    for item in _flatten_arrays(sequence):
        if isinstance(item, XdmAtomic):
            value = item.string_value
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
                clone = factory.clone(item, parent=parent, preserve_base=True)
                factory.attach_attribute(parent, clone)
            else:
                clone = factory.clone(item, parent=parent, preserve_base=True)
                factory.attach_namespace(parent, clone)
            last_atomic = False
            continue
        if item.kind is NodeKind.TEXT:
            if item.disable_output_escaping and preserve_disable_output_escaping:
                factory._append(
                    parent,
                    factory.text(
                        item.string_value,
                        parent=parent,
                        base_uri=parent.base_uri,
                        disable_output_escaping=True,
                    ),
                )
            else:
                _append_text(factory, parent, item.string_value)
            last_atomic = False
            continue
        clone = _clone_for_content(factory, item, parent)
        if item._adopts_parent_base and clone.kind is NodeKind.ELEMENT:
            clone._base_uri = parent.base_uri
            clone._adopts_parent_base = False
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
    existing = _prefix_for(namespace, parent.in_scope_namespaces, is_attribute=True)
    if existing is not None:
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
        attributes: Iterable[tuple[AttributeName, str] | tuple[str, str]] = (),
        attribute_set_attributes: Iterable[tuple[AttributeName, str] | tuple[str, str]] = (),
        namespaces: Mapping[str, str] | None = None,
        namespace_aliases: Mapping[str, str] | None = None,
        excluded_namespaces: frozenset[str] = frozenset(),
        namespace_alias_rules: Iterable[NamespaceAlias] = (),
        base_uri: str | None = None,
        annotation: XdmAnnotation | None = None,
        prefix: str | None = None,
        force_prefix: bool = False,
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
            attribute_set_attributes=attribute_set_attributes,
            namespaces=inherited,
            base_uri=base_uri,
            namespace_aliases=namespace_aliases,
            excluded_namespaces=excluded_namespaces,
            namespace_alias_rules=namespace_alias_rules,
            annotation=annotation,
            prefix=prefix,
            force_prefix=force_prefix,
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
        preserve_disable_output_escaping: bool = True,
    ) -> None:
        """Attach one sequence as complex content of the current parent."""
        parent = self.current
        _append_complex_content(
            self._factory,
            parent,
            sequence,
            allow_attributes=parent.kind is not NodeKind.DOCUMENT,
            inherit_namespaces=inherit_namespaces,
            preserve_disable_output_escaping=preserve_disable_output_escaping,
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
            preserve_base=True,
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

    def finalize_root(self, root: XdmNode) -> XdmNode:
        """Finalize *root* as this builder's temporary tree and return it."""
        self._factory.finalize_root(root)
        self._finalized = True
        return root

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
            builder.append(items, preserve_disable_output_escaping=False)
        else:
            for index, item in enumerate(items):
                if index:
                    builder.text(item_separator)
                builder.append((item,), preserve_disable_output_escaping=False)
        tree = builder.to_tree()
        # Sequence normalization constructs new text nodes; a marker copied
        # from an element subtree does not survive into the new tree.
        for node in (tree, *tree.descendants):
            if node.kind is NodeKind.TEXT:
                node._disable_output_escaping = False
        return tree


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
