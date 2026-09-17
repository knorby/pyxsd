"""Instance-side checking of XSD identity constraints.

After the instance document is bound, every schema element
declaration that carries ``xs:key``, ``xs:unique`` or ``xs:keyref``
constraints is checked against the bound instance tree. Each bound
instance records the element representative it was built from in
``_descriptor_``, which is what connects the schema-side constraints
to the instance-side nodes.

The supported XPath subset covers the XSD 1.1 selector/field grammar
(see :mod:`pyxsd.xpath_subset` for the exact admission rules):

- child steps separated by ``/`` (``item``, ``order/line``),
- ``.`` for the context node (``./item`` is the same as ``item``),
- ``*`` and ``prefix:*`` wildcard child steps, with namespace prefixes
  resolved through the declaration site's bindings and the XPath
  default namespace,
- ``.//`` for descendant-or-self (``.//item``), leading
  only,
- full ``child::``/``attribute::`` axis steps and abbreviated
  ``@attribute`` steps (an attribute step ends the path),
- top-level unions (``a | @b``).

Paths outside the subset are rejected at schema phase with an
``xpath-invalid`` error by the element representatives; the evaluation
here only ever sees validated :class:`~pyxsd.xpath_subset.ParsedXPath`
paths (or raw strings from test stand-ins, which go through the same
parser and degrade to an ``identity-unsupported`` warning).

Identity constraints are scoped to the element occurrence that owns
them: a repeating element with a key declaration gets an independent
value table per occurrence, and a keyref resolves against the nearest
enclosing scope that defines the referenced constraint. Values are
compared in the XSD value space (see ``xsd_value_key``), so
lexically different spellings of one value match.
"""

import logging
from typing import Any

from pyxsd.namespaces import local_name
from pyxsd.schema_base import SchemaBase
from pyxsd.validation import ValidationReport
from pyxsd.xpath_subset import ParsedXPath, XPathError, parse_xpath_subset
from pyxsd.xsd_data_types import XsdDataType, xsd_comparable_key

logger = logging.getLogger(__name__)

_MISSING: Any = object()
_UNSUPPORTED: Any = object()
_AMBIGUOUS: Any = object()

#: Marks constraint objects without a schema-phase parsed path (plain
#: path-string stand-ins, as used by tests): those take the legacy
#: string-parsing route at evaluation time.
_ABSENT: Any = object()

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


def _selectNodes(node: Any, constraint: Any, report: ValidationReport) -> list[Any] | None:
    """Returns the nodes a constraint's selector covers, or ``None``.

    ``None`` means the selector could not be evaluated; the reason is
    already on the report (a schema-phase ``xpath-invalid`` error, or an
    ``identity-unsupported`` warning for string-only constraint
    stand-ins).
    """
    selector = constraint.selector
    if not selector:
        report.add_warning(
            f"identity constraint '{constraint.constraintName}' has no selector; "
            "it will not be checked",
            code="identity-unsupported",
        )
        return None
    parsed = getattr(constraint, "parsedSelectorPath", _ABSENT)
    if parsed is _ABSENT:
        parsed = _legacyParsePath(selector, constraint, report, "selector")
        if parsed is None:
            return None
    elif parsed is None:
        return None
    return [
        match
        for descendant, steps in parsed.alternatives
        for match in _evalAlternative(node, descendant, steps)
    ]


def _fieldValues(
    selectedNode: Any,
    constraint: Any,
    report: ValidationReport,
) -> Any:
    """Returns the XSD value key for one selected node's fields.

    Returns ``_MISSING`` (no value), ``_AMBIGUOUS`` (a field selects
    more than one value; reported as an error) or ``_UNSUPPORTED``
    (path not supported; the reason is already on the report).
    """
    fieldPaths = constraint.fieldPaths
    parsedFields = getattr(constraint, "parsedFieldPaths", _ABSENT)
    values: list[Any] = []
    for index, fieldPath in enumerate(fieldPaths):
        if parsedFields is _ABSENT:
            parsed = _legacyParsePath(fieldPath, constraint, report, "field")
        else:
            parsed = parsedFields[index] if index < len(parsedFields) else None
        if parsed is None:
            return _UNSUPPORTED
        result = _evalField(selectedNode, parsed)
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


def _legacyParsePath(
    path: str,
    constraint: Any,
    report: ValidationReport,
    kind: str,
) -> ParsedXPath | None:
    """Parses a raw path string for a constraint without a schema-phase
    parse, warning ``identity-unsupported`` when it is outside the
    subset.

    Constraint stand-ins (tests) carry plain strings; the historical
    behavior — a warning and a skipped constraint — is preserved.
    """
    if "[" in path:
        report.add_warning(
            f"the {kind} '{path}' of identity constraint "
            f"'{constraint.constraintName}' uses a predicate, which pyxsd does "
            "not support; the constraint will not be checked",
            code="identity-unsupported",
        )
        return None
    try:
        return parse_xpath_subset(path, {}, None, None)
    except XPathError:
        report.add_warning(
            f"the {kind} '{path}' of identity constraint "
            f"'{constraint.constraintName}' is not a supported path; the "
            "constraint will not be checked",
            code="identity-unsupported",
        )
        return None


def _evalAlternative(node: Any, descendant: bool, steps: tuple[tuple[str, ...], ...]) -> list[Any]:
    """Evaluates one parsed path alternative against a bound node."""
    view = _stepView(steps)
    if descendant:
        return [
            match
            for candidate in _descendantOrSelfNodes(node)
            for match in _evalSteps(candidate, view)
        ]
    return _evalSteps(node, view)


def _stepView(steps: tuple[tuple[str, ...], ...]) -> list[str]:
    """Translates parsed steps into the evaluator's path view.

    Element steps keep their Clark name — child matching tries the
    exact name first and falls back to the local name — while a
    namespace wildcard collapses to ``*`` and an attribute step becomes
    ``@name`` (bound attribute values are keyed by local name).
    """
    view: list[str] = []
    for step in steps:
        kind = step[0]
        name = step[1] if len(step) > 1 else ""
        if kind == "self":
            continue
        if kind == "attribute":
            view.append(f"@{name if name == '*' else local_name(name)}")
        elif name == "*" or name.endswith("}*"):
            view.append("*")
        else:
            view.append(name)
    return view


def _evalField(selectedNode: Any, parsed: ParsedXPath) -> list[Any]:
    """Returns the values one parsed field selects on one node.

    A list of zero or more values is returned. Union alternatives
    concatenate; fields that select more than one value are diagnosed
    by the caller via the list length.
    """
    values: list[Any] = []
    for descendant, steps in parsed.alternatives:
        view = _stepView(steps)
        if view and view[-1].startswith("@"):
            attributeName = view[-1][1:]
            if len(view) > 1:
                nodes = (
                    [
                        match
                        for candidate in _descendantOrSelfNodes(selectedNode)
                        for match in _evalSteps(candidate, view[:-1])
                    ]
                    if descendant
                    else _evalSteps(selectedNode, view[:-1])
                )
            else:
                nodes = list(_descendantOrSelfNodes(selectedNode)) if descendant else [selectedNode]
            values += [
                value
                for node in nodes
                for value in _attributeValues(node, attributeName)
                if value is not None
            ]
        else:
            # A field ending in ``.`` reduced to the element steps
            # before it (or nothing at all, meaning the selected node
            # itself), so the remaining case is a field naming an
            # element: the value is that element's simple content.
            nodes = _evalAlternative(selectedNode, descendant, steps)
            values += [value for node in nodes for value in [_nodeValue(node)] if value is not None]
    return values


def _evalSteps(node: Any, steps: list[str]) -> list[Any]:
    """Walks child steps from ``node`` and returns the matching nodes.

    A Clark-named step matches the exact expanded name first and falls
    back to the local name, preserving the historical namespace-
    insensitive matching of bound children.
    """
    nodes = [node]
    for step in steps:
        stepLocal = local_name(step)
        nextNodes = []
        for current in nodes:
            for child in _childrenOf(current):
                childName = _nameOf(child)
                localName = childName.split("}", 1)[-1] if childName.startswith("{") else childName
                if step == "*" or childName == step or localName == stepLocal:
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


def _attributeValues(node: Any, attributeName: str) -> tuple[Any, ...]:
    """Returns the values an attribute step selects on one node.

    A named step selects that one attribute; the ``*`` wildcard selects
    every attribute the node carries.
    """
    if attributeName != "*":
        value = _attributeValue(node, attributeName)
        return (value,) if value is not None else ()
    attribs = getattr(node, "_attribs_", None) or {}
    return tuple(attribs.values())


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
