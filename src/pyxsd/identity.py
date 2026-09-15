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

Identity constraints are scoped to the element occurrence that owns
them: a repeating element with a key declaration gets an independent
value table per occurrence, and a keyref resolves against the nearest
enclosing scope that defines the referenced constraint. Values are
compared in the XSD value space (see ``xsd_value_key``), so
lexically different spellings of one value match.
"""

import logging
from typing import Any

from pyxsd.schema_base import SchemaBase
from pyxsd.validation import ValidationReport
from pyxsd.xsd_data_types import XsdDataType, xsd_comparable_key

logger = logging.getLogger(__name__)

_MISSING: Any = object()
_UNSUPPORTED: Any = object()
_AMBIGUOUS: Any = object()

KeyScopes = tuple[dict[str, set[tuple[Any, ...]]], ...]


def check_identity_constraints(rootInstance: Any, report: ValidationReport) -> None:
    """Checks every identity constraint reachable from the root instance.

    - ``rootInstance``: the bound root instance (or ``None``).
    - ``report``: the :class:`~pyxsd.validation.ValidationReport` that
      collects the findings.

    Constraints are evaluated in scoped passes: keys/uniques are
    collected for each owning element occurrence and pushed onto the
    scope chain, then keyrefs on the same occurrence (or a descendant)
    resolve against the nearest scope that defines the referenced
    constraint.
    """
    if rootInstance is None:
        return None
    _walk(rootInstance, (), report)
    return None


def _walk(instance: Any, scopes: KeyScopes, report: ValidationReport) -> None:
    """Applies the constraints of ``instance`` and recurses downward.

    A subtree bound by a ``processContents="skip"`` wildcard is skipped
    (XSD 1.1 §3.3.4.2): the walk neither applies the (absent)
    declaration's constraints to it nor lets its descendants serve as
    key/unique/keyref selections. ``_childrenOf`` hides skipped
    subtrees, so this guard only fires when the walk starts inside one.
    """
    if getattr(instance, "_skipped_", False):
        return None
    descriptor = getattr(instance, "_descriptor_", None)
    identities = list(getattr(descriptor, "identities", []) or []) if descriptor is not None else []

    localKeys: dict[str, set[tuple[Any, ...]]] = {}
    keyrefs: list[Any] = []
    for constraint in identities:
        kind = constraint.__class__.__name__
        if kind in ("Key", "Unique"):
            values = _collectKeyValues(instance, constraint, kind, report)
            if values is not None:
                localKeys[constraint.constraintName] = values
        elif kind == "Keyref":
            keyrefs.append(constraint)

    childScopes = (*scopes, localKeys) if localKeys else scopes
    for constraint in keyrefs:
        _checkKeyref(constraint, instance, childScopes, report)

    for child in getattr(instance, "_children_", None) or []:
        _walk(child, childScopes, report)
    return None


def _collectKeyValues(
    node: Any,
    constraint: Any,
    kind: str,
    report: ValidationReport,
) -> set[tuple[Any, ...]] | None:
    """Collects one key/unique constraint's value tuples for one occurrence.

    Returns ``None`` when the constraint cannot be evaluated (the
    warning is already on the report).
    """
    selected = _selectNodes(node, constraint, report)
    if selected is None:
        return None
    seen: set[tuple[Any, ...]] = set()
    for selectedNode in selected:
        resolved = _fieldValues(selectedNode, constraint, report)
        if resolved is _UNSUPPORTED:
            return None
        if resolved is _MISSING:
            if kind == "Key":
                report.add_error(
                    f"key '{constraint.constraintName}': a selected "
                    f"'{_nameOf(selectedNode)}' element has no value for one of its fields",
                    code="identity-key",
                    element=_nameOf(node),
                )
            # unique ignores nodes where a field is absent.
            continue
        if resolved is _AMBIGUOUS:
            # Already reported by _fieldValues; the constraint fails.
            continue
        if resolved in seen:
            report.add_error(
                f"{kind.lower()} '{constraint.constraintName}': duplicate value "
                f"{_formatValues(resolved)} on the selected "
                f"'{_nameOf(selectedNode)}' element",
                code="identity-key" if kind == "Key" else "identity-unique",
                element=_nameOf(node),
            )
            continue
        seen.add(resolved)
    return seen


def _checkKeyref(
    constraint: Any,
    node: Any,
    scopes: KeyScopes,
    report: ValidationReport,
) -> None:
    """Checks one keyref constraint's records against its key scope."""
    referLocal = constraint.refer.split(":")[-1]
    known = None
    for scope in reversed(scopes):
        if referLocal in scope:
            known = scope[referLocal]
            break
    if known is None:
        report.add_error(
            f"keyref '{constraint.constraintName}' refers to '{constraint.refer}', "
            "but no key or unique with that name was found in the schema",
            code="identity-keyref",
            element=constraint.constraintName,
        )
        return None

    selected = _selectNodes(node, constraint, report)
    if selected is None:
        return None
    for selectedNode in selected:
        resolved = _fieldValues(selectedNode, constraint, report)
        if resolved is _UNSUPPORTED:
            return None
        if resolved in (_MISSING, _AMBIGUOUS):
            # A keyref with a missing/ambiguous field is simply absent.
            continue
        if resolved not in known:
            report.add_error(
                f"keyref '{constraint.constraintName}': value {_formatValues(resolved)} "
                f"does not match any value of the '{constraint.refer}' constraint",
                code="identity-keyref",
                element=constraint.constraintName,
            )
    return None


def _fieldValues(
    selectedNode: Any,
    constraint: Any,
    report: ValidationReport,
) -> Any:
    """Returns the XSD value key for one selected node's fields.

    Returns ``_MISSING`` (no value), ``_AMBIGUOUS`` (a field selects
    more than one value; reported as an error) or ``_UNSUPPORTED``
    (path not supported; warning already recorded).
    """
    values: list[tuple[Any, ...]] = []
    for fieldPath in constraint.fieldPaths:
        result = _evalField(selectedNode, fieldPath, constraint, report)
        if result is _UNSUPPORTED:
            return _UNSUPPORTED
        if not result:
            return _MISSING
        if len(result) > 1:
            report.add_error(
                f"field '{fieldPath}' of identity constraint "
                f"'{constraint.constraintName}' selects more than one value on "
                f"the '{_nameOf(selectedNode)}' element",
                code="identity-key",
                element=_nameOf(selectedNode),
            )
            return _AMBIGUOUS
        values.append(xsd_comparable_key(result[0]))
    return tuple(values)


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
    """Returns the values one field selects on one node.

    A list of zero or more values is returned; ``_UNSUPPORTED`` when the
    path cannot be evaluated. Fields that select more than one value are
    diagnosed by the caller via the list length.
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
        return [
            value
            for node in nodes
            for value in [_attributeValue(node, attributeName)]
            if value is not None
        ]
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
    return [value for node in nodes for value in [_nodeValue(node)] if value is not None]


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
                localName = childName.split("}", 1)[-1] if childName.startswith("{") else childName
                if step == "*" or childName == step or localName == step:
                    nextNodes.append(child)
        nodes = nextNodes
    return nodes


def _descendantOrSelfNodes(node: Any) -> list[Any]:
    """Returns ``node`` and all its bound descendants."""
    nodes = [node]
    index = 0
    while index < len(nodes):
        current = nodes[index]
        index += 1
        nodes.extend(_childrenOf(current))
    return nodes


def _childrenOf(node: Any) -> list[Any]:
    """Returns the bound child instances of a bound node.

    Subtrees bound by a ``processContents="skip"`` wildcard are not
    part of identity-constraint selection (XSD 1.1 §3.3.4.2), so they
    are hidden from the walk, the descendant-or-self axis and every
    child step.
    """
    return [
        child
        for child in (getattr(node, "_children_", None) or [])
        if not getattr(child, "_skipped_", False)
    ]


def _nameOf(node: Any) -> str:
    """Returns the element name of a bound child instance, if known."""
    name = getattr(node, "_name_", None)
    if name is not None:
        return name
    descriptor = getattr(node, "_descriptor_", None)
    if descriptor is not None:
        return descriptor.name
    return node.__class__.__name__ if not isinstance(node, XsdDataType) else "?"


def _attributeValue(node: Any, attributeName: str) -> Any | None:
    """Returns the bound value of one attribute, or ``None``.

    The typed value stored on the instance is preferred over the raw
    lexical form, so equality is evaluated in the XSD value space.
    """
    value = node.__dict__.get(attributeName)
    if value is not None:
        return value
    attribs = getattr(node, "_attribs_", None)
    if attribs:
        value = attribs.get(attributeName)
        if value is not None:
            return value
    return None


def _nodeValue(node: Any) -> Any | None:
    """Returns the simple-content value of a bound node, or ``None``.

    Nilled elements carry no value and yield ``None``.
    """
    if getattr(node, "_nil_", False):
        return None
    if isinstance(node, XsdDataType):
        return node
    if isinstance(node, SchemaBase):
        value = getattr(node, "_value_", None)
        if isinstance(value, list):
            return value[0] if value else None
    return None


def _formatValues(values: tuple[Any, ...]) -> str:
    """Formats a field-value tuple for report messages."""
    if len(values) == 1:
        return repr(values[0])
    return "(" + ", ".join(repr(value) for value in values) + ")"
