"""Occurrence-level schema annotations for grounded XDM nodes.

The XDM node layer in :mod:`pyxsd.xdm.nodes` parses source XML without a
schema and records only the source evidence it can see (``xsi:type`` and
``xsi:nil`` spellings plus untyped defaults).  This module adds the
schema-aware layer: one immutable :class:`OccurrenceAnnotation` per
element and relevant attribute occurrence, keyed by the source occurrence
(expanded name plus same-name sibling index) rather than by expanded name.
Repeated siblings, local declarations, anonymous types, wildcards,
substitution groups, lists, and unions therefore stay distinct.

Capture is opt-in.  :func:`capturing_occurrences` installs a collection
sink for the duration of one binding run; without it, the binder hooks in
:mod:`pyxsd.instance_binding` and :mod:`pyxsd.schema_base` return after a
single context lookup and allocate nothing.  Ordinary parsing keeps its
existing behavior and defaults.

The module never imports a stylesheet or an XPath engine.  It is the
schema seam between pyxsd binding and the XDM node layer; the elementpath
adapters live in :mod:`pyxsd.xdm.schema`.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from pyxsd import xsd_data_types as xsd
from pyxsd.locations import PathStep
from pyxsd.namespaces import XSD_NS, clark
from pyxsd.schema_context import current_context
from pyxsd.xdm.values import XdmAtomic, XdmItem, as_xdm

__all__ = [
    "OccurrenceAnnotation",
    "OccurrenceCapture",
    "OccurrenceKey",
    "capture_attribute_occurrence",
    "capture_element_occurrence",
    "capture_remaining_attributes",
    "capture_schema_annotations",
    "capturing_occurrences",
    "occurrence_capture_active",
    "schema_type_name",
]

UNTYPED = f"{{{XSD_NS}}}untyped"
UNTYPED_ATOMIC = f"{{{XSD_NS}}}untypedAtomic"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

_CAPTURE: contextvars.ContextVar[OccurrenceCapture | None] = contextvars.ContextVar(
    "pyxsd_occurrence_capture", default=None
)
_BUILTIN_CLASSES: dict[str, type[Any]] | None = None


@dataclass(frozen=True, slots=True)
class OccurrenceKey:
    """The source identity of one element or attribute occurrence."""

    path: tuple[PathStep, ...]
    attribute: str | None = None


@dataclass(frozen=True, slots=True)
class OccurrenceAnnotation:
    """One element or attribute occurrence's schema-instance information.

    ``type_name`` is the XDM type annotation (a Clark QName, or ``None``
    for an anonymous type); ``actual_type`` is the pyxsd generated type
    class that governs the occurrence, which keeps anonymous types and
    repeated occurrences distinct by object identity.  ``validity`` and
    ``validation_attempted`` use the XSD PSVI vocabulary.  Source evidence
    from Task 8 (``xsi_type``, ``nil_lexical``, ``nil_declared``) is kept
    alongside, never folded into the schema-aware properties.
    """

    type_name: str | None = None
    nilled: bool | None = False
    validity: str = "not-validated"
    governing_declaration: Any = None
    xsi_type: str | None = None
    nil_lexical: str | None = None
    nil_declared: bool = False
    actual_type: Any = None
    validation_attempted: str = "none"
    local_declaration: bool = False
    anonymous_type: bool = False
    wildcard: Any = None
    substitution_group_head: Any = None
    repeated: bool = False
    is_attribute: bool = False
    typed_values: tuple[Any, ...] = ()
    default_value: str | None = None
    fixed_value: str | None = None
    value_source: str | None = None
    id_kind: str | None = None

    @property
    def actual_type_name(self) -> str | None:
        """The actual type's Clark name, when it has one."""
        return self.type_name


class OccurrenceCapture:
    """One binding run's occurrence annotations, before XDM attachment."""

    __slots__ = ("_annotations",)

    def __init__(self) -> None:
        self._annotations: dict[OccurrenceKey, OccurrenceAnnotation] = {}

    @property
    def annotations(self) -> Mapping[OccurrenceKey, OccurrenceAnnotation]:
        """A read-only view of every captured occurrence."""
        return MappingProxyType(self._annotations)

    def record(self, key: OccurrenceKey, annotation: OccurrenceAnnotation) -> None:
        """Record or refine one occurrence; the latest capture wins."""
        self._annotations[key] = annotation

    def annotation(
        self, key: OccurrenceKey | tuple[PathStep, ...], attribute: str | None = None
    ) -> OccurrenceAnnotation | None:
        """Return the annotation for one occurrence, or ``None``."""
        if not isinstance(key, OccurrenceKey):
            key = OccurrenceKey(key, attribute)
        return self._annotations.get(key)

    def finish(self, report: Any) -> None:
        """Fold the binding report's exact errors into per-occurrence validity.

        Validation is full for declared/assessed occurrences and partial or
        absent for wildcard content.  An exact error marks the occurrence
        invalid; an attribute error also invalidates its owning element.
        """
        invalid: set[OccurrenceKey] = set()
        for issue in getattr(report, "errors", ()):
            location = getattr(issue, "location", None)
            if location is None:
                continue
            key = OccurrenceKey(location.path, location.attribute)
            invalid.add(key)
            if location.attribute is not None:
                invalid.add(OccurrenceKey(location.path))
        for key, annotation in tuple(self._annotations.items()):
            if key in invalid:
                validity = "invalid"
            elif annotation.validation_attempted == "full":
                validity = "valid"
            else:
                validity = "notKnown"
            self._annotations[key] = replace(annotation, validity=validity)

    def apply(self, document: Any) -> None:
        """Attach matching annotations to an XDM document's nodes.

        Only source paths present in this capture are touched; nodes from
        another occurrence (even with the same expanded name) keep the
        unvalidated Task 8 annotation.
        """
        root = getattr(document, "root_element", None)
        if root is None:
            return
        for node in (root, *root.descendants):
            if node.kind != "element":
                continue
            location = node.source_location()
            if location is not None:
                annotation = self._annotations.get(OccurrenceKey(location.path))
                if annotation is not None:
                    node._apply_occurrence_annotation(annotation)
            for attribute in node.attributes:
                location = attribute.source_location()
                if location is None:
                    continue
                annotation = self._annotations.get(OccurrenceKey(location.path, location.attribute))
                if annotation is not None:
                    attribute._apply_occurrence_annotation(annotation)


def occurrence_capture_active() -> bool:
    """Whether a binding run has opted into occurrence capture."""
    return _CAPTURE.get() is not None


@contextmanager
def capturing_occurrences() -> Iterator[OccurrenceCapture]:
    """Collect occurrence annotations for the enclosed binding run."""
    capture = OccurrenceCapture()
    token = _CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _CAPTURE.reset(token)


def capture_schema_annotations(schema: Any, source: Any) -> OccurrenceCapture:
    """Bind *source* against *schema* and return its occurrence capture.

    The caller owns the source; a stream must be seekable or fresh because
    binding consumes it.  The result is not attached to an XDM document
    automatically: :meth:`OccurrenceCapture.apply` performs the
    occurrence-identity match, which the document-capture layer uses when
    it has an XDM tree for the same source.
    """
    with capturing_occurrences() as capture:
        document = schema.parse(source)
    capture.finish(document.report)
    return capture


# ---------------------------------------------------------------------------
# Capture helpers used by the binder


def capture_element_occurrence(
    element: Any,
    *,
    governing_declaration: Any = None,
    actual_type: Any = None,
    nilled: bool = False,
    validation_attempted: str = "full",
    wildcard: Any = None,
    substitution_group_head: Any = None,
    repeated: bool = False,
    bound: Any = None,
) -> None:
    """Capture one element occurrence for the active sink, if any."""
    capture = _CAPTURE.get()
    if capture is None:
        return
    key = _key_for(element)
    if key is None:
        return
    declaration = governing_declaration
    default_value = _declaration_constraint(declaration, "getDefault")
    fixed_value = _declaration_constraint(declaration, "getFixed")
    empty = element.text is None and len(element) == 0
    value_source = _value_source(declaration, empty, default_value, fixed_value)
    nil_lexical = element.get(f"{{{_XSI_NS}}}nil")
    nil_declared = nil_lexical is not None and nil_lexical.strip() in {"true", "1"}
    schema_name = schema_type_name(actual_type)
    type_name: str | None = UNTYPED if validation_attempted != "full" else schema_name
    capture.record(
        key,
        OccurrenceAnnotation(
            type_name=type_name,
            nilled=bool(nilled),
            governing_declaration=declaration,
            xsi_type=element.get(f"{{{_XSI_NS}}}type"),
            nil_lexical=nil_lexical,
            nil_declared=nil_declared,
            actual_type=actual_type,
            validation_attempted=validation_attempted,
            local_declaration=_is_local_declaration(declaration),
            anonymous_type=is_anonymous_type(actual_type),
            wildcard=wildcard,
            substitution_group_head=substitution_group_head,
            repeated=bool(repeated) or _declaration_is_list(declaration),
            is_attribute=False,
            typed_values=_element_typed_values(
                bound, element, nilled, actual_type, validation_attempted
            ),
            default_value=default_value,
            fixed_value=fixed_value,
            value_source=value_source,
            id_kind=id_kind_for(actual_type),
        ),
    )


def capture_attribute_occurrence(
    element: Any,
    name: str,
    *,
    governing_declaration: Any = None,
    actual_type: Any = None,
    value: Any = None,
    wildcard: Any = None,
    validation_attempted: str = "full",
    default_value: str | None = None,
    fixed_value: str | None = None,
) -> None:
    """Capture one attribute occurrence for the active sink, if any."""
    capture = _CAPTURE.get()
    if capture is None:
        return
    key = _key_for(element, attribute=name)
    if key is None:
        return
    if default_value is None:
        default_value = _declaration_constraint(governing_declaration, "getDefault")
    if fixed_value is None:
        fixed_value = _declaration_constraint(governing_declaration, "getFixed")
    if validation_attempted != "full" or actual_type is None:
        type_name: str | None = UNTYPED_ATOMIC
    else:
        type_name = schema_type_name(actual_type)
    capture.record(
        key,
        OccurrenceAnnotation(
            type_name=type_name,
            nilled=None,
            governing_declaration=governing_declaration,
            actual_type=actual_type,
            validation_attempted=validation_attempted,
            local_declaration=_is_local_declaration(governing_declaration),
            anonymous_type=is_anonymous_type(actual_type),
            wildcard=wildcard,
            is_attribute=True,
            typed_values=_attribute_typed_values(value, actual_type),
            default_value=default_value,
            fixed_value=fixed_value,
            value_source=(
                "instance" if default_value is not None or fixed_value is not None else None
            ),
            id_kind=id_kind_for(actual_type),
        ),
    )


def capture_remaining_attributes(element: Any) -> None:
    """Capture attributes with no declaration/wildcard annotation yet.

    A simple-typed root, or an attribute outside every wildcard, still has
    a source occurrence.  Recording it leaves the occurrence queryable
    instead of silently absent.
    """
    capture = _CAPTURE.get()
    if capture is None:
        return
    key = _key_for(element)
    if key is None:
        return
    for name, value in element.attrib.items():
        if "xmlns" in name:
            continue
        attribute_key = OccurrenceKey(key.path, name)
        if attribute_key in capture._annotations:
            continue
        capture.record(
            attribute_key,
            OccurrenceAnnotation(
                type_name=UNTYPED_ATOMIC,
                nilled=None,
                actual_type=None,
                validation_attempted="none",
                is_attribute=True,
                typed_values=_atomize(value),
            ),
        )


# ---------------------------------------------------------------------------
# Type and value helpers


def schema_type_name(type_class: Any) -> str | None:
    """Return a generated type's Clark name, or ``None`` for anonymous types.

    Built-in XSD datatypes carry no target namespace of their own; their
    name is in the XML Schema namespace.  Generated anonymous types use a
    ``parent|kind`` bookkeeping name, which is not a QName.
    """
    if type_class is None:
        return None
    builtin_names = _builtin_classes()
    if type_class in builtin_names.values():
        name = type_class.__dict__.get("name")
    else:
        # A generated complex class may carry an element descriptor named
        # ``name``, shadowing the class's bookkeeping attribute; the type
        # factory always sets ``__name__`` to the schema type name.
        name = getattr(type_class, "__name__", None)
    if not isinstance(name, str) or not name or "|" in name:
        return None
    if type_class in builtin_names.values():
        return clark(XSD_NS, name)
    namespace = getattr(type_class, "_targetNamespace_", None)
    if namespace is None:
        schema = getattr(type_class, "schema", None)
        namespace = getattr(schema, "target_namespace", None)
    if namespace is None:
        namespace = getattr(type_class, "namespace", None)
    return clark(namespace, name)


def is_anonymous_type(type_class: Any) -> bool:
    """Whether *type_class* is a schema-generated inline (anonymous) type."""
    if type_class is None or type_class in _builtin_classes().values():
        return False
    name = getattr(type_class, "__name__", None)
    return isinstance(name, str) and "|" in name


def id_kind_for(type_class: Any) -> str | None:
    """Classify an ID/IDREF-derived type for the PSVI ID/IDREF table."""
    if type_class is None:
        return None
    names: set[str] = set()
    for base in getattr(type_class, "__mro__", ()):
        name = base.__dict__.get("name")
        if isinstance(name, str):
            names.add(name)
        names.add(base.__name__)
    for candidate in ("IDREFS", "ENTITIES", "IDREF", "ENTITY", "ID"):
        if candidate in names:
            return candidate
    return None


def _builtin_classes() -> dict[str, type[Any]]:
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


def _declaration_constraint(declaration: Any, getter: str) -> str | None:
    if declaration is None:
        return None
    method = getattr(declaration, getter, None)
    if method is None:
        return None
    try:
        value = method()
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _declaration_is_list(declaration: Any) -> bool:
    method = getattr(declaration, "isList", None)
    if method is None:
        return False
    try:
        return bool(method())
    except Exception:
        return False


def _is_local_declaration(declaration: Any) -> bool:
    method = getattr(declaration, "isGlobalDeclaration", None)
    if method is None:
        return False
    try:
        return not bool(method())
    except Exception:
        return False


def _value_source(
    declaration: Any,
    empty: bool,
    default_value: str | None,
    fixed_value: str | None,
) -> str | None:
    if empty and fixed_value is not None:
        return "fixed"
    if empty and default_value is not None:
        return "default"
    if default_value is not None or fixed_value is not None:
        return "instance"
    return None


def _key_for(element: Any, *, attribute: str | None = None) -> OccurrenceKey | None:
    context = current_context()
    if context is None or context.provenance is None:
        return None
    location = context.provenance.source_location(element)
    if location is None:
        return None
    return OccurrenceKey(location.path, attribute)


def _string_value(element: Any) -> str:
    itertext = getattr(element, "itertext", None)
    if itertext is not None:
        return "".join(itertext())
    return element.text or ""


def _element_typed_values(
    bound: Any,
    element: Any,
    nilled: bool,
    actual_type: Any,
    validation_attempted: str,
) -> tuple[Any, ...]:
    if nilled:
        return ()
    if validation_attempted != "full" or actual_type is None:
        return (XdmAtomic.untyped_atomic(_string_value(element)),)
    if actual_type is xsd.AnyType:
        return (XdmAtomic.untyped_atomic(_string_value(element)),)
    content_kind = getattr(actual_type, "_contentKind_", None)
    is_complex = content_kind == "complex" or (
        content_kind is None and is_anonymous_type(actual_type)
    )
    if is_complex:
        simple_content = getattr(actual_type, "_simpleContentType_", None)
        if isinstance(simple_content, type):
            try:
                with _qname_context_for(actual_type, element):
                    return _atomize(simple_content(_string_value(element)))
            except (TypeError, ValueError):
                return (XdmAtomic.untyped_atomic(_string_value(element)),)
        if getattr(actual_type, "_elementOnly_", False):
            return ()
        return (XdmAtomic.untyped_atomic(_string_value(element)),)
    if bound is not None:
        return _atomize(bound)
    return (XdmAtomic.untyped_atomic(_string_value(element)),)


def _attribute_typed_values(value: Any, actual_type: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(actual_type, type):
        try:
            return _atomize(actual_type(value))
        except (TypeError, ValueError):
            pass
    return _atomize(value)


def _atomize(value: Any) -> tuple[Any, ...]:
    """Convert one bound value to an XDM typed-value sequence.

    XSD lists atomize to their items; unions expose the validated member
    value; generated atomic classes keep their Clark type name.
    """
    if value is None:
        return ()
    if isinstance(value, XdmItem):
        return (value,)
    if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
        items: list[Any] = []
        for member in value:
            items.extend(_atomize(member))
        return tuple(items)
    string_list_item = _string_list_item_class(type(value))
    if string_list_item is not None:
        return tuple(_atomize(string_list_item(token)) for token in value.tokens)
    if isinstance(value, xsd.XsdDataType):
        if getattr(type(value), "_unionMembers", None):
            member = getattr(value, "memberValue", None)
            if member is not None and member is not value:
                return _atomize(member)
        type_name = schema_type_name(type(value))
        if type_name is None:
            return _atomize_anonymous_atomic(value)
        return (XdmAtomic(value, type_name, type(value)),)
    try:
        return as_xdm(value)
    except TypeError:
        return (XdmAtomic.untyped_atomic(str(value)),)


def _string_list_item_class(value_class: type[Any]) -> type[Any] | None:
    """The item datatype of the built-in string-backed list types."""
    name = value_class.__dict__.get("name") or value_class.__name__
    return {
        "IDREFS": xsd.IDREF,
        "ENTITIES": xsd.ENTITY,
        "NMTOKENS": xsd.NMTOKEN,
    }.get(name)


def _atomize_anonymous_atomic(value: Any) -> tuple[Any, ...]:
    """Represent an anonymous atomic value with its primitive XSD type."""
    for base in type(value).__mro__[1:]:
        if base in _builtin_classes().values():
            try:
                converted = base(str(value))
            except (TypeError, ValueError):
                break
            return (XdmAtomic.typed(converted, base),)
    return (XdmAtomic.untyped_atomic(str(value)),)


def _qname_context_for(actual_type: Any, element: Any) -> Any:
    """Resolve QName-valued simple content through the active binding scope."""
    bindings_getter = getattr(actual_type, "_qname_bindings", None)
    bindings = None
    if bindings_getter is not None:
        try:
            bindings = bindings_getter(element)
        except Exception:
            bindings = None
    from pyxsd.xsd_data_types import qname_context

    return qname_context(bindings)
