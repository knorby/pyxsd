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
from typing import Any, NamedTuple

from pyxsd.namespaces import local_name
from pyxsd.schema_base import SchemaBase
from pyxsd.validation import ValidationReport
from pyxsd.xpath_subset import ParsedXPath, XPathError, parse_xpath_subset
from pyxsd.xsd_data_types import (
    AnyType,
    Base64Binary,
    Boolean,
    Date,
    DateTime,
    Duration,
    GDay,
    GMonth,
    GMonthDay,
    GYear,
    GYearMonth,
    HexBinary,
    QName,
    Time,
    XsdDataType,
    xsd_comparable_key,
)
from pyxsd.xsd_data_types import (
    Decimal as XsdDecimal,
)

logger = logging.getLogger(__name__)

_MISSING: Any = object()
_UNSUPPORTED: Any = object()
_AMBIGUOUS: Any = object()
#: Marks a field whose single selected element has complex content (or
#: is governed by ``xs:anyType``): such an element has no value and the
#: constraint is violated (XSD 1.1 §3.13.4 clause 3, idK012/idZ010).
_COMPLEX: Any = object()

#: Marks constraint objects without a schema-phase parsed path (plain
#: path-string stand-ins, as used by tests): those take the legacy
#: string-parsing route at evaluation time.
_ABSENT: Any = object()

KeyScopes = tuple[dict[str, set[tuple[Any, ...]]], ...]


class _Scope:
    """One scope occurrence's contribution to the scope tree.

    ``tables`` holds the occurrence's key/unique value tables keyed by
    table name, ``keyrefs`` the keyref constraints to validate after
    the walk, and ``parent`` links the enclosing scope occurrence.
    Occurrences without tables or keyrefs are skipped: they contribute
    nothing to either side of the deferred pass.
    """

    __slots__ = ("children", "keyrefs", "parent", "tables")

    def __init__(self, parent: "_Scope | None"):
        self.parent = parent
        self.tables: dict[str, set[tuple[Any, ...]]] = {}
        self.keyrefs: list[tuple[Any, Any]] = []
        self.children: list[_Scope] = []


def check_identity_constraints(rootInstance: Any, report: ValidationReport) -> None:
    """Checks every identity constraint reachable from the root instance.

    - ``rootInstance``: the bound root instance (or ``None``).
    - ``report``: the :class:`~pyxsd.validation.ValidationReport` that
      collects the findings.

    The walk records each occurrence's key/unique tables into a scope
    tree and attaches keyref occurrences to their enclosing scope;
    once the whole document has been walked, every keyref is validated
    against the node tables the referenced constraint accumulated
    within the keyref's own subtree. An element's identity-constraint
    table assembles each eligible constraint's table from the
    element's own occurrence and its descendants (XSD §3.11.5
    upward propagation), so a keyref never draws on a table assembled
    outside its subtree (XSD §3.11.4 keyref clause).
    """
    if rootInstance is None:
        return None
    root = _Scope(None)
    _walk(rootInstance, root, report)
    _validateKeyrefs(root, report)
    return None


def _walk(instance: Any, scope: _Scope, report: ValidationReport) -> None:
    """Records the constraints of ``instance`` and recurses downward.

    A subtree bound by a ``processContents="skip"`` wildcard is skipped
    (XSD 1.1 §3.3.4.2): the walk neither applies the (absent)
    declaration's constraints to it nor lets its descendants serve as
    key/unique/keyref selections. ``_childrenOf`` hides skipped
    subtrees, so this guard only fires when the walk starts inside one.

    Key/unique tables are collected per occurrence; keyrefs are only
    recorded here — ``_validateKeyrefs`` checks them after the walk,
    so a keyref sees the complete tables assembled within its own
    subtree, wherever the walk recorded them.
    """
    if getattr(instance, "_skipped_", False):
        return None
    descriptor = getattr(instance, "_descriptor_", None)
    identities = list(getattr(descriptor, "identities", []) or []) if descriptor is not None else []

    localKeys: dict[str, set[tuple[Any, ...]]] = {}
    keyrefs: list[Any] = []
    for constraint in identities:
        if getattr(constraint, "isConstraintRef", False):
            # An XSD 1.1 constraint reference site acts as the named
            # constraint it resolves to; an unresolvable site was
            # already reported at schema phase and is skipped.
            constraint = getattr(constraint, "borrowedFrom", None)
            if constraint is None:
                continue
        kind = constraint.__class__.__name__
        if kind in ("Key", "Unique"):
            values = _collectKeyValues(instance, constraint, kind, report)
            if values is not None:
                localKeys[_tableKey(constraint)] = values
        elif kind == "Keyref":
            keyrefs.append(constraint)

    if localKeys or keyrefs:
        childScope = _Scope(scope)
        childScope.tables = localKeys
        scope.children.append(childScope)
        scope = childScope
    for constraint in keyrefs:
        scope.keyrefs.append((constraint, instance))

    for child in getattr(instance, "_children_", None) or []:
        _walk(child, scope, report)
    return None


def _validateKeyrefs(scope: _Scope, report: ValidationReport) -> dict[str, set[tuple[Any, ...]]]:
    """Validates every recorded keyref against its subtree's tables.

    Post-order over the scope tree: each node merges its own tables
    with its descendants' merged tables — node tables assemble
    strictly upward from the children (XSD §3.11.5 upward
    propagation) — and then checks its keyrefs against the merge. A
    keyref therefore resolves only against the referenced
    constraint's tables from its own occurrence and descendant
    occurrences: never an ancestor's table, and never the whole
    document (XSD §3.11.4 keyref clause and its subtree note). A
    scope occurrence whose table is empty still counts as present:
    the constraint exists there, so its (empty) table simply matches
    no keyref member.
    """
    merged = dict(scope.tables)
    for child in scope.children:
        for key, values in _validateKeyrefs(child, report).items():
            merged.setdefault(key, set()).update(values)
    for constraint, node in scope.keyrefs:
        _checkKeyref(constraint, node, (merged,), report)
    return merged


def _tableKey(constraint: Any) -> str:
    """Returns the scope-table key for a key/unique constraint.

    The schema phase records each constraint's Clark name
    (``{targetNamespace}name``) on ``constraintClark``; constraint
    stand-ins without one fall back to the bare constraint name.
    """
    return getattr(constraint, "constraintClark", None) or constraint.constraintName


def _referTableKey(constraint: Any) -> str:
    """Returns the scope-table key a keyref's resolved ``refer`` names.

    ``referClark`` carries the schema-phase Clark resolution of the
    ``refer`` QName; stand-ins without one fall back to the refer's
    local name.
    """
    referClark = getattr(constraint, "referClark", None)
    if referClark:
        return referClark
    return constraint.refer.split(":")[-1]


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
        if resolved is _COMPLEX:
            report.add_error(
                f"{kind.lower()} '{constraint.constraintName}': a field selects an "
                f"element with complex content on the '{_nameOf(selectedNode)}' element",
                code="identity-key",
                element=_nameOf(node),
            )
            continue
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
    """Checks one keyref constraint's records against its key scope.

    ``scopes`` is the chain of per-occurrence tables the keyref may
    draw from; the deferred pass supplies a single table merged from
    every qualifying scope occurrence.
    """
    referKey = _referTableKey(constraint)
    known = None
    for scope in reversed(scopes):
        if referKey in scope:
            known = scope[referKey]
            break
    if known is None:
        report.add_error(
            f"keyref '{constraint.constraintName}' refers to '{constraint.refer}', "
            "but no key or unique with that name was found in scope",
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
        if resolved in (_MISSING, _AMBIGUOUS, _COMPLEX):
            # A keyref with a missing, ambiguous or complex-content
            # field is simply absent.
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
    # Union alternatives concatenate into one selector node set.
    matches: list[Any] = []
    seen: set[int] = set()
    for descendant, steps in parsed.alternatives:
        candidates: list[Any]
        if descendant:
            candidates = [
                match
                for context in _descendantOrSelfNodes(node)
                for match in _evalSteps(context, steps)
            ]
        else:
            candidates = _evalSteps(node, steps)
        for match in candidates:
            if id(match) not in seen:
                seen.add(id(match))
                matches.append(match)
    return matches


def _fieldValues(
    selectedNode: Any,
    constraint: Any,
    report: ValidationReport,
) -> Any:
    """Returns the value key tuple for one selected node's fields.

    Returns ``_MISSING`` (a field selects no value), ``_AMBIGUOUS`` (a
    field selects more than one node; reported as an error),
    ``_COMPLEX`` (a field selects an element with complex content;
    reported as an error) or ``_UNSUPPORTED`` (path not supported; the
    reason is already on the report).

    Cardinality is counted before any value is discarded: a nilled
    element selected alongside a valued attribute is two field nodes,
    not one (XSD 1.1 §3.13.4 clause 3).
    """
    fieldPaths = constraint.fieldPaths
    parsedFields = getattr(constraint, "parsedFieldPaths", _ABSENT)
    keys: list[Any] = []
    for index, fieldPath in enumerate(fieldPaths):
        if parsedFields is _ABSENT:
            parsed = _legacyParsePath(fieldPath, constraint, report, "field")
        else:
            parsed = parsedFields[index] if index < len(parsedFields) else None
        if parsed is None:
            return _UNSUPPORTED
        nodes = _fieldNodes(selectedNode, parsed)
        if len(nodes) > 1:
            report.add_error(
                f"field '{fieldPath}' of identity constraint "
                f"'{constraint.constraintName}' selects more than one value on "
                f"the '{_nameOf(selectedNode)}' element",
                code="identity-key",
                element=_nameOf(selectedNode),
            )
            return _AMBIGUOUS
        if not nodes:
            return _MISSING
        node = nodes[0]
        if isinstance(node, _AttributeField):
            keys.append(_valueSpaceKey(node.value))
            continue
        if _isComplexContent(node):
            return _COMPLEX
        value = _nodeValue(node)
        if value is None:
            # A nilled (or value-less) selected element counts for
            # cardinality but supplies no value.
            return _MISSING
        keys.append(_valueSpaceKey(value))
    return tuple(keys)


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


class _AttributeField(NamedTuple):
    """One attribute node selected by a field step."""

    value: Any


def _fieldNodes(selectedNode: Any, parsed: ParsedXPath) -> list[Any]:
    """Evaluates one parsed field's node set on a selected node.

    Union alternatives concatenate into one node set (deduplicated);
    a final attribute step contributes the attributes it selects.
    Element nodes are returned whole — nil and complex content are
    judged by the caller so cardinality is counted before any
    value-discard (XSD 1.1 §3.13.4 clause 3).
    """
    nodes: list[Any] = []
    seenNodes: set[int | tuple[int, str]] = set()
    for descendant, steps in parsed.alternatives:
        attributeStep: str | None = None
        elementSteps = steps
        if steps and steps[-1][0] == "attribute":
            attributeStep = steps[-1][1]
            elementSteps = steps[:-1]
        if descendant:
            matches = [
                match
                for candidate in _descendantOrSelfNodes(selectedNode)
                for match in _evalSteps(candidate, elementSteps)
            ]
        else:
            matches = _evalSteps(selectedNode, elementSteps)
        for match in matches:
            if attributeStep is None:
                if id(match) not in seenNodes:
                    seenNodes.add(id(match))
                    nodes.append(match)
                continue
            for name, value in _attributeSelections(match, attributeStep):
                # (node, attribute-name) pairs deduplicate independently
                marker = (id(match), name)
                if marker not in seenNodes:
                    seenNodes.add(marker)
                    nodes.append(_AttributeField(value))
    return nodes


def _evalSteps(node: Any, steps: tuple[tuple[str, ...], ...]) -> list[Any]:
    """Walks element steps from ``node`` and returns the matching nodes.

    A step matches a child by expanded name: an exact Clark-name match,
    the ``*`` wildcard for any element, or a ``{uri}*`` namespace
    wildcard. Namespace-insensitive local-name matching is gone — an
    unprefixed step only ever names the no-namespace element.
    """
    nodes = [node]
    for step in steps:
        if step[0] == "self":
            continue
        name = step[1] if len(step) > 1 else "*"
        nextNodes = []
        for current in nodes:
            for child in _childrenOf(current):
                if _elementStepMatches(name, _nameOf(child)):
                    nextNodes.append(child)
        nodes = nextNodes
    return nodes


def _elementStepMatches(stepName: str, nodeName: str) -> bool:
    """Whether one parsed element step matches one node name."""
    if stepName == "*":
        return True
    if stepName.endswith("}*"):
        return nodeName.startswith(stepName[:-1])
    return stepName == nodeName


def _attributeSelections(node: Any, stepName: str) -> list[tuple[str, Any]]:
    """Returns the (name, value) pairs one attribute step selects.

    ``@*`` selects every attribute the node carries; ``@prefix:*`` is
    translated to Clark form and selects only that namespace's
    attributes; a named step selects that one attribute.
    """
    if stepName == "*":
        return list(_visibleAttributes(node).items())
    if stepName.endswith("}*"):
        prefix = stepName[:-1]
        return [
            (name, value)
            for name, value in _visibleAttributes(node).items()
            if name.startswith(prefix)
        ]
    value = _attributeValue(node, stepName)
    return [(stepName, value)] if value is not None else []


def _visibleAttributes(node: Any) -> dict[str, Any]:
    """The node's raw attribute table, minus skip-wildcard attributes.

    Attributes absorbed by a ``processContents="skip"`` wildcard are
    not validated and are not part of any field's node set (XSD 1.1
    §3.3.4.2; idZ015).
    """
    attribs = getattr(node, "_attribs_", None) or {}
    skipped = getattr(node, "_wildcardSkipAttributes_", None)
    if skipped:
        return {name: value for name, value in attribs.items() if name not in skipped}
    return attribs


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
    """Returns the value of one named attribute, or ``None``.

    R6: presence is decided by the raw attribute table first, so an
    attribute wins over any same-named child accessor; the lookup is
    namespace-exact (Clark key for a qualified attribute, bare local
    name for an unqualified one). The descriptor-bound typed value is
    preferred once presence is established, so equality is evaluated in
    the XSD value space; an attribute absent from the raw table is
    still found when its declaration supplied a ``default``/``fixed``
    value through the descriptor (such values never enter
    ``_attribs_``).
    """
    attribs = _visibleAttributes(node)
    typed = _typedAttributeValue(node, attributeName)
    if attributeName in attribs:
        return typed if typed is not None else attribs[attributeName]
    return typed


def _typedAttributeValue(node: Any, attributeName: str) -> Any | None:
    """Returns the descriptor-bound typed value of one attribute, or ``None``.

    Only a declaration whose instance name is exactly ``attributeName``
    (bare or Clark, honoring form defaults in strict namespace mode)
    can supply the value, so a bare step never reaches a qualified
    attribute's slot.
    """
    if not isinstance(node, SchemaBase):
        return None
    local = local_name(attributeName)
    descriptor = node.descAttributes().get(local)
    if descriptor is None:
        return None
    try:
        instanceName = type(node)._instance_name_of(descriptor, is_attribute=True)
    except Exception:
        return None
    if instanceName != attributeName:
        return None
    return node.__dict__.get(local)


def _isComplexContent(node: Any) -> bool:
    """Whether a field-selected element has complex content.

    An element with element children, or one governed by a complex
    type definition (including ``xs:anyType``), has no [schema actual
    value] and cannot supply a field value (XSD 1.1 §3.13.4 clause 3).
    An element governed by ``xs:anyType`` binds as ``SchemaBase`` exact
    (the ur-type has no generated class): that stand-in is complex
    content even when the instance carries only character data
    (idZ010).
    """
    if type(node) is SchemaBase:
        return True
    if isinstance(node, AnyType):
        return True
    if isinstance(node, XsdDataType):
        return False
    if isinstance(node, SchemaBase):
        return not isinstance(getattr(node, "_value_", None), list)
    return False


def _valueSpaceKey(value: Any) -> tuple[str, Any]:
    """The comparison key for one field value.

    Pairs a value-space tag with :func:`xsd_comparable_key`: two values
    compare equal only within one value space. ``3.0`` and ``3`` are
    conflicting when both are decimal but non-conflicting when one is a
    string and one a decimal (XSD 1.1 §3.13.4; idF012-014,
    fields00202m3). Within a space the comparable key decides, so
    ``xs:int`` ``1``/``01`` still collide and the string-derived types
    (``xs:ID`` vs ``xs:string``) still share one space.
    """
    key = xsd_comparable_key(value)
    # Order matters: the str-family arm is last because several typed
    # values (dates, binaries, lists) are str subclasses.
    if isinstance(value, (bool, Boolean)):
        return ("boolean", key)
    if isinstance(value, Duration):
        return ("duration", key)
    if isinstance(value, XsdDecimal):
        return ("decimal", key)
    if isinstance(value, int):
        # The integer family shares xs:decimal's value space.
        return ("decimal", key)
    if isinstance(value, float):
        return ("float", key)
    if isinstance(value, (DateTime, Date, Time, GYear, GYearMonth, GMonthDay, GMonth, GDay)):
        return (type(value).name, key)
    if isinstance(value, HexBinary):
        return ("hexBinary", key)
    if isinstance(value, Base64Binary):
        return ("base64Binary", key)
    if isinstance(value, QName):
        return ("QName", key)
    if isinstance(value, str):
        return ("string", key)
    return ("", key)


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
