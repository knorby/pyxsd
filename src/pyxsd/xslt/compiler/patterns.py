"""Compilation and candidate-aware matching for XSLT 3.0 patterns.

The Pattern30 grammar — scanning, parsing, and the immutable parse tree — lives
in :mod:`pyxsd.xslt.compiler.pattern_syntax`.  This module owns the compiled
pattern representation: it turns parsed alternatives into normative default
priorities, candidate filters, and forward-plan requirements, then matches with
two plans:

* an optimized right-to-left plan for selection patterns without parenthesized
  postfix steps.  It applies cheap kind/name/type filters before walking
  parents and ancestors, and uses the candidate's own sibling focus for
  positional predicates; and
* a formal ``root(.)//(EE)``-equivalent forward plan for complex patterns.
  It is memoized per tree and environment, so an accidental whole-tree scan is
  visible in :class:`PatternStatistics` rather than hidden behind elapsed time.

Both plans share one predicate evaluator, rooted-function implementation, and
schema-test implementation, and every dynamic error raised while matching one
item is treated as a non-match as required by XSLT 3.0 section 5.5.4.  An
execution-budget alarm is re-raised instead: converting it would cancel the
caller's bound on the work.

A bare ``map(*)`` is a sequence type and is rejected with ``XTSE0340``; the
generalized item pattern is ``.[. instance of map(*)]``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, cast
from urllib.parse import urljoin

from pyxsd.locations import IssueLocation
from pyxsd.namespaces import local_name as _local_name
from pyxsd.namespaces import namespace_of as _namespace_of
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xdm.values import XdmAtomic, XdmItem, XdmSequence, as_xdm
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus
from pyxsd.xslt.compiler.context import StaticContext
from pyxsd.xslt.compiler.expressions import ItemType
from pyxsd.xslt.compiler.pattern_syntax import (
    _CURRENT_VARIABLE,
    _NODE_KINDS,
    _TYPE_KIND_KINDS,
    AbsoluteOnly,
    AbsolutePath,
    AxisStep,
    CompiledPredicate,
    DescendantPath,
    FunctionArgument,
    IntersectExceptExpr,
    NameTest,
    NodeTest,
    ParsedAlternative,
    PathExpr,
    PostfixStep,
    RelativePath,
    RootedBase,
    RootedPath,
    StepExpr,
    UnionExpr,
    VariableBase,
    _principal_kind,
    _static_error,
    parse_pattern,
)
from pyxsd.xslt.errors import is_budget_alarm

XSD_NS = "http://www.w3.org/2001/XMLSchema"

_ELEMENT_AXIS_KINDS = frozenset(
    {NodeKind.ELEMENT, NodeKind.TEXT, NodeKind.COMMENT, NodeKind.PROCESSING_INSTRUCTION}
)
_DESCENDANT_OR_SELF_KINDS = frozenset(
    {
        NodeKind.DOCUMENT,
        NodeKind.ELEMENT,
        NodeKind.TEXT,
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
    }
)

_NUMERIC_LOCALS = frozenset(
    {
        "decimal",
        "double",
        "float",
        "byte",
        "int",
        "integer",
        "long",
        "negativeInteger",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "short",
        "unsignedByte",
        "unsignedInt",
        "unsignedLong",
        "unsignedShort",
    }
)
_STRING_LOCALS = frozenset(
    {
        "anyURI",
        "ENTITIES",
        "ENTITY",
        "ID",
        "IDREF",
        "IDREFS",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "NMTOKENS",
        "normalizedString",
        "string",
        "token",
        "untypedAtomic",
    }
)

__all__ = [
    "CompiledPattern",
    "PatternAlternative",
    "PatternMatcher",
    "PatternStatistics",
    "compile_pattern",
    "default_priority",
]


# ---------------------------------------------------------------------------
# Compiled pattern representation


@dataclass(frozen=True, slots=True)
class CandidateShape:
    """A cheap kind/name filter for one possible selected node."""

    kinds: frozenset[NodeKind]
    name: str | None = None
    local: str | None = None
    namespace: str | None = None
    any_namespace: bool = True
    target: str | None = None

    def matches(self, item: XdmItem) -> bool:
        if not isinstance(item, XdmNode):
            return False
        if item.kind not in self.kinds:
            return False
        if self.name is not None and item.name != self.name:
            return False
        if self.local is not None and item.local_name != self.local:
            return False
        if not self.any_namespace and (item.namespace_uri or "") != (self.namespace or ""):
            return False
        return self.target is None or item.target == self.target


@dataclass(frozen=True, slots=True)
class CandidateFilter:
    """Candidate shapes for one selection alternative."""

    shapes: tuple[CandidateShape, ...] = ()
    all_items: bool = False

    def matches(self, item: XdmItem) -> bool:
        if self.all_items:
            return True
        return any(shape.matches(item) for shape in self.shapes)


@dataclass(frozen=True, slots=True)
class PatternAlternative:
    """One normative union alternative with its default priority."""

    index: int
    priority: Decimal
    predicate: CompiledExpression | None = None
    path: IntersectExceptExpr | None = None
    filter: CandidateFilter = field(default_factory=CandidateFilter)
    requires_forward: bool = False

    def __post_init__(self) -> None:
        if (self.predicate is None) == (self.path is None):
            raise ValueError("a pattern alternative is either a predicate or a selection")


@dataclass(frozen=True, slots=True)
class CompiledPattern:
    """One immutable compiled Pattern30 with normative per-alternative priorities."""

    source: str
    context: StaticContext
    alternatives: tuple[PatternAlternative, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("a pattern requires non-empty source")
        if not isinstance(self.context, StaticContext):
            raise TypeError("context must be a StaticContext")
        if not self.alternatives or not all(
            isinstance(alternative, PatternAlternative) for alternative in self.alternatives
        ):
            raise TypeError("alternatives must be a non-empty tuple of PatternAlternative")

    @property
    def default_priorities(self) -> tuple[Decimal, ...]:
        return tuple(alternative.priority for alternative in self.alternatives)


# ---------------------------------------------------------------------------
# Candidate filters and dispatch plans


def _candidate_filter(expr: IntersectExceptExpr) -> CandidateFilter:
    return CandidateFilter(_shapes_for_path(expr.first))


def _shapes_for_path(path: PathExpr) -> tuple[CandidateShape, ...]:
    if isinstance(path, RelativePath):
        return _shapes_for_step(path.steps[-1][1], top=len(path.steps) == 1)
    if isinstance(path, AbsoluteOnly):
        return (CandidateShape(frozenset({NodeKind.DOCUMENT})),)
    if isinstance(path, (AbsolutePath, DescendantPath)):
        return _shapes_for_step(path.relative.steps[-1][1], top=False)
    return (CandidateShape(_NODE_KINDS),)


def _shapes_for_step(step: StepExpr, *, top: bool) -> tuple[CandidateShape, ...]:
    if isinstance(step, PostfixStep):
        shapes: list[CandidateShape] = []
        for alternative in step.union.alternatives:
            shapes.extend(_shapes_for_path(alternative.first))
        return tuple(shapes)
    axis = step.top_axis if top else step.axis
    possible = _AXIS_KINDS[axis]
    node_test = step.node_test
    if isinstance(node_test, NameTest):
        shapes = [
            CandidateShape(
                frozenset({_principal_kind_node(axis)}),
                name=node_test.name,
                local=node_test.local,
                namespace=node_test.namespace,
                any_namespace=node_test.any_namespace,
            )
        ]
        return tuple(shape for shape in shapes if shape.kinds <= possible)
    kinds = _TYPE_KIND_KINDS[node_test.kind] & possible
    if not kinds:
        return ()
    name: str | None = None
    target: str | None = None
    if node_test.kind in {"element", "attribute"}:
        item_type = node_test.item_type
        if item_type.name is not None and not item_type.wildcard:
            name = item_type.name
    if node_test.kind == "processing-instruction":
        target = node_test.item_type.target
    return (CandidateShape(frozenset(kinds), name=name, target=target),)


def _principal_kind_node(axis: str) -> NodeKind:
    principal = _principal_kind(axis)
    return {
        "element": NodeKind.ELEMENT,
        "attribute": NodeKind.ATTRIBUTE,
        "namespace": NodeKind.NAMESPACE,
    }[principal]


_AXIS_KINDS: dict[str, frozenset[NodeKind]] = {
    "child": _ELEMENT_AXIS_KINDS,
    "child-or-top": _ELEMENT_AXIS_KINDS,
    "attribute": frozenset({NodeKind.ATTRIBUTE}),
    "attribute-or-top": frozenset({NodeKind.ATTRIBUTE}),
    "self": _NODE_KINDS,
    "descendant": _ELEMENT_AXIS_KINDS,
    "descendant-or-self": _DESCENDANT_OR_SELF_KINDS,
    "namespace": frozenset({NodeKind.NAMESPACE}),
    "namespace-or-top": frozenset({NodeKind.NAMESPACE}),
}


def _requires_forward(expr: IntersectExceptExpr) -> bool:
    """Whether only the formal whole-tree plan is safely equivalent.

    ``intersect`` and ``except`` must evaluate both operands in the same
    ``root(.)//EE`` context, so they use the formal plan rather than the
    right-to-left parent/ancestor optimization.  Parenthesized postfix steps
    likewise use the formal plan.
    """
    if expr.rest:
        return True
    return _path_contains_postfix(expr.first)


def _path_contains_postfix(path: PathExpr) -> bool:
    if isinstance(path, (AbsolutePath, DescendantPath)):
        return _relative_contains_postfix(path.relative)
    if isinstance(path, RelativePath):
        return _relative_contains_postfix(path)
    if isinstance(path, RootedPath):
        return path.following is not None and _relative_contains_postfix(path.following)
    return False


def _relative_contains_postfix(path: RelativePath) -> bool:
    for _, step in path.steps:
        if isinstance(step, PostfixStep):
            return True
        if isinstance(step, AxisStep) and step.predicates:
            # Predicates are XPath, not patterns, but their grammar is complete
            # and does not contain pattern postfix steps.
            continue
    return False


# ---------------------------------------------------------------------------
# Matching statistics and public matcher


@dataclass(slots=True)
class PatternStatistics:
    """Operation counters for one matcher, index, or diagnostic run."""

    candidate_filters: int = 0
    candidate_tests: int = 0
    ancestor_steps: int = 0
    predicate_evaluations: int = 0
    equivalent_expression_evaluations: int = 0
    function_evaluations: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_invalidations: int = 0
    errors_ignored: int = 0


class PatternMatcher:
    """Match one compiled pattern with an optimized or formal plan."""

    def __init__(
        self,
        pattern: CompiledPattern,
        *,
        statistics: PatternStatistics | None = None,
        strategy: Literal["optimized", "forward"] = "optimized",
    ) -> None:
        if not isinstance(pattern, CompiledPattern):
            raise TypeError("pattern must be a CompiledPattern")
        if strategy not in ("optimized", "forward"):
            raise ValueError(f"unknown pattern matching strategy {strategy!r}")
        self.pattern = pattern
        self.strategy = strategy
        self.statistics = statistics or PatternStatistics()
        self._environment: ExpressionEnvironment | None = None
        self._matches: dict[tuple[int, object], bool] = {}

    def matches(self, item: XdmItem, environment: ExpressionEnvironment) -> bool:
        """Whether any alternative of the pattern matches *item*."""
        if not isinstance(environment, ExpressionEnvironment):
            raise TypeError("environment must be an ExpressionEnvironment")
        self._select_environment(environment)
        return any(
            self.matches_alternative(alternative.index, item, environment)
            for alternative in self.pattern.alternatives
        )

    def matches_alternative(
        self, index: int, item: XdmItem, environment: ExpressionEnvironment
    ) -> bool:
        """Match one normative union alternative by index."""
        if not isinstance(environment, ExpressionEnvironment):
            raise TypeError("environment must be an ExpressionEnvironment")
        self._select_environment(environment)
        alternative = self.pattern.alternatives[index]
        key = (index, _pattern_cache_key(item))
        cached = self._matches.get(key)
        if cached is not None:
            self.statistics.cache_hits += 1
            return cached
        self.statistics.cache_misses += 1
        try:
            result = self._evaluate_alternative(alternative, item, environment)
        except Exception as exc:
            # XSLT 3.0 section 5.5.4 makes a dynamic error raised while
            # matching one item a non-match, but an execution-budget alarm is
            # not a pattern failure: converting it would cancel the caller's
            # bound on the work and let the match continue unbounded.
            if is_budget_alarm(exc):
                raise
            self.statistics.errors_ignored += 1
            result = False
        self._matches[key] = result
        return result

    def _evaluate_alternative(
        self,
        alternative: PatternAlternative,
        item: XdmItem,
        environment: ExpressionEnvironment,
    ) -> bool:
        if alternative.predicate is not None:
            result = _evaluate(alternative.predicate, item, environment, Focus.singleton(item))
            return bool(result)
        if not isinstance(item, XdmNode):
            return False
        path = alternative.path
        assert path is not None
        if self.strategy == "forward" or alternative.requires_forward:
            return self._forward_match(alternative, path, item, environment)
        return self._reverse_path(path.first, item, environment)

    def _select_environment(self, environment: ExpressionEnvironment) -> None:
        if environment is self._environment:
            return
        if self._environment is not None:
            self.statistics.cache_invalidations += 1
        self._environment = environment
        self._matches.clear()

    # -- formal whole-tree plan ------------------------------------------------

    def _forward_match(
        self,
        alternative: PatternAlternative,
        path: IntersectExceptExpr,
        item: XdmNode,
        environment: ExpressionEnvironment,
    ) -> bool:
        root = item.root
        self.statistics.equivalent_expression_evaluations += 1
        nodes: dict[XdmNode, None] = {}
        for context_node in (root, *root.descendants):
            for node in _evaluate_intersect_except(
                path,
                context_node,
                environment,
                item,
                self.statistics,
                self.pattern.context.schema,
            ):
                nodes[node] = None
        return item in nodes

    # -- optimized right-to-left plan -----------------------------------------

    def _reverse_path(
        self, path: PathExpr, item: XdmNode, environment: ExpressionEnvironment
    ) -> bool:
        if isinstance(path, AbsoluteOnly):
            return item.kind is NodeKind.DOCUMENT and item.parent is None
        if isinstance(path, AbsolutePath):
            root = item.root
            if root.kind is not NodeKind.DOCUMENT:
                return False
            return self._reverse_relative(
                path.relative, item, environment, root, top=False, descendant_entry=False
            )
        if isinstance(path, DescendantPath):
            root = item.root
            if root.kind is not NodeKind.DOCUMENT:
                return False
            return self._reverse_relative(
                path.relative, item, environment, root, top=False, descendant_entry=True
            )
        if isinstance(path, RootedPath):
            return self._reverse_rooted(path, item, environment)
        root = item.root
        return self._reverse_relative(
            path, item, environment, root, top=True, descendant_entry=True
        )

    def _reverse_relative(
        self,
        relative: RelativePath,
        item: XdmNode,
        environment: ExpressionEnvironment,
        start: XdmNode,
        *,
        top: bool,
        descendant_entry: bool,
    ) -> bool:
        steps = relative.steps
        if len(steps) == 1:
            step = steps[0][1]
            if not descendant_entry:
                return self._step_selects(step, item, start, environment, top=top)
            for context_node in self._reverse_contexts(step, item, top=top):
                if _is_descendant_or_self(context_node, start) and (
                    self._step_selects(step, item, context_node, environment, top=top)
                ):
                    return True
            return False
        separator, last = steps[-1]
        prefix = RelativePath(steps[:-1])
        for context_node in self._reverse_contexts(last, item, top=False):
            if not self._step_selects(last, item, context_node, environment, top=False):
                continue
            if separator == "/":
                if self._reverse_relative(
                    prefix,
                    context_node,
                    environment,
                    start,
                    top=top,
                    descendant_entry=descendant_entry,
                ):
                    return True
                continue
            for possible in (context_node, *context_node.ancestors):
                self.statistics.ancestor_steps += 1
                if self._reverse_relative(
                    prefix,
                    possible,
                    environment,
                    start,
                    top=top,
                    descendant_entry=descendant_entry,
                ):
                    return True
        return False

    def _reverse_rooted(
        self, path: RootedPath, item: XdmNode, environment: ExpressionEnvironment
    ) -> bool:
        base = _rooted_base(path.base, item, environment, self.statistics)
        base = self._filter(base, path.predicates, environment, item)
        if path.following is None:
            return any(node is item for node in base)
        assert path.following_separator is not None
        for node in base:
            if self._reverse_relative(
                path.following,
                item,
                environment,
                node,
                top=False,
                descendant_entry=path.following_separator == "//",
            ):
                return True
        return False

    def _reverse_contexts(self, step: StepExpr, item: XdmNode, *, top: bool) -> tuple[XdmNode, ...]:
        if isinstance(step, PostfixStep):  # pragma: no cover - forward plan owns postfix
            return ()
        effective = step.top_axis if top else step.axis
        kind = item.kind
        if effective == "child":
            parent = item.parent
            return () if parent is None or kind not in _ELEMENT_AXIS_KINDS else (parent,)
        if effective == "child-or-top":
            if kind not in _ELEMENT_AXIS_KINDS:
                return ()
            parent = item.parent
            if parent is not None:
                return (parent,)
            return (item,)
        if effective == "attribute":
            parent = item.parent
            return () if parent is None or kind is not NodeKind.ATTRIBUTE else (parent,)
        if effective == "attribute-or-top":
            if kind is not NodeKind.ATTRIBUTE:
                return ()
            parent = item.parent
            return (item,) if parent is None else (parent,)
        if effective == "namespace":
            parent = item.parent
            return () if parent is None or kind is not NodeKind.NAMESPACE else (parent,)
        if effective == "namespace-or-top":
            if kind is not NodeKind.NAMESPACE:
                return ()
            parent = item.parent
            return (item,) if parent is None else (parent,)
        if effective == "self":
            return (item,)
        if effective == "descendant":
            if kind not in _ELEMENT_AXIS_KINDS:
                return ()
            ancestors = item.ancestors
            self.statistics.ancestor_steps += len(ancestors)
            return ancestors
        if kind not in _DESCENDANT_OR_SELF_KINDS:
            return ()
        ancestors = (item, *item.ancestors)
        self.statistics.ancestor_steps += len(ancestors) - 1
        return ancestors

    def _step_selects(
        self,
        step: StepExpr,
        item: XdmNode,
        context_node: XdmNode,
        environment: ExpressionEnvironment,
        *,
        top: bool,
    ) -> bool:
        if isinstance(step, PostfixStep):  # pragma: no cover - forward plan owns postfix
            return False
        nodes = _axis_nodes(step, context_node, top)
        nodes = tuple(
            node
            for node in nodes
            if _node_test_matches(step.node_test, node, step.axis, self.pattern.context.schema)
        )
        nodes = self._filter(nodes, step.predicates, environment, item)
        return any(node is item for node in nodes)

    # -- shared predicate and rooted evaluation -------------------------------

    def _filter(
        self,
        nodes: Sequence[XdmNode],
        predicates: Sequence[CompiledPredicate],
        environment: ExpressionEnvironment,
        candidate: XdmNode,
    ) -> tuple[XdmNode, ...]:
        result = tuple(nodes)
        for predicate in predicates:
            filtered: list[XdmNode] = []
            size = len(result)
            for position, node in enumerate(result, 1):
                self.statistics.predicate_evaluations += 1
                value = _evaluate(
                    predicate.expression,
                    candidate,
                    environment,
                    Focus.singleton(node, position=position, size=size),
                )
                if _predicate_true(value, position):
                    filtered.append(node)
            result = tuple(filtered)
        return result


# ---------------------------------------------------------------------------
# Axis and node-test semantics


def _axis_nodes(step: AxisStep, context_node: XdmNode, top: bool) -> tuple[XdmNode, ...]:
    axis = step.top_axis if top else step.axis
    if axis == "child":
        return context_node.children
    if axis == "child-or-top":
        if context_node.parent is None and context_node.kind in _ELEMENT_AXIS_KINDS:
            return (context_node,)
        return context_node.children
    if axis == "attribute":
        return context_node.attributes
    if axis == "attribute-or-top":
        if context_node.parent is None and context_node.kind is NodeKind.ATTRIBUTE:
            return (context_node,)
        return context_node.attributes
    if axis == "namespace":
        return context_node.namespace_nodes
    if axis == "namespace-or-top":
        if context_node.parent is None and context_node.kind is NodeKind.NAMESPACE:
            return (context_node,)
        return context_node.namespace_nodes
    if axis == "self":
        return (context_node,)
    if axis == "descendant":
        return context_node.descendants
    return (context_node, *context_node.descendants)


def _node_test_matches(
    node_test: NodeTest, node: XdmNode, axis: str, schema: SchemaPool | None
) -> bool:
    if isinstance(node_test, NameTest):
        if node.kind is not _principal_kind_node(axis):
            return False
        return node_test.matches(node)
    item_type = node_test.item_type
    if not _kind_matches(item_type, node, schema):
        return False
    if node_test.declarations:
        return _declaration_matches(node, node_test.declarations, item_type.kind)
    if node_test.child_declarations:
        child = item_type.child
        assert child is not None
        return _declaration_matches(
            cast(XdmDocument, node).root_element,
            node_test.child_declarations,
            child.kind,
        )
    return True


def _kind_matches(item_type: ItemType, node: XdmNode, schema: SchemaPool | None) -> bool:
    kind = item_type.kind
    if kind == "node":
        return True
    if kind == "text":
        return node.kind is NodeKind.TEXT
    if kind == "comment":
        return node.kind is NodeKind.COMMENT
    if kind == "namespace-node":
        return node.kind is NodeKind.NAMESPACE
    if kind == "processing-instruction":
        if node.kind is not NodeKind.PROCESSING_INSTRUCTION:
            return False
        return item_type.target is None or node.target == item_type.target
    if kind in {"element", "schema-element"}:
        if node.kind is not NodeKind.ELEMENT:
            return False
    elif kind in {"attribute", "schema-attribute"}:
        if node.kind is not NodeKind.ATTRIBUTE:
            return False
    elif kind == "document-node":
        if node.kind is not NodeKind.DOCUMENT:
            return False
        if item_type.child is None:
            return True
        return _kind_matches(item_type.child, cast(XdmDocument, node).root_element, schema)
    else:  # pragma: no cover - every node kind is handled above
        return False
    if (
        kind in {"element", "attribute"}
        and item_type.name is not None
        and not item_type.wildcard
        and node.name != item_type.name
    ):
        return False
    if kind in {"element", "attribute"} and item_type.type_name is not None:
        return _type_matches(node, item_type.type_name, item_type.nillable, schema)
    return True


def _declaration_matches(node: XdmNode, declarations: tuple[str, ...], kind: str) -> bool:
    declaration = node.annotation.governing_declaration
    key = _declaration_key(declaration, require_global=kind == "schema-element")
    if key is None or key not in declarations:
        return False
    if kind == "schema-attribute":
        return node.kind is NodeKind.ATTRIBUTE and node.name in declarations
    return node.kind is NodeKind.ELEMENT


def _declaration_key(declaration: Any, *, require_global: bool) -> str | None:
    if declaration is None:
        return None
    name = getattr(declaration, "name", None)
    if not isinstance(name, str) or not name:
        return None
    if name.startswith("{"):
        return name
    if require_global:
        is_global = getattr(declaration, "isGlobalDeclaration", None)
        if callable(is_global) and not is_global():
            return None
    get_namespace = getattr(declaration, "getNamespace", None)
    namespace = get_namespace() if callable(get_namespace) else None
    if namespace is None:
        parent = getattr(declaration, "parent", None)
        namespace = getattr(parent, "target_namespace", None)
    return f"{{{namespace}}}{name}" if namespace else name


def _type_matches(node: XdmNode, type_name: str, nillable: bool, schema: SchemaPool | None) -> bool:
    annotation = node.annotation
    if annotation.nilled and not nillable:
        return False
    actual_name = annotation.type_name or node.type_name
    if actual_name == type_name:
        return True
    actual_class = getattr(annotation, "actual_type", None)
    declared_class = _declared_class(type_name, schema)
    if isinstance(actual_class, type) and isinstance(declared_class, type):
        return issubclass(actual_class, declared_class)
    return _builtin_derives(actual_name, type_name)


def _declared_class(type_name: str, schema: SchemaPool | None) -> type[Any] | None:
    declared = schema.get_type(type_name) if schema is not None else None
    if declared is not None:
        return getattr(declared, "_type_class", None)
    return _builtin_class(type_name)


def _builtin_class(type_name: str) -> type[Any] | None:
    namespace = _namespace_of(type_name)
    local = _local_name(type_name)
    if namespace != XSD_NS:
        return None
    from pyxsd.xdm.annotations import builtin_type_classes

    return builtin_type_classes().get(local)


def _builtin_derives(actual_name: str | None, declared_name: str) -> bool:
    if actual_name is None:
        return False
    actual_local = _local_name(actual_name)
    declared_local = _local_name(declared_name)
    if declared_local == "anyType":
        return True
    if actual_local == declared_local:
        return True
    if declared_local == "anySimpleType":
        return actual_local not in {"anyType", "untyped"}
    if declared_local == "anyAtomicType":
        return actual_local not in {"anyType", "untyped", "anySimpleType"}
    actual_class = _builtin_class(actual_name)
    declared_class = _builtin_class(declared_name)
    if isinstance(actual_class, type) and isinstance(declared_class, type):
        return issubclass(actual_class, declared_class)
    return False


# ---------------------------------------------------------------------------
# Forward evaluation (formal root(.)//EE plan)


def _evaluate_intersect_except(
    expr: IntersectExceptExpr,
    context_node: XdmNode,
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
    schema: SchemaPool | None,
) -> tuple[XdmNode, ...]:
    result = _evaluate_path(expr.first, context_node, environment, candidate, statistics, schema)
    for operator, path in expr.rest:
        other = set(_evaluate_path(path, context_node, environment, candidate, statistics, schema))
        if operator == "intersect":
            result = tuple(node for node in result if node in other)
        else:
            result = tuple(node for node in result if node not in other)
    return result


def _evaluate_union(
    union: UnionExpr,
    context_node: XdmNode,
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
    schema: SchemaPool | None,
) -> tuple[XdmNode, ...]:
    nodes: dict[XdmNode, None] = {}
    for alternative in union.alternatives:
        for node in _evaluate_intersect_except(
            alternative, context_node, environment, candidate, statistics, schema
        ):
            nodes[node] = None
    return tuple(sorted(nodes, key=lambda node: (id(node.root), node.document_order)))


def _evaluate_path(
    path: PathExpr,
    context_node: XdmNode,
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
    schema: SchemaPool | None,
) -> tuple[XdmNode, ...]:
    if isinstance(path, RelativePath):
        return _evaluate_steps(
            path, (context_node,), environment, candidate, statistics, schema, top=True
        )
    if isinstance(path, AbsoluteOnly):
        root = context_node.root
        return (root,) if root.kind is NodeKind.DOCUMENT else ()
    if isinstance(path, AbsolutePath):
        root = context_node.root
        if root.kind is not NodeKind.DOCUMENT:
            return ()
        return _evaluate_steps(
            path.relative,
            (root,),
            environment,
            candidate,
            statistics,
            schema,
            top=False,
        )
    if isinstance(path, DescendantPath):
        root = context_node.root
        if root.kind is not NodeKind.DOCUMENT:
            return ()
        return _evaluate_steps(
            path.relative,
            (root, *root.descendants),
            environment,
            candidate,
            statistics,
            schema,
            top=False,
        )
    base = _rooted_base(path.base, context_node, environment, statistics)
    base = _filter_sequence(base, path.predicates, environment, candidate, statistics)
    if path.following is None:
        return base
    assert path.following_separator is not None
    results: list[XdmNode] = []
    for node in base:
        results.extend(
            _evaluate_steps(
                path.following,
                (node, *node.descendants) if path.following_separator == "//" else (node,),
                environment,
                candidate,
                statistics,
                schema,
                top=False,
            )
        )
    return tuple(results)


def _evaluate_steps(
    relative: RelativePath,
    contexts: tuple[XdmNode, ...],
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
    schema: SchemaPool | None,
    *,
    top: bool,
) -> tuple[XdmNode, ...]:
    current = contexts
    first = True
    for separator, step in relative.steps:
        produced: list[XdmNode] = []
        if first:
            for context_node in current:
                produced.extend(
                    _evaluate_step(
                        step,
                        context_node,
                        environment,
                        candidate,
                        statistics,
                        schema,
                        top=top,
                    )
                )
            first = False
        elif separator == "/":
            for context_node in current:
                produced.extend(
                    _evaluate_step(
                        step,
                        context_node,
                        environment,
                        candidate,
                        statistics,
                        schema,
                        top=False,
                    )
                )
        else:
            for context_node in current:
                for descendant in (context_node, *context_node.descendants):
                    produced.extend(
                        _evaluate_step(
                            step,
                            descendant,
                            environment,
                            candidate,
                            statistics,
                            schema,
                            top=False,
                        )
                    )
        current = tuple(produced)
    return current


def _evaluate_step(
    step: StepExpr,
    context_node: XdmNode,
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
    schema: SchemaPool | None,
    *,
    top: bool,
) -> tuple[XdmNode, ...]:
    if isinstance(step, PostfixStep):
        nodes = _evaluate_union(
            step.union, context_node, environment, candidate, statistics, schema
        )
        return _filter_sequence(nodes, step.predicates, environment, candidate, statistics)
    nodes = tuple(
        node
        for node in _axis_nodes(step, context_node, top)
        if _node_test_matches(step.node_test, node, step.axis, schema)
    )
    return _filter_sequence(nodes, step.predicates, environment, candidate, statistics)


def _filter_sequence(
    nodes: Sequence[XdmNode],
    predicates: Sequence[CompiledPredicate],
    environment: ExpressionEnvironment,
    candidate: XdmNode,
    statistics: PatternStatistics,
) -> tuple[XdmNode, ...]:
    result = tuple(nodes)
    for predicate in predicates:
        filtered: list[XdmNode] = []
        size = len(result)
        for position, node in enumerate(result, 1):
            statistics.predicate_evaluations += 1
            value = _evaluate(
                predicate.expression,
                candidate,
                environment,
                Focus.singleton(node, position=position, size=size),
            )
            if _predicate_true(value, position):
                filtered.append(node)
        result = tuple(filtered)
    return result


# ---------------------------------------------------------------------------
# Rooted bases


def _rooted_base(
    base: RootedBase,
    focus: XdmNode,
    environment: ExpressionEnvironment,
    statistics: PatternStatistics,
) -> tuple[XdmNode, ...]:
    if isinstance(base, VariableBase):
        value = environment.variables.get(base.name)
        if value is None:
            return ()
        return tuple(item for item in as_xdm(value) if isinstance(item, XdmNode))
    statistics.function_evaluations += 1
    local = base.local
    if local == "root":
        return (focus.root,)
    arguments = [_argument_value(argument, environment) for argument in base.arguments]
    if local == "doc":
        uri = _singleton_string(arguments[0])
        document = _document_for_uri(uri, environment, focus)
        return () if document is None else (document,)
    if local in {"id", "element-with-id"}:
        tokens = _id_tokens(arguments[0])
        root = focus.root
        if len(arguments) > 1:
            node = _singleton_node(arguments[1])
            if node is None:
                return ()
            root = node.root
        return _id_nodes(root, tokens)
    nodes = _key_nodes(arguments, environment)
    if len(arguments) == 2:
        root = focus.root
        nodes = tuple(node for node in nodes if node.root is root)
    return nodes


def _argument_value(argument: FunctionArgument, environment: ExpressionEnvironment) -> XdmSequence:
    if argument.kind == "literal":
        return argument.value
    assert argument.name is not None
    value = environment.variables.get(argument.name)
    if value is None:
        return ()
    return as_xdm(value)


def _singleton_string(sequence: XdmSequence) -> str:
    if len(sequence) != 1:
        raise ValueError("a rooted function argument requires one string value")
    item = sequence[0]
    if isinstance(item, XdmAtomic):
        return item.lexical
    if isinstance(item, XdmNode):
        return item.string_value
    raise TypeError("a rooted function argument is not string-valued")


def _singleton_node(sequence: XdmSequence) -> XdmNode | None:
    if len(sequence) != 1 or not isinstance(sequence[0], XdmNode):
        return None
    return sequence[0]


def _id_tokens(sequence: XdmSequence) -> frozenset[str]:
    tokens: set[str] = set()
    for item in sequence:
        if isinstance(item, XdmAtomic):
            text = item.lexical
        elif isinstance(item, XdmNode):
            text = item.string_value
        else:
            raise TypeError("an id argument is not string-valued")
        tokens.update(token for token in text.split() if token)
    return frozenset(tokens)


def _document_for_uri(
    uri: str, environment: ExpressionEnvironment, focus: XdmNode
) -> XdmDocument | None:
    candidates = [uri]
    base = environment.base_uri or focus.base_uri
    if base:
        candidates.append(urljoin(base, uri))
    for candidate in candidates:
        document = environment.documents.get(candidate)
        if document is not None:
            return document
    for document in environment.documents.values():
        if document.document_uri in candidates:
            return document
    return None


def _id_nodes(root: XdmNode, tokens: frozenset[str]) -> tuple[XdmNode, ...]:
    if not tokens:
        return ()
    found: dict[XdmNode, None] = {}
    for node in (root, *root.descendants):
        if node.kind is not NodeKind.ELEMENT:
            continue
        if (
            getattr(node.annotation, "id_kind", None) in {"ID", "id"}
            and node.string_value in tokens
        ):
            found[node] = None
        for attribute in node.attributes:
            if (
                getattr(attribute.annotation, "id_kind", None) in {"ID", "id"}
                and attribute.string_value in tokens
            ):
                found[node] = None
    return tuple(sorted(found, key=lambda node: node.document_order))


def _key_nodes(
    arguments: Sequence[XdmSequence], environment: ExpressionEnvironment
) -> tuple[XdmNode, ...]:
    name = _singleton_string(arguments[0])
    index = environment.key_indexes.get(name)
    if index is None:
        return ()
    nodes: dict[XdmNode, None] = {}
    for value in arguments[1]:
        if isinstance(value, XdmAtomic):
            keys: tuple[Any, ...] = (value.lexical, value.value, value)
        elif isinstance(value, XdmNode):
            keys = (value.string_value,)
        else:
            continue
        for key in keys:
            for node in index.get(key, ()):
                nodes[node] = None
    ordered = sorted(nodes, key=lambda node: (id(node.root), node.document_order))
    if len(arguments) > 2:
        top = _singleton_node(arguments[2])
        if top is None:
            raise TypeError("the third key() argument must be a singleton node")
        ordered = [node for node in ordered if node.root is top.root]
    return tuple(ordered)


# ---------------------------------------------------------------------------
# Predicate evaluation


def _evaluate(
    expression: CompiledExpression,
    candidate: XdmItem,
    environment: ExpressionEnvironment,
    focus: Focus,
) -> XdmSequence:
    variables = dict(environment.variables)
    variables[_CURRENT_VARIABLE] = (candidate,)
    local = ExpressionEnvironment(
        variables=variables,
        functions=environment.functions,
        function_overrides=environment.function_overrides,
        key_indexes=environment.key_indexes,
        documents=environment.documents,
        collections=environment.collections,
        default_collection=environment.default_collection,
        text_resources=environment.text_resources,
        decimal_formats=environment.decimal_formats,
        current_datetime=environment.current_datetime,
        implicit_timezone=environment.implicit_timezone,
        base_uri=environment.base_uri,
    )
    return XPathEngine(expression.version).evaluate(expression, local, focus=focus)


def _pattern_cache_key(item: XdmItem) -> object:
    """A complete identity key for one candidate item.

    XDM atomic equality is XPath value equality, so ``xs:integer(1)`` and
    ``xs:decimal("1")`` compare equal.  Predicate patterns are type-sensitive,
    so the type annotation is part of the key.  Nodes and other items use their
    identity-based equality.
    """
    if isinstance(item, XdmAtomic):
        return "atomic", item.type_name, item
    return "node" if isinstance(item, XdmNode) else "item", item


def _predicate_true(value: XdmSequence, position: int) -> bool:
    if len(value) == 1 and isinstance(value[0], XdmAtomic):
        atomic = value[0]
        if _local_name(atomic.type_name) in _NUMERIC_LOCALS:
            return _numeric_equals_position(atomic.value, position)
    return _effective_boolean_value(value)


def _numeric_equals_position(value: Any, position: int) -> bool:
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(value == position)


def _effective_boolean_value(value: XdmSequence) -> bool:
    if not value:
        return False
    first = value[0]
    if isinstance(first, XdmNode):
        return True
    if len(value) != 1 or not isinstance(first, XdmAtomic):
        raise TypeError("a predicate does not have an effective boolean value")
    local = _local_name(first.type_name)
    if local == "boolean":
        return bool(first.value)
    if local in _NUMERIC_LOCALS:
        if isinstance(first.value, float) and math.isnan(first.value):
            return False
        return first.value != 0
    if local in _STRING_LOCALS:
        return bool(str(first.value))
    raise TypeError("a predicate does not have an effective boolean value")


def _is_descendant_or_self(node: XdmNode, start: XdmNode) -> bool:
    if node is start:
        return True
    # descendant-or-self::node() never reaches attributes or namespaces from a
    # different context node, and document order alone would accept later
    # sibling subtrees.  Use the actual ancestor relation.
    if node.kind in {NodeKind.ATTRIBUTE, NodeKind.NAMESPACE}:
        return False
    return any(ancestor is start for ancestor in node.ancestors)


# ---------------------------------------------------------------------------
# Public compilation and priority helpers


def compile_pattern(
    source: str,
    context: StaticContext,
    *,
    typed: str = "unspecified",
    location: IssueLocation | None = None,
) -> CompiledPattern:
    """Parse one Pattern30 in *context* and freeze its normative alternatives.

    *typed* selects the mode's ``xsl:mode/@typed`` policy.  ``strict`` and
    ``lax`` reinterpret first-step element name tests as
    ``schema-element`` tests as required by XSLT 3.0 section 6.6.3; the other
    policies leave the pattern unchanged.
    """
    if not isinstance(context, StaticContext):
        raise TypeError("context must be a StaticContext")
    if not isinstance(source, str):
        raise TypeError("a pattern source must be a string")
    if not source.strip():
        raise _static_error(context, "a pattern must not be empty", "XTSE0340", location=location)
    parsed = parse_pattern(source, context, typed=typed, location=location)
    alternatives = tuple(
        _compiled_alternative(index, alternative) for index, alternative in enumerate(parsed)
    )
    return CompiledPattern(source, context, alternatives)


def _compiled_alternative(index: int, alternative: ParsedAlternative) -> PatternAlternative:
    """Attach dispatch metadata to one parsed pattern alternative."""
    if alternative.predicate is not None:
        return PatternAlternative(
            index=index,
            priority=alternative.priority,
            predicate=alternative.predicate,
            filter=CandidateFilter(all_items=True),
        )
    path = alternative.path
    assert path is not None
    return PatternAlternative(
        index=index,
        priority=alternative.priority,
        path=path,
        filter=_candidate_filter(path),
        requires_forward=_requires_forward(path),
    )


def default_priority(
    pattern: CompiledPattern | str, context: StaticContext | None = None
) -> Decimal:
    """Return the highest default priority of *pattern*'s union alternatives."""
    if isinstance(pattern, str):
        if context is None:
            context = StaticContext()
        pattern = compile_pattern(pattern, context)
    elif not isinstance(pattern, CompiledPattern):
        raise TypeError("pattern must be a CompiledPattern or a string")
    return max(pattern.default_priorities)
