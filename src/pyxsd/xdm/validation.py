"""Validation of XDM element, attribute, and document occurrences.

XSLT construction instructions can request schema validation with a global
declaration (``validation="strict"`` or ``"lax"``) or with a named type
(``type="..."``), and schema-aware source input can request the same named
type.  This module applies that request to the actual occurrence and writes
the resulting type annotation, nilled property, typed value, and defaulted
attributes/nodes onto the same XDM tree.  It never serializes a result and
re-parses it; an element is converted structurally to an ElementTree for
pyxsd's binder, and the resulting annotation is matched back by occurrence
position.

The module is independent of the XSLT execution layer.  Constructed failures
raise :class:`ConstructedValidationError` with a reason; the XSLT boundary maps
each reason to its exact ``XTTE`` code.  Source requested-type assessment maps
the same reasons onto :class:`~pyxsd.xdm.document.DocumentValidationError`.
Raw maps, arrays, and atomic values never reach this module: delivery
validation is only invoked after sequence normalization actually creates a
document node.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Literal, cast

from pyxsd import xsd_data_types as xsd
from pyxsd.derivation import combinedBlock, is_valid_xsi_type
from pyxsd.locations import ParseProvenance
from pyxsd.namespaces import XSD_NS, XSI_NS, NamespaceContext, clark
from pyxsd.schema_context import (
    SchemaContext,
    active_context,
    context_provenance,
    context_report,
    parser_host_override,
)
from pyxsd.validation import ValidationReport
from pyxsd.xdm.annotations import (
    UNTYPED,
    UNTYPED_ATOMIC,
    OccurrenceAnnotation,
    _atomize,
    capturing_occurrences,
    id_kind_for,
    schema_type_name,
)
from pyxsd.xdm.document import element_from_xdm
from pyxsd.xdm.errors import XdmInputError
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool, _attribute_descriptors
from pyxsd.xdm.values import XdmAtomic
from pyxsd.xsd_data_types import xsd_value_key

__all__ = [
    "ConstructedValidationError",
    "ValidationRequest",
    "namespace_sensitive",
    "validate_document",
    "validate_occurrence",
]

_ANY_TYPE = clark(XSD_NS, "anyType")
_VALIDATION_MODES = frozenset({"strict", "lax", "preserve", "strip"})
_OPERATIONS = frozenset({"element", "attribute", "copy", "copy-of"})
_ID_CODES = frozenset({"idref-unresolved", "id-duplicate"})
_SIMPLE_XSI_ATTRIBUTES = frozenset({"type", "nil", "schemaLocation", "noNamespaceSchemaLocation"})
_DOCUMENT_CONSTRAINT_CODES = frozenset(
    {
        "idref-unresolved",
        "id-duplicate",
        "identity-key",
        "identity-keyref",
        "identity-unique",
    }
)
_BUILTIN_POOL: SchemaPool | None = None


class _UntypedAtomicAdapter:
    """Adapter for ``xs:untypedAtomic``, which has no generated type class."""

    _type_class = None
    name = UNTYPED_ATOMIC

    def __init__(self, schema_pool: SchemaPool | None) -> None:
        self.maps = schema_pool

    def is_simple(self) -> bool:
        return True

    def is_valid(self, value: Any) -> bool:
        return True

    def cast(self, value: Any) -> Any:
        return XdmAtomic.untyped_atomic(value)


def _builtin_pool() -> SchemaPool:
    """The immutable built-in-type pool, used when no schema was imported."""
    global _BUILTIN_POOL
    if _BUILTIN_POOL is None:
        _BUILTIN_POOL = SchemaPool()
    return _BUILTIN_POOL


def _lookup_type(schema_pool: SchemaPool | None, name: str) -> Any:
    """Resolve a named type from the package pool or the built-in XSD set."""
    if name == UNTYPED_ATOMIC:
        return _UntypedAtomicAdapter(schema_pool)
    if schema_pool is not None:
        adapter = schema_pool.get_type(name)
        if adapter is not None:
            return adapter
    if name.startswith(f"{{{XSD_NS}}}"):
        return _builtin_pool().get_type(name)
    return None


@contextmanager
def _isolated_schema(
    schema: Any,
    *,
    parents: dict[int, Any] | None = None,
) -> Iterator[tuple[Any, Any, SchemaContext, NamespaceContext]]:
    """Yield a private schema/host/context view for one validation run.

    ``Schema.parse`` and the generated binder keep per-parse binding indexes
    and the active report/provenance on a shared host/context.  Occurrence
    validation clones both for the duration of one assessment, and routes
    generated-class host lookups through :func:`parser_host_override`, so
    concurrent transformations cannot overwrite one another's annotations.
    """
    namespace_context = NamespaceContext()
    base_host = getattr(schema, "_host", None)
    if base_host is None:  # pragma: no cover - compiled schemas carry a host
        host: Any = SimpleNamespace(
            report=getattr(schema, "report", None),
            components=getattr(schema, "components", None),
            mode=getattr(schema, "mode", None),
            namespaceContext=namespace_context,
            classes=getattr(schema, "classes", {}),
        )
    else:
        host = copy.copy(base_host)
        host.namespaceContext = namespace_context
    host._elementParents = {} if parents is None else parents
    host._elementTypes = {}
    base_context = getattr(schema, "_schema_context", None)
    context = (
        replace(base_context)
        if base_context is not None
        else SchemaContext(components=getattr(schema, "components", None))
    )
    context.report = None
    context.provenance = None
    isolated = copy.copy(schema)
    isolated._host = host
    isolated._schema_context = context
    isolated.namespace_context = namespace_context
    with parser_host_override(schema, host):
        yield isolated, host, context, namespace_context


def _occurrence_error(
    error_type: type[XdmInputError],
    message: str,
    reason: str,
) -> XdmInputError:
    """Build the caller's public validation error with its own reason arm."""
    return cast(Any, error_type)(message, reason=reason)


class ConstructedValidationError(XdmInputError):
    """A constructed node failed validity assessment.

    ``reason`` names the XSLT error arm without importing the XSLT layer:
    ``invalid`` (XTTE1510), ``unknown`` (XTTE1512), ``lax-invalid``
    (XTTE1515), ``type`` (XTTE1540), ``qname`` (XTTE1545),
    ``document-shape`` (XTTE1550), ``document-constraint`` (XTTE1555), and
    ``copy-type`` (XTTE1535).
    """

    def __init__(
        self,
        message: str,
        *,
        reason: Literal[
            "invalid",
            "unknown",
            "lax-invalid",
            "type",
            "qname",
            "document-shape",
            "document-constraint",
            "copy-type",
        ],
    ) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    """One construction or delivery validation request.

    ``type_name`` is an already-expanded Clark name.  When it is present the
    validation mode is irrelevant: the node is assessed against that type.
    """

    validation: str = "strip"
    type_name: str | None = None

    def __post_init__(self) -> None:
        if self.validation not in _VALIDATION_MODES:
            raise ValueError(f"invalid validation mode {self.validation!r}")
        if self.type_name is not None and not isinstance(self.type_name, str):
            raise TypeError("a validation type name must be a string or None")

    @property
    def requested(self) -> bool:
        """Whether this request asks schema validity assessment."""
        return self.type_name is not None or self.validation in ("strict", "lax")


def validate_occurrence(
    node: XdmNode,
    request: ValidationRequest,
    *,
    schema_pool: SchemaPool | None,
    operation: Literal["element", "attribute", "copy", "copy-of"] = "element",
) -> None:
    """Validate one constructed element or attribute occurrence in place.

    ``operation`` distinguishes a newly constructed node from a copied node:
    preserving a newly constructed attribute still annotates it as
    ``xs:untypedAtomic``, while preserving a copied attribute retains its
    annotation.  Preserving a copied element (``copy-of``) also retains the
    element's own annotation instead of replacing it with ``xs:anyType``.
    """
    if operation not in _OPERATIONS:
        raise ValueError(f"invalid construction operation {operation!r}")
    if node.kind is NodeKind.DOCUMENT:
        validate_document(cast(XdmDocument, node), request, schema_pool=schema_pool)
        return
    if node.kind is NodeKind.ELEMENT:
        _validate_element(node, request, schema_pool, operation=operation)
        return
    if node.kind is NodeKind.ATTRIBUTE:
        _validate_attribute(node, request, schema_pool, operation=operation)
        return


def validate_document(
    document: XdmDocument,
    request: ValidationRequest,
    *,
    schema_pool: SchemaPool | None,
) -> None:
    """Validate a normalized delivered document in place.

    Validation is only requested for ``strict``, ``lax``, or a named type.
    ``preserve`` and ``strip`` only change annotations.  When validation is
    requested the document must contain exactly one element child and no text
    children; comments and processing instructions are copied unchanged.
    """
    if not isinstance(document, XdmDocument):
        raise TypeError("document validation requires an XdmDocument")
    if not request.requested:
        if request.validation == "strip":
            _strip_subtree(document)
        return
    elements = [child for child in document.children if child.kind is NodeKind.ELEMENT]
    text = [child for child in document.children if child.kind is NodeKind.TEXT]
    if len(elements) != 1 or text:
        raise ConstructedValidationError(
            "a validated document node must contain exactly one element and no text",
            reason="document-shape",
        )
    _validate_element(elements[0], request, schema_pool, operation="element", document_root=True)
    document._finalize_document_order()


# ---------------------------------------------------------------------------
# Element validation


def _assess_element(
    node: XdmNode,
    adapter: Any,
    *,
    document_root: bool = False,
    declaration: Any = None,
    error_type: type[XdmInputError],
    reason: str,
) -> None:
    """Assess one element occurrence against a concrete type definition.

    Source requested-type validation and constructed-element validation both
    delegate here.  The type adapter is resolved against an invocation-private
    schema/host/context view, the occurrence is converted structurally for the
    binder, and the resulting annotations and defaulted attributes are applied
    back to the original XDM node.  *declaration* is the governing element
    declaration when one exists; failures use *error_type* and *reason* so each
    public boundary keeps its own error contract.
    """
    schema_pool = getattr(adapter, "maps", None)
    adapter = _actual_type(
        node,
        adapter,
        schema_pool,
        error_type=error_type,
        reason=reason,
    )
    if declaration is None:
        declaration = _element_declaration(schema_pool, node.name)
    nilled = _nilled_state(node, error_type=error_type, reason=reason)
    if nilled:
        if declaration is None or not declaration.isNillable():
            raise _occurrence_error(
                error_type,
                f"the element {node.name!r} cannot be nilled without a nillable declaration",
                reason,
            )
        if any(
            child.kind is NodeKind.ELEMENT or child.kind is NodeKind.TEXT for child in node.children
        ):
            raise _occurrence_error(
                error_type,
                f"the nilled element {node.name!r} cannot carry content",
                reason,
            )
    if getattr(adapter, "name", None) == _ANY_TYPE:
        _annotate_any_type(node)
        _lax_validate_children(
            node,
            schema_pool,
            reason=reason,
            error_type=error_type,
        )
        return
    if adapter.is_simple():
        _validate_simple_element(
            node,
            adapter,
            nilled=nilled,
            error_type=error_type,
            reason=reason,
        )
        return
    type_class = getattr(adapter, "_type_class", None)
    schema = getattr(getattr(adapter, "schema", None), "_schema", None)
    if not isinstance(type_class, type) or schema is None:  # pragma: no cover
        raise _occurrence_error(
            error_type,
            f"the type {getattr(adapter, 'name', None)!r} has no compiled definition",
            reason,
        )
    with _isolated_schema(schema) as (_isolated, host, context, namespace_context):
        element = element_from_xdm(node, namespace_context)
        _normalize_empty_text(element)
        provenance = ParseProvenance(element, None)
        report = ValidationReport()
        host._elementParents = _parent_index(element)
        with capturing_occurrences() as capture:
            with (
                active_context(context),
                context_report(context, report),
                context_provenance(context, provenance),
            ):
                instance = _make_instance(
                    type_class,
                    element,
                    nilled=nilled,
                    error_type=error_type,
                    reason=reason,
                )
                if declaration is not None:
                    instance._descriptor_ = declaration
                _capture_root(
                    node,
                    element,
                    capture,
                    type_class,
                    instance,
                    declaration=_governing_declaration(declaration, adapter),
                    nilled=nilled,
                )
            capture.finish(report)
    if document_root:
        from pyxsd.instance_binding import check_identity_constraints_entry

        check_identity_constraints_entry(schema, instance, report)
    errors = _instance_errors(report, document=document_root)
    if errors:
        if document_root and any(
            getattr(issue, "code", None) in _DOCUMENT_CONSTRAINT_CODES for issue in errors
        ):
            raise _occurrence_error(
                error_type,
                "the validated document has unsatisfied document-level constraints",
                ("document-constraint" if error_type is ConstructedValidationError else reason),
            )
        raise _occurrence_error(
            error_type,
            f"the element {node.name!r} is not valid against {getattr(adapter, 'name', None)!r}",
            reason,
        )
    _apply_capture(
        node,
        element,
        provenance,
        capture,
        error_type=error_type,
        reason=reason,
    )
    node.document._finalize_document_order()


def _assess_declaration(
    node: XdmNode,
    adapter: Any,
    *,
    document_root: bool = False,
    error_type: type[XdmInputError],
    reason: str,
) -> None:
    """Assess one occurrence declared by a global element declaration.

    The declaration's own resolver selects the actual type (including
    ``xsi:type`` overrides, conditional type alternatives, substitution-group
    head types, and abstract-declaration rejection); the selected type then
    goes through the same :func:`_assess_element` occurrence path used by
    requested-type validation.
    """
    schema_pool = getattr(adapter, "maps", None)
    schema = schema_pool.schema_for_element(node.name) if schema_pool is not None else None
    declaration = getattr(adapter, "_er", None)
    if schema is None or declaration is None:  # pragma: no cover - pool and ER agree
        raise _occurrence_error(
            error_type,
            f"the declaration for {node.name!r} has no compiled schema",
            reason,
        )
    from pyxsd.instance_binding import class_for_root

    report = ValidationReport()
    with _isolated_schema(schema) as (
        isolated,
        _host,
        _context,
        namespace_context,
    ):
        element = element_from_xdm(node, namespace_context)
        _normalize_empty_text(element)
        type_class = class_for_root(isolated, declaration, element, report)
    if _instance_issues(report) or type_class is None:
        raise _occurrence_error(
            error_type,
            f"the element {node.name!r} is not valid against its declaration",
            reason,
        )
    _assess_element(
        node,
        _type_adapter_for_class(schema_pool, type_class, schema, declaration),
        document_root=document_root,
        declaration=declaration,
        error_type=error_type,
        reason=reason,
    )


def _type_adapter_for_class(
    schema_pool: SchemaPool | None,
    type_class: type[Any],
    schema: Any,
    declaration: Any,
) -> Any:
    """Adapt a declaration-selected generated class for occurrence assessment."""
    from pyxsd.schema_base import SchemaBase

    if type_class is SchemaBase:
        adapter = _lookup_type(schema_pool, _ANY_TYPE)
        if adapter is None:  # pragma: no cover - the built-in pool provides it
            raise ConstructedValidationError(
                "the implicit anyType declaration has no compiled definition",
                reason="type",
            )
        return adapter
    if schema_pool is None:  # pragma: no cover - a declaration implies a pool
        raise ConstructedValidationError(
            "the declaration has no schema pool",
            reason="type",
        )
    adapter = schema_pool.type_for_class(type_class)
    if getattr(adapter, "schema", None) is None:
        from pyxsd.xdm.schema import PyxsdSchemaProxy, PyxsdType

        adapter = PyxsdType(
            schema_pool,
            type_class,
            er=declaration,
            proxy=PyxsdSchemaProxy(schema, schema_pool),
        )
    return adapter


def _validate_element(
    node: XdmNode,
    request: ValidationRequest,
    schema_pool: SchemaPool | None,
    *,
    operation: str,
    document_root: bool = False,
) -> None:
    if request.type_name is not None:
        _validate_element_type(node, request.type_name, schema_pool, document_root=document_root)
        return
    if request.validation == "strip":
        _strip_subtree(node)
        return
    if request.validation == "preserve":
        if operation != "copy-of":
            _annotate_any_type(node)
        return
    _validate_element_declaration(
        node,
        request.validation,
        schema_pool,
        document_root=document_root,
    )


def _validate_element_declaration(
    node: XdmNode,
    mode: str,
    schema_pool: SchemaPool | None,
    *,
    document_root: bool = False,
    lax_reason: str = "lax-invalid",
    error_type: type[XdmInputError] = ConstructedValidationError,
) -> None:
    adapter = schema_pool.get_element(node.name) if schema_pool is not None else None
    if adapter is None:
        if mode == "strict":
            raise _occurrence_error(
                error_type,
                f"no global element declaration for {node.name!r} is in scope",
                "unknown",
            )
        _annotate_any_type(node)
        _lax_validate_children(
            node,
            schema_pool,
            error_type=error_type,
        )
        return
    _assess_declaration(
        node,
        adapter,
        document_root=document_root,
        error_type=error_type,
        reason="invalid" if mode == "strict" else lax_reason,
    )


def _lax_validate_children(
    node: XdmNode,
    schema_pool: SchemaPool | None,
    *,
    reason: str = "lax-invalid",
    error_type: type[XdmInputError] = ConstructedValidationError,
) -> None:
    if schema_pool is None:
        return
    stack = [node]
    while stack:
        current = stack.pop()
        for attribute in current.attributes:
            if schema_pool.get_attribute(attribute.name) is not None:
                _validate_attribute_declaration(
                    attribute,
                    "lax",
                    schema_pool,
                    lax_reason=reason,
                    error_type=error_type,
                )
        current_children = [
            child for child in reversed(current.children) if child.kind is NodeKind.ELEMENT
        ]
        for child in current_children:
            if schema_pool.get_element(child.name) is not None:
                _validate_element_declaration(
                    child,
                    "lax",
                    schema_pool,
                    lax_reason=reason,
                    error_type=error_type,
                )
            else:
                stack.append(child)


def _validate_element_type(
    node: XdmNode,
    type_name: str,
    schema_pool: SchemaPool | None,
    *,
    document_root: bool = False,
) -> None:
    if type_name == UNTYPED:
        _strip_subtree(node)
        return
    if type_name == _ANY_TYPE:
        _annotate_any_type(node)
        _lax_validate_children(node, schema_pool, reason="type")
        return
    adapter = _lookup_type(schema_pool, type_name)
    if adapter is None:
        raise ConstructedValidationError(
            f"the type {type_name!r} is not in scope",
            reason="type",
        )
    _assess_element(
        node,
        adapter,
        document_root=document_root,
        error_type=ConstructedValidationError,
        reason="type",
    )


def _validate_simple_element(
    node: XdmNode,
    adapter: Any,
    *,
    nilled: bool,
    error_type: type[XdmInputError] = ConstructedValidationError,
    reason: str = "type",
) -> None:
    """Assess an element occurrence against a simple type definition."""
    type_name = adapter.name
    for child in node.children:
        if child.kind is NodeKind.ELEMENT:
            raise _occurrence_error(
                error_type,
                f"the element {node.name!r} is not valid against {type_name!r}",
                reason,
            )
    for attribute in node.attributes:
        if attribute.namespace_uri != XSI_NS or attribute.local_name not in _SIMPLE_XSI_ATTRIBUTES:
            raise _occurrence_error(
                error_type,
                f"the simple-typed element {node.name!r} cannot carry the attribute "
                f"{attribute.name!r}",
                reason,
            )
    if nilled:
        node._apply_occurrence_annotation(
            _element_annotation(
                node,
                type_name=type_name,
                actual_type=getattr(adapter, "_type_class", None),
                typed_values=(),
                nilled=True,
                id_kind=id_kind_for(getattr(adapter, "_type_class", None)),
            )
        )
        return
    if not adapter.is_valid(node.string_value):
        raise _occurrence_error(
            error_type,
            f"the element {node.name!r} is not valid against {adapter.name!r}",
            reason,
        )
    node._apply_occurrence_annotation(
        _element_annotation(
            node,
            type_name=type_name,
            actual_type=getattr(adapter, "_type_class", None),
            typed_values=_atomize(adapter.cast(node.string_value)),
            id_kind=id_kind_for(getattr(adapter, "_type_class", None)),
        )
    )


def _element_declaration(schema_pool: SchemaPool | None, name: str | None) -> Any:
    adapter = schema_pool.get_element(name) if schema_pool is not None else None
    return None if adapter is None else getattr(adapter, "_er", None)


def _nilled_state(
    node: XdmNode,
    *,
    error_type: type[XdmInputError],
    reason: str,
) -> bool:
    """Return the nilled property, rejecting an invalid ``xsi:nil`` lexical."""
    nil = node.attribute(f"{{{XSI_NS}}}nil")
    if nil is None:
        return False
    lexical = nil.string_value.strip()
    if lexical not in ("true", "false", "1", "0"):
        raise _occurrence_error(
            error_type,
            f"the xsi:nil value {nil.string_value!r} on {node.name!r} is not a boolean",
            reason,
        )
    return lexical in ("true", "1")


def _actual_type(
    node: XdmNode,
    requested: Any,
    schema_pool: SchemaPool | None,
    *,
    error_type: type[XdmInputError],
    reason: str,
) -> Any:
    """Resolve an ``xsi:type`` override over the stipulated requested type."""
    lexical = node.attribute(f"{{{XSI_NS}}}type")
    if lexical is None:
        return requested
    resolved = getattr(node.annotation, "xsi_type", None)
    if resolved is None:
        resolved = _resolve_qname(lexical.string_value, node.in_scope_namespaces)
    if resolved is None or resolved == requested.name:
        return requested
    if requested.name == UNTYPED_ATOMIC or resolved == UNTYPED_ATOMIC:
        raise _occurrence_error(
            error_type,
            f"the xsi:type {resolved!r} is not validly derived from {requested.name!r}",
            reason,
        )
    actual = _lookup_type(schema_pool, resolved)
    if actual is None:
        raise _occurrence_error(
            error_type,
            f"the xsi:type {resolved!r} is not in scope",
            reason,
        )
    declared_class = getattr(requested, "_type_class", None)
    actual_class = getattr(actual, "_type_class", None)
    invalid = is_valid_xsi_type(
        actual_class,
        declared_class,
        combinedBlock(None, declared_class),
    )
    if invalid is not None:
        raise _occurrence_error(
            error_type,
            f"the xsi:type {resolved!r} is not validly derived from {requested.name!r}",
            reason,
        )
    return actual


def _make_instance(
    type_class: type[Any],
    element: Any,
    *,
    nilled: bool,
    error_type: type[XdmInputError],
    reason: str,
) -> Any:
    if nilled:
        generated = cast(Any, type_class)
        return generated._nilledInstance(type_class, element, generated._node_name(element))
    maker = getattr(type_class, "makeInstanceFromTag", None)
    if maker is None:  # pragma: no cover - generated classes expose the binder
        raise _occurrence_error(
            error_type,
            "the requested type cannot bind an element occurrence",
            reason,
        )
    return maker(element)


def _governing_declaration(declaration: Any, adapter: Any) -> Any:
    """The declaration that actually governs this occurrence, if any."""
    if declaration is None:
        return None
    getter = getattr(declaration, "getType", None)
    declared = getter() if getter is not None else None
    actual = getattr(adapter, "_type_class", None)
    if isinstance(declared, type) and isinstance(actual, type) and issubclass(actual, declared):
        return declaration
    return None


def _capture_root(
    node: XdmNode,
    element: Any,
    capture: Any,
    type_class: type[Any],
    instance: Any,
    *,
    declaration: Any = None,
    nilled: bool = False,
) -> None:
    from pyxsd.xdm.annotations import capture_element_occurrence, capture_remaining_attributes

    capture_remaining_attributes(element)
    capture_element_occurrence(
        element,
        governing_declaration=declaration,
        actual_type=type_class,
        nilled=nilled,
        validation_attempted="full",
        bound=instance,
        xsi_type_namespaces=dict(node.in_scope_namespaces),
    )


# ---------------------------------------------------------------------------
# Attribute validation


def _validate_attribute(
    node: XdmNode,
    request: ValidationRequest,
    schema_pool: SchemaPool | None,
    *,
    operation: str,
) -> None:
    if request.type_name is not None:
        _validate_attribute_type(node, request.type_name, schema_pool)
        return
    if request.validation in ("strip", "preserve"):
        if not (request.validation == "preserve" and operation in ("copy", "copy-of")):
            _annotate_untyped_atomic_attribute(node)
        return
    _validate_attribute_declaration(node, request.validation, schema_pool)


def _validate_attribute_declaration(
    node: XdmNode,
    mode: str,
    schema_pool: SchemaPool | None,
    *,
    lax_reason: str = "lax-invalid",
    error_type: type[XdmInputError] = ConstructedValidationError,
) -> None:
    adapter = schema_pool.get_attribute(node.name) if schema_pool is not None else None
    if adapter is None:
        if mode == "strict":
            raise _occurrence_error(
                error_type,
                f"no global attribute declaration for {node.name!r} is in scope",
                "unknown",
            )
        _annotate_untyped_atomic_attribute(node)
        return
    type_adapter = adapter.type
    if type_adapter is None:  # pragma: no cover - attribute declarations resolve a type
        raise _occurrence_error(
            error_type,
            f"the attribute {node.name!r} has no resolved type",
            "invalid" if mode == "strict" else lax_reason,
        )
    _check_attribute_qname(type_adapter)
    value = node.string_value
    if not type_adapter.is_valid(value):
        raise _occurrence_error(
            error_type,
            f"the attribute {node.name!r} is not valid against its declaration",
            "invalid" if mode == "strict" else lax_reason,
        )
    typed = type_adapter.cast(value)
    fixed = _constraint(getattr(adapter, "_er", None), "getFixed")
    if fixed is not None:
        try:
            fixed_value = type_adapter.cast(fixed)
        except (TypeError, ValueError):  # pragma: no cover - compiled fixed values are valid
            fixed_value = fixed
        if xsd_value_key(typed) != xsd_value_key(fixed_value):
            raise _occurrence_error(
                error_type,
                f"the attribute {node.name!r} conflicts with its fixed value",
                "invalid" if mode == "strict" else lax_reason,
            )
    node._apply_occurrence_annotation(
        _attribute_annotation(
            node,
            type_name=type_adapter.name,
            actual_type=getattr(type_adapter, "_type_class", None),
            typed_values=_atomize(typed),
            governing_declaration=getattr(adapter, "_er", None),
            id_kind=id_kind_for(getattr(type_adapter, "_type_class", None)),
        )
    )


def _validate_attribute_type(
    node: XdmNode,
    type_name: str,
    schema_pool: SchemaPool | None,
) -> None:
    if type_name in (UNTYPED, _ANY_TYPE):
        raise ConstructedValidationError(
            f"the complex type {type_name!r} cannot validate an attribute",
            reason="type",
        )
    if type_name == UNTYPED_ATOMIC:
        _annotate_untyped_atomic_attribute(node)
        return
    adapter = _lookup_type(schema_pool, type_name)
    if adapter is None:
        raise ConstructedValidationError(
            f"the type {type_name!r} is not in scope",
            reason="type",
        )
    if not adapter.is_simple():
        raise ConstructedValidationError(
            f"the complex type {type_name!r} cannot validate an attribute",
            reason="type",
        )
    _check_attribute_qname(adapter)
    if not adapter.is_valid(node.string_value):
        raise ConstructedValidationError(
            f"the constructed attribute {node.name!r} is not valid against {type_name!r}",
            reason="type",
        )
    typed = adapter.cast(node.string_value)
    node._apply_occurrence_annotation(
        _attribute_annotation(
            node,
            type_name=adapter.name,
            actual_type=getattr(adapter, "_type_class", None),
            typed_values=_atomize(typed),
            id_kind=id_kind_for(getattr(adapter, "_type_class", None)),
        )
    )


def namespace_sensitive(node: XdmNode) -> bool:
    """Whether a copied node's content has namespace-sensitive type information."""
    stack: list[XdmNode] = [node]
    while stack:
        current = stack.pop()
        annotation = current.annotation
        if _uses_qname_or_notation(getattr(annotation, "actual_type", None)):
            return True
        for value in getattr(annotation, "typed_values", ()):
            type_name = getattr(value, "type_name", "")
            if type_name.endswith("}QName") or type_name.endswith("}NOTATION"):
                return True
        stack.extend(reversed(current.children))
        stack.extend(current.attributes)
    return False


def _check_attribute_qname(adapter: Any) -> None:
    type_class = getattr(adapter, "_type_class", None)
    if _uses_qname_or_notation(type_class):
        raise ConstructedValidationError(
            "a parentless attribute cannot be validated against a QName-derived type",
            reason="qname",
        )


def _uses_qname_or_notation(type_class: Any) -> bool:
    if not isinstance(type_class, type):  # pragma: no cover - adapters expose a type class
        return False
    for candidate in type_class.__mro__:
        if candidate in (xsd.QName, xsd.NOTATION):
            return True
        members = vars(candidate).get("_unionMembers")
        if members and any(_uses_qname_or_notation(member) for member in members):
            return True
        item = vars(candidate).get("itemType")
        if isinstance(item, type) and _uses_qname_or_notation(item):
            return True
    return False


# ---------------------------------------------------------------------------
# Annotation application and defaulted values


def _validated_annotation(annotation: OccurrenceAnnotation, node: XdmNode) -> OccurrenceAnnotation:
    """Map a wildcard's not-known assessment to the XSLT annotation rule.

    A wildcard with ``processContents="lax"`` or ``"skip"`` leaves an
    occurrence unvalidated.  XSLT 3.0 §24.4.1.1 requires such an element to
    be annotated ``xs:anyType`` and such an attribute ``xs:untypedAtomic``.
    """
    if annotation.validation_attempted == "full" and annotation.validity != "notKnown":
        return annotation
    if node.kind is NodeKind.ATTRIBUTE:
        return OccurrenceAnnotation(
            type_name=UNTYPED_ATOMIC,
            nilled=None,
            validation_attempted="none",
            typed_values=(XdmAtomic.untyped_atomic(node.string_value),),
            is_attribute=True,
        )
    return OccurrenceAnnotation(
        type_name=_ANY_TYPE,
        nilled=bool(getattr(annotation, "nilled", False)),
        validation_attempted="none",
        actual_type=xsd.AnyType,
        typed_values=(XdmAtomic.untyped_atomic(node.string_value),),
        xsi_type=getattr(annotation, "xsi_type", None),
        xsi_type_lexical=getattr(annotation, "xsi_type_lexical", None),
        nil_lexical=getattr(annotation, "nil_lexical", None),
        nil_declared=bool(getattr(annotation, "nil_declared", False)),
    )


def _apply_capture(
    target: XdmNode,
    element: Any,
    provenance: ParseProvenance,
    capture: Any,
    *,
    error_type: type[XdmInputError] = ConstructedValidationError,
    reason: str = "invalid",
) -> None:
    """Apply a pyxsd binding capture to the original XDM occurrence."""
    stack: list[tuple[XdmNode, Any]] = [(target, element)]
    while stack:
        target_node, source_element = stack.pop()
        location = provenance.source_location(source_element)
        annotation = capture.annotation(location.path) if location is not None else None
        if annotation is None:  # pragma: no cover - the binder captures the root and children
            annotation = getattr(target_node.annotation, "actual_type", None)
        else:
            target_node._apply_occurrence_annotation(_validated_annotation(annotation, target_node))
        _add_default_attributes(
            target_node,
            getattr(annotation, "actual_type", None) if annotation is not None else None,
        )
        target_attributes = {attribute.name: attribute for attribute in target_node.attributes}
        for source_attribute in source_element.attrib:
            attribute_annotation = (
                None if location is None else capture.annotation(location.path, source_attribute)
            )
            if attribute_annotation is None:  # pragma: no cover - capture records every attribute
                continue
            target_attribute = target_attributes[source_attribute]
            target_attribute._apply_occurrence_annotation(
                _validated_annotation(attribute_annotation, target_attribute)
            )
        target_children = [
            child for child in target_node.children if child.kind is NodeKind.ELEMENT
        ]
        source_children = list(source_element)
        if len(target_children) != len(source_children):  # pragma: no cover - internal invariant
            raise _occurrence_error(
                error_type,
                "the validated tree does not match the validated occurrence",
                reason,
            )
        stack.extend(zip(target_children, source_children, strict=True))


def _add_default_attributes(target: XdmNode, actual_type: Any) -> None:
    if not isinstance(actual_type, type):
        return
    existing = {attribute.name for attribute in target.attributes}
    for descriptor in _attribute_descriptors(actual_type):
        default = _constraint(descriptor, "getDefault")
        fixed = _constraint(descriptor, "getFixed")
        lexical = default if default is not None else fixed
        if lexical is None:
            continue
        name = _attribute_instance_name(descriptor, actual_type)
        if not isinstance(name, str) or name in existing:
            continue
        type_class = descriptor.getType()
        typed_values: tuple[Any, ...]
        if isinstance(type_class, type):
            try:
                typed_values = _atomize(type_class(lexical))
            except (TypeError, ValueError):  # pragma: no cover - compiled defaults are valid
                typed_values = (XdmAtomic.untyped_atomic(lexical),)
        else:  # pragma: no cover - attribute declarations resolve simple types
            typed_values = (XdmAtomic.untyped_atomic(lexical),)
        annotation = OccurrenceAnnotation(
            type_name=schema_type_name(type_class),
            nilled=None,
            validation_attempted="full",
            actual_type=type_class,
            is_attribute=True,
            typed_values=typed_values,
            default_value=default,
            fixed_value=fixed,
            value_source="default" if default is not None else "fixed",
        )
        _append_attribute_node(target, name, lexical, annotation)
        existing.add(name)


def _constraint(descriptor: Any, getter: str) -> str | None:
    method = getattr(descriptor, getter, None)
    if method is None:  # pragma: no cover - attribute descriptors define both
        return None
    try:
        value = method()
    except Exception:  # pragma: no cover - malformed declarations are reported earlier
        return None
    return value if isinstance(value, str) else None


def _attribute_instance_name(descriptor: Any, actual_type: type[Any]) -> str | None:
    """The expanded instance name of one declared attribute use.

    ``expandedName`` includes the target namespace even for an unqualified
    local attribute, so the generated descriptor's instance-name method is the
    authoritative source.
    """
    schema = getattr(actual_type, "schema", None)
    method = getattr(descriptor, "expandedInstanceName", None)
    if method is not None:
        try:
            return method(is_attribute=True, parser=getattr(schema, "_host", None))
        except Exception:  # pragma: no cover - unresolved refs fall back below
            pass
    name = getattr(descriptor, "expandedName", None) or getattr(
        descriptor, "name", None
    )  # pragma: no cover - descriptors expose a name
    return name if isinstance(name, str) else None  # pragma: no cover - descriptors expose a name


def _append_attribute_node(
    parent: XdmNode,
    name: str,
    value: str,
    annotation: OccurrenceAnnotation,
) -> None:
    namespace, _, _ = name[1:].partition("}") if name.startswith("{") else ("", "", name)
    namespace_uri = namespace or None
    scope = dict(parent.in_scope_namespaces)
    prefix = ""
    if namespace_uri is not None:
        prefix = next(
            (candidate for candidate, uri in scope.items() if candidate and uri == namespace_uri),
            "",
        )
        if not prefix:
            prefix = _new_prefix(scope)
            scope[prefix] = namespace_uri
            _append_namespace_node(parent, prefix, namespace_uri, scope)
    attribute = XdmNode(
        NodeKind.ATTRIBUTE,
        document=parent.document,
        node_id=-1,
        source_name=parent._source_name,
        source_index=parent._source_index,
        parent=parent,
        name=name,
        value=value,
        annotations=cast(Any, annotation),
        base_uri=parent.base_uri,
        document_uri=parent.document_uri,
        in_scope_namespaces=scope,
        prefix=prefix,
        namespace_uri=namespace_uri,
    )
    parent._append_attribute(attribute)
    parent._in_scope_namespaces = scope
    parent._namespace_declarations = tuple(
        (declared_prefix, uri) for declared_prefix, uri in scope.items() if declared_prefix != "xml"
    )


def _append_namespace_node(
    parent: XdmNode,
    prefix: str,
    uri: str,
    scope: dict[str, str],
) -> None:
    node = XdmNode(
        NodeKind.NAMESPACE,
        document=parent.document,
        node_id=-1,
        source_name=parent._source_name,
        source_index=parent._source_index,
        parent=parent,
        name=prefix,
        value=uri,
        base_uri=parent.base_uri,
        document_uri=parent.document_uri,
        in_scope_namespaces=scope,
        prefix=prefix,
    )
    parent._append_namespace_node(node)


def _new_prefix(scope: dict[str, str]) -> str:
    index = 1
    while f"ns{index}" in scope:
        index += 1
    return f"ns{index}"


def _element_annotation(
    node: XdmNode,
    *,
    type_name: str | None,
    actual_type: Any,
    typed_values: tuple[Any, ...],
    nilled: bool = False,
    id_kind: str | None = None,
) -> OccurrenceAnnotation:
    evidence = node.annotation
    return OccurrenceAnnotation(
        type_name=type_name,
        nilled=nilled,
        validity="valid",
        actual_type=actual_type,
        validation_attempted="full",
        typed_values=typed_values,
        xsi_type=getattr(evidence, "xsi_type", None),
        xsi_type_lexical=getattr(evidence, "xsi_type_lexical", None),
        nil_lexical=getattr(evidence, "nil_lexical", None),
        nil_declared=bool(getattr(evidence, "nil_declared", False)),
        id_kind=id_kind,
    )


def _attribute_annotation(
    node: XdmNode,
    *,
    type_name: str | None,
    actual_type: Any,
    typed_values: tuple[Any, ...],
    governing_declaration: Any = None,
    id_kind: str | None = None,
) -> OccurrenceAnnotation:
    return OccurrenceAnnotation(
        type_name=type_name,
        nilled=None,
        validity="valid",
        actual_type=actual_type,
        validation_attempted="full",
        governing_declaration=governing_declaration,
        is_attribute=True,
        typed_values=typed_values,
        id_kind=id_kind,
    )


def _annotate_any_type(node: XdmNode) -> None:
    node._apply_occurrence_annotation(
        _element_annotation(
            node,
            type_name=_ANY_TYPE,
            actual_type=xsd.AnyType,
            typed_values=(XdmAtomic.untyped_atomic(node.string_value),),
        )
    )


def _annotate_untyped_atomic_attribute(node: XdmNode) -> None:
    node._apply_occurrence_annotation(
        _attribute_annotation(
            node,
            type_name=UNTYPED_ATOMIC,
            actual_type=None,
            typed_values=(XdmAtomic.untyped_atomic(node.string_value),),
            id_kind=getattr(node.annotation, "id_kind", None),
        )
    )


def _strip_subtree(root: XdmNode) -> None:
    """Replace every element/attribute annotation below *root* with untyped."""
    stack: list[XdmNode] = [root]
    while stack:
        node = stack.pop()
        if node.kind is NodeKind.ELEMENT:
            node._apply_occurrence_annotation(
                OccurrenceAnnotation(
                    type_name=UNTYPED,
                    nilled=False,
                    validation_attempted="none",
                    typed_values=(XdmAtomic.untyped_atomic(node.string_value),),
                    xsi_type=getattr(node.annotation, "xsi_type", None),
                    xsi_type_lexical=getattr(node.annotation, "xsi_type_lexical", None),
                    nil_lexical=getattr(node.annotation, "nil_lexical", None),
                    nil_declared=bool(getattr(node.annotation, "nil_declared", False)),
                    id_kind=getattr(node.annotation, "id_kind", None),
                )
            )
            stack.extend(reversed(node.children))
            stack.extend(node.attributes)
        elif node.kind is NodeKind.ATTRIBUTE:
            _annotate_untyped_atomic_attribute(node)
        else:
            stack.extend(reversed(node.children))


# ---------------------------------------------------------------------------
# Helpers


def _resolve_qname(lexical: str, namespaces: Any) -> str | None:
    text = lexical.strip()
    prefix, separator, local = text.partition(":")
    if separator:
        uri = namespaces.get(prefix)
        if uri is None:
            return None
        return clark(uri, local)
    uri = namespaces.get("")
    return clark(uri, text) if uri else text


def _normalize_empty_text(element: Any) -> None:
    """Treat an absent character-data run as no text, not an empty string.

    ``element_from_xdm`` materializes an empty string when an element has no
    text children.  pyxsd's binder distinguishes that from an absent text node
    when applying an element declaration's default value, so construction
    validation normalizes the structural conversion without serializing it.
    """
    stack = [element]
    while stack:
        current = stack.pop()
        if current.text == "":
            current.text = None
        if current.tail == "":
            current.tail = None
        stack.extend(current)


def _parent_index(element: Any) -> dict[int, Any]:
    parents: dict[int, Any] = {}
    stack = [element]
    while stack:
        parent = stack.pop()
        for child in parent:
            parents[id(child)] = parent
            stack.append(child)
    return parents


def _instance_issues(report: Any) -> tuple[Any, ...]:
    """Every instance-phase error, before any document-level filtering."""
    from pyxsd.validation import IssueSeverity

    return tuple(
        issue
        for issue in report
        if issue.phase == "instance" and issue.severity is IssueSeverity.ERROR
    )


def _instance_errors(report: Any, *, document: bool) -> tuple[Any, ...]:
    errors = _instance_issues(report)
    if document:
        return errors
    return tuple(issue for issue in errors if getattr(issue, "code", None) not in _ID_CODES)
