"""Instance-side checking of XSD identity constraints.

After the instance document is bound, every schema element
declaration that carries ``xs:key``, ``xs:unique`` or ``xs:keyref``
constraints is checked against the bound instance tree. Each bound
instance records the element representative it was built from in
``_descriptor_``, which is what connects the schema-side constraints
to the instance-side nodes.

The supported XPath subset covers the common shapes used in identity
constraints:

- child steps separated by ``/`` (``item``, ``order/line``),
- ``.`` for the context node (``./item`` is the same as ``item``),
- ``*`` as a wildcard child step,
- ``.//`` for descendant-or-self (``.//item``),
- fields ending in ``@attribute``, an element name (the element's
  simple content) or ``.`` (the selected node itself).

Namespace prefixes in steps are ignored (matching is by local name),
consistent with the rest of the parser. Predicates (``[...]``) and
absolute paths (``/``) are not supported and cause the constraint to
be skipped with a report warning.

Values are compared by their string (lexical) form: attribute values
come from the bound lexical form or a typed fallback (which covers
applied schema defaults), element fields use the bound value.
"""

import logging
from typing import Any

from pyxsd.schema_base import SchemaBase
from pyxsd.validation import ValidationReport
from pyxsd.xsd_data_types import XsdDataType

logger = logging.getLogger(__name__)

_MISSING: Any = object()
_UNSUPPORTED: Any = object()


def check_identity_constraints(rootInstance: Any, report: ValidationReport) -> None:
    """Checks every identity constraint reachable from the root instance.

    - ``rootInstance``: the bound root instance (or ``None``).
    - ``report``: the :class:`~pyxsd.validation.ValidationReport` that
      collects the findings.

    Keys and uniques are collected first (over the whole tree), then
    keyrefs are resolved against the collected values. A keyref whose
    ``refer`` names no collected key or unique is an error.
    """
    if rootInstance is None:
        return None
    keyValues: dict[str, set[tuple[str, ...]]] = {}
    keyrefRecords: list[tuple[Any, tuple[str, ...]]] = []
    _walk(rootInstance, keyValues, keyrefRecords, report)
    for constraint, values in keyrefRecords:
        _checkKeyref(constraint, values, keyValues, report)
    return None


def _walk(
    instance: Any,
    keyValues: dict[str, set[tuple[str, ...]]],
    keyrefRecords: list[tuple[Any, tuple[str, ...]]],
    report: ValidationReport,
) -> None:
    """Applies the constraints of ``instance`` and recurses downward."""
    descriptor = getattr(instance, "_descriptor_", None)
    if descriptor is not None:
        for constraint in getattr(descriptor, "identities", []):
            _applyConstraint(instance, constraint, keyValues, keyrefRecords, report)
    for child in getattr(instance, "_children_", None) or []:
        if isinstance(child, SchemaBase):
            _walk(child, keyValues, keyrefRecords, report)
    return None


def _applyConstraint(
    node: Any,
    constraint: Any,
    keyValues: dict[str, set[tuple[str, ...]]],
    keyrefRecords: list[tuple[Any, tuple[str, ...]]],
    report: ValidationReport,
) -> None:
    """Evaluates one constraint against the children of one node."""
    kind = constraint.__class__.__name__
    selected = _selectNodes(node, constraint, report)
    if selected is None:
        return None
    seen = None
    if kind in ("Key", "Unique"):
        seen = keyValues.setdefault(constraint.constraintName, set())
    for selectedNode in selected:
        values = []
        missingField = None
        for fieldPath in constraint.fieldPaths:
            value = _evalField(selectedNode, fieldPath, constraint, report)
            if value is _UNSUPPORTED:
                # The constraint cannot be checked; the warning is
                # already on the report.
                return None
            if value is _MISSING:
                missingField = fieldPath
                break
            values.append(value)
        if missingField is not None:
            if kind == "Key":
                report.add_error(
                    f"key '{constraint.constraintName}': field '{missingField}' "
                    f"has no value on the selected '{_nameOf(selectedNode)}' element",
                    code="identity-key",
                    element=_nameOf(node),
                )
            # unique and keyref ignore nodes where a field is absent.
            continue
        valueTuple = tuple(values)
        if kind == "Keyref":
            keyrefRecords.append((constraint, valueTuple))
            continue
        if seen is not None and valueTuple in seen:
            report.add_error(
                f"{kind.lower()} '{constraint.constraintName}': duplicate value "
                f"{_formatValues(valueTuple)} on the selected '{_nameOf(selectedNode)}' element",
                code="identity-key" if kind == "Key" else "identity-unique",
                element=_nameOf(node),
            )
            continue
        if seen is not None:
            seen.add(valueTuple)
    return None


def _checkKeyref(
    constraint: Any,
    values: tuple[str, ...],
    keyValues: dict[str, set[tuple[str, ...]]],
    report: ValidationReport,
) -> None:
    """Reports a keyref record that matches no collected key or unique."""
    referLocal = constraint.refer.split(":")[-1]
    known = keyValues.get(referLocal)
    if known is None:
        report.add_error(
            f"keyref '{constraint.constraintName}' refers to '{constraint.refer}', "
            "but no key or unique with that name was found in the schema",
            code="identity-keyref",
            element=constraint.constraintName,
        )
        return None
    if values not in known:
        report.add_error(
            f"keyref '{constraint.constraintName}': value {_formatValues(values)} "
            f"does not match any value of the '{constraint.refer}' constraint",
            code="identity-keyref",
            element=constraint.constraintName,
        )
    return None


def _selectNodes(node: Any, constraint: Any, report: ValidationReport) -> list[Any] | None:
    """Returns the nodes a constraint's selector covers, or ``None``.

    ``None`` means the selector could not be evaluated (unsupported
    construct); a warning has already been recorded in that case.
    """
    selector = constraint.selector
    if not selector:
        report.add_warning(
            f"identity constraint '{constraint.constraintName}' has no selector; "
            "it will not be checked",
            code="identity-unsupported",
        )
        return None
    if "[" in selector:
        report.add_warning(
            f"the selector '{selector}' of identity constraint "
            f"'{constraint.constraintName}' uses a predicate, which pyxsd does "
            "not support; the constraint will not be checked",
            code="identity-unsupported",
        )
        return None
    descendant, steps = _parsePath(selector)
    if steps is None:
        report.add_warning(
            f"the selector '{selector}' of identity constraint "
            f"'{constraint.constraintName}' is not a supported path; the "
            "constraint will not be checked",
            code="identity-unsupported",
        )
        return None
    if descendant:
        return [
            match
            for candidate in _descendantOrSelfNodes(node)
            for match in _evalSteps(candidate, steps)
        ]
    return _evalSteps(node, steps)


def _evalField(selectedNode: Any, fieldPath: str, constraint: Any, report: ValidationReport) -> Any:
    """Returns the string value of one field of one selected node.

    Returns ``_MISSING`` when the field has no value on this node.
    """
    if "[" in fieldPath:
        report.add_warning(
            f"the field '{fieldPath}' of identity constraint "
            f"'{constraint.constraintName}' uses a predicate, which pyxsd does "
            "not support; the constraint will not be checked",
            code="identity-unsupported",
        )
        return _UNSUPPORTED
    descendant, steps = _parsePath(fieldPath)
    if steps is None:
        report.add_warning(
            f"the field '{fieldPath}' of identity constraint "
            f"'{constraint.constraintName}' is not a supported path; the "
            "constraint will not be checked",
            code="identity-unsupported",
        )
        return _UNSUPPORTED
    if steps and steps[-1].startswith("@"):
        attributeName = steps[-1][1:]
        if len(steps) > 1:
            nodes = (
                [
                    match
                    for candidate in _descendantOrSelfNodes(selectedNode)
                    for match in _evalSteps(candidate, steps[:-1])
                ]
                if descendant
                else _evalSteps(selectedNode, steps[:-1])
            )
        else:
            nodes = [selectedNode] if not descendant else list(_descendantOrSelfNodes(selectedNode))
        for node in nodes:
            value = _attributeValue(node, attributeName)
            if value is not None:
                return value
        return _MISSING
    # A field ending in ``.`` was already reduced to the element steps
    # before it (or nothing at all, meaning the selected node itself)
    # by ``_parsePath``, so the remaining case is a field naming an
    # element: the value is that element's simple content.
    nodes = (
        [
            match
            for candidate in _descendantOrSelfNodes(selectedNode)
            for match in _evalSteps(candidate, steps)
        ]
        if descendant
        else _evalSteps(selectedNode, steps)
    )
    for node in nodes:
        value = _nodeValue(node)
        if value is not None:
            return value
    return _MISSING


def _parsePath(path: str) -> tuple[bool, list[str] | None]:
    """Splits an XPath-subset path into (descendant, steps).

    Returns ``(descendant, steps)`` where ``steps`` is the list of
    child steps (prefixes stripped, ``.`` steps removed), or
    ``(False, None)`` when the path is not supported.
    """
    if path.startswith("/"):
        return False, None
    parts = path.split("/")
    descendant = any(part == "" for part in parts)
    steps = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "*":
            steps.append("*")
        else:
            steps.append(part.split(":")[-1])
    return descendant, steps


def _evalSteps(node: Any, steps: list[str]) -> list[Any]:
    """Walks child steps from ``node`` and returns the matching nodes."""
    nodes = [node]
    for step in steps:
        nextNodes = []
        for current in nodes:
            for child in _childrenOf(current):
                childName = _nameOf(child)
                if step == "*" or childName == step:
                    nextNodes.append(child)
        nodes = nextNodes
    return nodes


def _descendantOrSelfNodes(node: Any) -> list[Any]:
    """Returns ``node`` and all its bound complex descendants."""
    nodes = [node]
    index = 0
    while index < len(nodes):
        current = nodes[index]
        index += 1
        nodes.extend(child for child in _childrenOf(current) if isinstance(child, SchemaBase))
    return nodes


def _childrenOf(node: Any) -> list[Any]:
    """Returns the bound child instances of a bound node."""
    return list(getattr(node, "_children_", None) or [])


def _nameOf(node: Any) -> str:
    """Returns the element name of a bound child instance, if known."""
    name = getattr(node, "_name_", None)
    if name is not None:
        return name
    descriptor = getattr(node, "_descriptor_", None)
    if descriptor is not None:
        return descriptor.name
    return node.__class__.__name__ if not isinstance(node, XsdDataType) else "?"


def _attributeValue(node: Any, attributeName: str) -> str | None:
    """Returns the bound value of one attribute, or ``None``."""
    attribs = getattr(node, "_attribs_", None)
    if attribs:
        value = attribs.get(attributeName)
        if value is not None:
            return str(value)
    value = node.__dict__.get(attributeName)
    if value is not None:
        return str(value)
    return None


def _nodeValue(node: Any) -> str | None:
    """Returns the simple-content value of a bound node, or ``None``."""
    if isinstance(node, XsdDataType):
        return str(node)
    if isinstance(node, SchemaBase):
        value = getattr(node, "_value_", None)
        if isinstance(value, list):
            return str(value[0]) if value else None
    return None


def _formatValues(values: tuple[str, ...]) -> str:
    """Formats a field-value tuple for report messages."""
    if len(values) == 1:
        return repr(values[0])
    return "(" + ", ".join(repr(value) for value in values) + ")"
