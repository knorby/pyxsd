"""Current-serialized snapshots and projected-value validation, without extras."""

from __future__ import annotations

import base64
from decimal import Decimal
from typing import Any

from pyxsd import xsd_data_types as xd
from pyxsd.document import Document
from pyxsd.schema import Schema

from . import IntegrationError
from ._shape import ElementShape, list_item_type, particle_elements, scalar_kind


def prepare_document(schema: Schema, document: Document, revalidate: bool) -> Document:
    if document.schema is not schema:
        raise IntegrationError("document belongs to another compiled schema")
    schema.require_valid()
    if revalidate:
        document = document.revalidate()
    document.require_valid()
    if document.root is None:
        raise IntegrationError("document has no bound root")
    return document


def text_of(node: Any) -> str:
    value = getattr(node, "_value_", None)
    if isinstance(value, list):
        return "".join(v.lexical() if isinstance(v, xd.XsdDataType) else str(v) for v in value)
    return "" if value is None else str(value)


def plain_value(cls: type, value: Any) -> Any:
    kind = scalar_kind(cls)
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(value)
    if kind == "decimal":
        return Decimal(value)
    if kind == "float":
        return float(value)
    if kind == "base64":
        return base64.b64decode(str(value), validate=True)
    if kind == "hex":
        return bytes.fromhex(str(value))
    if kind == "qname":
        if not getattr(value, "_resolved_", False):
            raise IntegrationError("QName lacks trustworthy namespace context")
        uri = value._uri_
        return f"{{{uri}}}{value._local_}" if uri else value._local_
    if kind == "list":
        items = value if isinstance(value, list) else value.tokens
        return [plain_value(list_item_type(cls), item) for item in items]
    return str(value)


def lexical_value(cls: type, lexical: str, *, evidence: Any = None, declaration: Any = None) -> Any:
    try:
        if scalar_kind(cls) == "qname":
            if (
                evidence is not None
                and str(evidence) == lexical
                and getattr(evidence, "_resolved_", False)
            ):
                return plain_value(cls, evidence)
            if declaration is not None:
                context = declaration.getSchema().namespaceContext
                with xd.qname_context(context.bindings_for(declaration.xsdElement)):
                    value = cls(lexical)
                    if ":" in lexical and not value._uri_:
                        raise IntegrationError("QName prefix has no current namespace binding")
                    return plain_value(cls, value)
            raise IntegrationError("QName lacks trustworthy current namespace context")
        return plain_value(cls, cls(lexical))
    except (TypeError, ValueError) as exc:
        raise IntegrationError(f"invalid {cls.__name__} value {lexical!r}: {exc}") from exc


def check_counts(shape: ElementShape, counts: dict[int, int]) -> None:
    def walk(p: Any) -> None:
        if p is None:
            return
        if p.kind == "element":
            count = counts.get(id(p.descriptor), 0)
            if count < p.min_occurs or (p.max_occurs is not None and count > p.max_occurs):
                shape.fail(
                    f"{p.name}: occurrence count {count} violates {p.min_occurs}..{p.max_occurs}"
                )
            return
        present = [
            child
            for child in p.children
            if any(counts.get(id(leaf.descriptor), 0) for leaf in particle_elements(child))
        ]
        if not present and p.min_occurs == 0:
            return
        if p.max_occurs == 0 and present:
            shape.fail("prohibited group contains elements")
        if p.kind == "choice":
            if len(present) != 1:
                shape.fail("choice requires exactly one present branch")
            walk(present[0])
        else:
            for child in p.children:
                walk(child)

    walk(shape.particle)


def project(shape: ElementShape, node: Any, *, explicit: bool = False) -> Any:
    """Project one bound occurrence, reading lexical containers rather than accessors."""
    return _project(shape, node, explicit, set())


def _project(shape: ElementShape, node: Any, explicit: bool, active: set[int]) -> Any:
    if id(node) in active:
        shape.fail("cyclic bound input")
    if getattr(node, "_descriptor_", None) is not shape.declaration:
        shape.fail("node does not match the selected declaration")
    if type(node) is not shape.cls:
        shape.fail("runtime polymorphism is unsupported")
    children = getattr(node, "_children_", []) or []
    lexical = text_of(node)
    nil = getattr(node, "_nil_", False)
    if nil and (not shape.nillable or children or lexical):
        shape.fail("invalid nil state/content")
    if shape.primitive:
        assert shape.scalar is not None
        if nil:
            return None
        if children:
            shape.fail("scalar contains children")
        forced = shape.declaration.getFixed() or shape.declaration.getDefault()
        value = lexical_value(
            shape.scalar,
            lexical if lexical else forced or "",
            evidence=node,
            declaration=shape.declaration if not lexical else None,
        )
        if shape.declaration.getFixed() is not None:
            fixed = lexical_value(
                shape.scalar, shape.declaration.getFixed(), declaration=shape.declaration
            )
            if value != fixed:
                shape.fail("fixed element value violated")
        return value
    data: dict[str, Any] = {}
    if shape.nillable:
        data["$nil"] = bool(nil)
    attributes = getattr(node, "_attribs_", {}) or {}
    known = {a.name for a in shape.attributes}
    for name in attributes:
        if name not in known and not name.startswith(
            ("xmlns", "xsi:", "{http://www.w3.org/2001/XMLSchema-instance}")
        ):
            shape.fail(f"undeclared attribute {name}")
    for attr in shape.attributes:
        present = attr.name in attributes
        if attr.prohibited:
            if present:
                shape.fail(f"prohibited attribute {attr.name}")
            continue
        if attr.required and not present:
            shape.fail(f"required attribute {attr.name} is missing")
        raw = (
            attributes.get(attr.name)
            if present
            else attr.fixed
            if attr.fixed is not None
            else attr.default
        )
        if raw is None:
            continue
        if explicit and not present:
            continue
        evidence = vars(node).get(attr.declaration._storageKey())
        value = lexical_value(
            attr.scalar,
            raw,
            evidence=evidence,
            declaration=attr.declaration if not present else None,
        )
        if attr.fixed is not None and value != lexical_value(
            attr.scalar, attr.fixed, declaration=attr.declaration
        ):
            shape.fail(f"fixed attribute {attr.name} violated")
        data[attr.alias] = value
    if nil:
        return data
    if shape.scalar is not None:
        if children:
            shape.fail("simple content contains children")
        forced = shape.declaration.getFixed() or shape.declaration.getDefault()
        data["$"] = lexical_value(
            shape.scalar,
            lexical if lexical else forced or "",
            evidence=node,
            declaration=shape.declaration if not lexical else None,
        )
        if shape.declaration.getFixed() is not None and data["$"] != lexical_value(
            shape.scalar, shape.declaration.getFixed(), declaration=shape.declaration
        ):
            shape.fail("fixed simple content violated")
    elif lexical:
        shape.fail("unexpected text in element-only content")
    grouped: dict[int, list[Any]] = {id(child.declaration): [] for child in shape.children}
    for child in children:
        key = id(getattr(child, "_descriptor_", None))
        if key not in grouped:
            shape.fail(f"undeclared child {getattr(child, '_name_', '?')}")
        grouped[key].append(child)
    check_counts(shape, {key: len(values) for key, values in grouped.items()})
    for child_shape in shape.children:
        occurrences = grouped[id(child_shape.declaration)]
        values = [
            _project(child_shape, child, explicit, active | {id(node)}) for child in occurrences
        ]
        if child_shape.repeated:
            if values or not explicit:
                data[child_shape.name] = values
        elif values:
            data[child_shape.name] = values[0]
    return data
