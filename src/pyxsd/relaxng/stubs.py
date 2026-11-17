"""Full and compact typing stubs for compiled RELAX NG projections.

The compiled grammar owns one finite projection plan and one generated class
per stable route.  Those classes are created at runtime and are not importable
Python declarations, so this module renders the typing surface a caller would
otherwise have to reconstruct by hand.  The plan is the only source of class
layouts: this module never reads XSD declarations, never treats a ``define``
name as a class, and never asks an invalid schema for its projection.

Full mode is importable-quality ``.pyi`` source: a deterministic provenance and
consumption header, imports used by the rendered body, one class block per
selected stable route, generic-route comments, and start aliases.  Every full
annotation is routed through private module aliases and post-class type aliases,
so legal XML member and start names such as ``list``, ``str``, or
``RelaxNGElement`` cannot shadow the types a class body names.  Compact mode is
the agent-oriented description: a two-line header with an alias
legend, one line per class and member, generic-route comments, and a trailing
start line.  Neither mode calls :meth:`RelaxNGSchema.require_valid`; a schema
whose report has errors emits a deterministic explanatory comment instead.

Component filtering matches the linear XSD stub workflow: ``component_ids``
names introspection components and ``depth`` bounds outgoing ``uses`` edges.
Only selected ``route`` components can yield classes.  A non-route component is
an inventory seed, not a class: selecting a definition or pattern emits no
class unless its dependency closure reaches selected routes (as the grammar
component does).  Unknown IDs raise ``KeyError`` and a negative depth raises
``ValueError``; selection never mutates the schema or its plan.

Annotations describe valid typed documents.  A unique decodable atomic scalar
names the decoded Python/XSD value class; a text, ambiguous, nondecodable, or
plainly unclassifiable scalar is ``AlternativeValue``.  A provable plain list
names its tuple/list item shape and otherwise remains ``AlternativeValue``.
Those scalar annotations take an absence arm when an element with no leading
character data is valid.  A stable child names its generated class, a generic
child is ``RelaxNGElement``, and a child whose class the component selection
excludes is ``Any`` with the route, depth, and exclusion in the comment.
Open-name children are not turned into fabricated descriptors; the class
comment documents the ordered ``WILDCARD_CHILDREN`` view instead.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unicodedata import normalize

from pyxsd import __version__, xsd_data_types
from pyxsd.relaxng.contracts import ScalarContract, scalar_contract
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    XSD_LIBRARY,
    DatatypeContext,
    ScalarDatatype,
    datatype_supports_decode,
)
from pyxsd.relaxng.introspection import (
    describe_component,
    describe_schema,
    related_components,
)
from pyxsd.relaxng.patterns import (
    Choice,
    Data,
    DataExcept,
    Empty,
    Group,
    List,
    OneOrMore,
    Pattern,
    Ref,
    Text,
    Value,
)
from pyxsd.relaxng.projection import ContentContract, RouteContract

if TYPE_CHECKING:
    from pyxsd.relaxng.projection import ProjectionPlan
    from pyxsd.relaxng.schema import RelaxNGSchema

__all__ = ["emit_relaxng_stub"]

#: Expanded-name local type names whose XSD 1.0 decoder returns a lexical
#: ``str`` subclass rather than the shared calendar class.  The public
#: annotation is ``str``; the private wrapper class is not a stable typing name.
_LEXICAL_YEAR_TYPES = frozenset({"date", "dateTime", "gYear", "gYearMonth"})

#: The XSD built-in classes keyed by their datatype local name.  Only names a
#: compiled XSD contract can carry reach this table; an unknown name falls back
#: to ``Any`` with a reason instead of naming a made-up class.
_XSD_TYPE_NAMES: dict[str, type[Any]] = {
    value.__dict__["name"]: value
    for value in vars(xsd_data_types).values()
    if isinstance(value, type) and isinstance(value.__dict__.get("name"), str)
}

#: Built-in library types are the two RELAX NG string datatypes.
_BUILTIN_TYPES: dict[str, type[Any]] = {
    "string": xsd_data_types.String,
    "token": xsd_data_types.Token,
}

#: Fixed compact alias spellings, in legend order.
_COMPACT_FIXED = (
    ("any", "Any", "typing.Any"),
    ("alternative", "Alt", "pyxsd.relaxng.values.AlternativeValue"),
    ("mutable", "Seq", "collections.abc.MutableSequence"),
    ("element", "RNG", "pyxsd.relaxng.tree.RelaxNGElement"),
)

#: Private full-mode alias stems.  Each is suffixed until it is absent from the
#: complete set of generated class names, member aliases, and start candidates.
_FULL_ALIAS_BASES = (
    ("builtins", "_builtins"),
    ("mutable", "_mutable"),
    ("any", "_any"),
    ("alternative", "_alt"),
    ("element", "_rng"),
    ("xsd", "_xsd"),
)

#: Module bindings a full start alias must not recreate at module scope.
_RESERVED_START_NAMES = frozenset(
    {
        "annotations",
        "__name__",
        "__doc__",
        "__package__",
        "__loader__",
        "__spec__",
        "__file__",
        "__cached__",
        "__builtins__",
        "__annotations__",
        "__all__",
    }
)


@dataclass(frozen=True)
class _TypeRef:
    """One rendered annotation and the explanation its line carries."""

    text: str
    reason: str | None = None


@dataclass(frozen=True)
class _Member:
    """One class-level descriptor annotation."""

    name: str
    annotation: _TypeRef


@dataclass(frozen=True)
class _ClassBlock:
    """One stable route's class block."""

    name: str
    route_id: str
    comments: tuple[str, ...]
    members: tuple[_Member, ...]


@dataclass(frozen=True)
class _GenericRoute:
    """One route with no generated class."""

    route_id: str
    label: str
    reason: str


@dataclass(frozen=True)
class _Start:
    """One compiled start route and the alias decision for it."""

    route_id: str
    label: str
    alias: str | None
    type_name: str | None
    comment: str | None
    type_alias: str | None = None


@dataclass(frozen=True)
class _FullNames:
    """Private full-mode names chosen against the whole emitted namespace."""

    builtins: str
    mutable: str
    any: str
    alternative: str
    element: str
    xsd: str
    class_aliases: dict[str, str]
    class_order: tuple[str, ...]

    @property
    def private_names(self) -> frozenset[str]:
        """Every module-level private alias a root name must not duplicate."""
        return frozenset(
            {
                self.builtins,
                self.mutable,
                self.any,
                self.alternative,
                self.element,
                self.xsd,
                *self.class_aliases.values(),
            }
        )


def emit_relaxng_stub(
    schema: RelaxNGSchema,
    *,
    compact: bool = False,
    component_ids: tuple[str, ...] | None = None,
    depth: int | None = None,
) -> str:
    """Return the typing stub text for *schema*'s stable projections.

    Full mode is an importable-quality ``.pyi`` document.  Compact mode is a
    deterministic agent view with a two-line header and alias legend.  Both
    modes render one block per selected stable route, a comment for each
    selected generic route, and start aliases or start comments.

    ``component_ids`` selects RELAX NG introspection IDs; ``depth`` bounds
    their outgoing ``uses`` closure (``None`` follows every reachable
    component).  An empty selection emits no classes or aliases.  Unknown IDs
    raise ``KeyError`` and negative depths raise ``ValueError``.  A non-route
    component is only an inventory seed; it never becomes a class itself.

    The schema's error report is never turned into an exception here.  An
    invalid schema emits deterministic explanatory text and its projection
    plan is never accessed.  Selection never mutates the schema.
    """
    if depth is not None and depth < 0:
        raise ValueError("depth must be non-negative")
    if component_ids is not None:
        for component_id in component_ids:
            describe_component(schema, component_id)
    if schema.report.has_errors:
        return _render_unavailable(schema, compact=compact)

    plan = schema.projection
    selected_routes: tuple[str, ...]
    if component_ids is None:
        selected_routes = tuple(plan.routes)
        filtered = False
    else:
        selected = _selection(schema, component_ids, depth)
        selected_routes = tuple(route_id for route_id in plan.routes if route_id in selected)
        filtered = True

    factory = schema.projection_classes
    class_names: dict[str, str] = {}
    classes: dict[str, type[Any]] = {}
    generic_routes: list[_GenericRoute] = []
    for route_id in selected_routes:
        route = plan.routes[route_id]
        if route.generic_reason is None:
            cls = factory.class_for(route_id)
            classes[route_id] = cls
            class_names[route_id] = cls.__name__
        else:
            generic_routes.append(
                _GenericRoute(route_id, _route_label(route), _comment_text(route.generic_reason))
            )

    full_names = _full_names(plan, selected_routes, class_names, classes)
    used: set[str] = set()
    blocks = tuple(
        _class_block(
            plan,
            plan.routes[route_id],
            classes[route_id],
            class_names,
            depth,
            used,
        )
        for route_id in selected_routes
        if route_id in class_names
    )
    starts = _starts(
        plan,
        class_names,
        selected_routes,
        filtered,
        full_names.private_names,
        full_names.class_aliases,
    )
    notice = _empty_notice(plan, class_names, selected_routes, filtered)

    if compact:
        return _render_compact(schema, blocks, generic_routes, starts, notice, used)
    return _render_full(schema, blocks, generic_routes, starts, notice, full_names, class_names)


def _suffixed_name(base: str, taken: set[str]) -> str:
    """Return *base* or its first free numeric suffix, and reserve it."""
    candidate = base
    index = 2
    while candidate in taken:
        candidate = f"{base}_{index}"
        index += 1
    taken.add(candidate)
    return candidate


def _full_names(
    plan: ProjectionPlan,
    selected_routes: tuple[str, ...],
    class_names: dict[str, str],
    classes: dict[str, type[Any]],
) -> _FullNames:
    """Choose private aliases absent from every emitted class and member name."""
    taken = set(class_names.values())
    for route_id in selected_routes:
        if route_id not in class_names:
            continue
        route = plan.routes[route_id]
        taken.update(field.alias for field in route.attributes.values())
        taken.update(field.alias for field in route.children.values())
        scalar_field = classes[route_id].__dict__.get("_relaxng_scalar_field_")
        if isinstance(scalar_field, str):
            taken.add(scalar_field)
    for route_id in plan.roots:
        expanded_name = plan.routes[route_id].expanded_name
        if expanded_name is not None:
            taken.add(_alias_name(expanded_name))

    fixed = {key: _suffixed_name(base, taken) for key, base in _FULL_ALIAS_BASES}

    class_aliases: dict[str, str] = {}
    class_order: list[str] = []
    index = 0
    for route_id in selected_routes:
        if route_id not in class_names:
            continue
        class_aliases[route_id] = _suffixed_name(f"_t{index}", taken)
        class_order.append(route_id)
        index += 1

    return _FullNames(
        builtins=fixed["builtins"],
        mutable=fixed["mutable"],
        any=fixed["any"],
        alternative=fixed["alternative"],
        element=fixed["element"],
        xsd=fixed["xsd"],
        class_aliases=class_aliases,
        class_order=tuple(class_order),
    )


def _selection(
    schema: RelaxNGSchema, component_ids: tuple[str, ...], depth: int | None
) -> set[str]:
    """Return selected component IDs; a non-route seed stays only a seed."""
    description = describe_schema(schema)
    reach = len(description.components) if depth is None else depth
    selected: set[str] = set()
    for component_id in component_ids:
        selected.add(component_id)
        selected.update(
            component.id
            for component in related_components(schema, component_id, direction="uses", depth=reach)
        )
    return selected


def _route_label(route: RouteContract) -> str:
    """Return a deterministic human label for one route."""
    return route.expanded_name or "open element name class"


def _comment_text(text: str) -> str:
    """Fold line breaks so a reason can never break a comment line."""
    return " ".join(text.split())


def _scalar_datatype(plan: ProjectionPlan, contract: ScalarContract) -> Any | None:
    """Return the compiled datatype behind *contract*, when it has one."""
    pattern = plan.encoding_patterns.get(contract.pattern_identity)
    return getattr(pattern, "datatype", None)


def _value_ref(type_name: str, xsd_type: type[Any]) -> _TypeRef:
    """Return the decoded public value class for one known datatype."""
    if type_name in {"QName", "NOTATION"}:
        return _TypeRef("_xsd.QName")
    if type_name in _LEXICAL_YEAR_TYPES:
        return _TypeRef("str")
    if issubclass(xsd_type, xsd_data_types.Boolean):
        return _TypeRef("bool")
    if issubclass(xsd_type, (xsd_data_types.Base64Binary, xsd_data_types.HexBinary)):
        return _TypeRef("bytes")
    if xsd_type.__module__ == xsd_data_types.__name__ and hasattr(
        xsd_data_types, xsd_type.__name__
    ):
        return _TypeRef(f"_xsd.{xsd_type.__name__}")
    if xsd_type.__module__ == "builtins" and xsd_type.__name__ in {
        "bool",
        "bytes",
        "float",
        "int",
        "str",
    }:
        return _TypeRef(xsd_type.__name__)
    return _TypeRef(
        "_Any",
        f"decoded value class {xsd_type.__module__}.{xsd_type.__qualname__} has no "
        "stable import in this stub; Any avoids an unresolved name",
    )


def _atomic_ref(plan: ProjectionPlan, contract: ScalarContract, used: set[str]) -> _TypeRef:
    """Return the annotation for one unique atomic scalar contract."""
    datatype = _scalar_datatype(plan, contract)
    if datatype is None:
        used.add("any")
        return _TypeRef(
            "_Any",
            "no compiled datatype is registered for this scalar contract; "
            "Any avoids naming an unproven Python type",
        )
    if not datatype_supports_decode(datatype):
        used.add("alternative")
        return _TypeRef(
            "_AlternativeValue",
            "the datatype declares no decoder; runtime reads return AlternativeValue",
        )
    xsd_type: type[Any] | None
    if contract.library_uri == BUILTIN_LIBRARY:
        xsd_type = _BUILTIN_TYPES.get(contract.type_name or "")
    elif contract.library_uri == XSD_LIBRARY:
        xsd_type = _XSD_TYPE_NAMES.get(contract.type_name or "")
    elif type(datatype) is ScalarDatatype:
        xsd_type = datatype.xsd_type
    else:
        xsd_type = None
    if xsd_type is None:
        used.add("any")
        return _TypeRef(
            "_Any",
            f"datatype library {contract.library_uri!r} type "
            f"{contract.type_name!r} has no static Python value class; "
            "Any avoids a guess",
        )
    ref = _value_ref(contract.type_name or "", xsd_type)
    if ref.text == "_Any":
        used.add("any")
    elif ref.text.startswith("_xsd."):
        used.add("xsd")
    return ref


@dataclass(frozen=True)
class _ListItems:
    """One statically plain list item shape and its decoder eligibility."""

    homogeneous: bool
    items: tuple[ScalarContract, ...]
    decodable: bool = True


def _list_children(pattern: Pattern) -> tuple[Pattern, ...]:
    """Return the operands that decide one plain list token shape."""
    if isinstance(pattern, Ref):
        return (pattern.target,)
    if isinstance(pattern, Group):
        return (pattern.first, pattern.second)
    if isinstance(pattern, Choice):
        return (pattern.first, pattern.second)
    if isinstance(pattern, OneOrMore):
        return (pattern.pattern,)
    return ()


def _combine_list(pattern: Pattern, results: dict[int, _ListItems | None]) -> _ListItems | None:
    """Combine already-analysed operands into a plain list item shape."""
    if isinstance(pattern, Ref):
        return results.get(id(pattern.target))
    if isinstance(pattern, Empty):
        return _ListItems(False, ())
    if isinstance(pattern, (Data, DataExcept, Value)):
        return _ListItems(
            False,
            (scalar_contract(pattern),),
            datatype_supports_decode(pattern.datatype),
        )
    if isinstance(pattern, Group):
        first = results.get(id(pattern.first))
        second = results.get(id(pattern.second))
        if first is None or second is None or first.homogeneous or second.homogeneous:
            return None
        return _ListItems(
            False,
            first.items + second.items,
            first.decodable and second.decodable,
        )
    if isinstance(pattern, OneOrMore):
        item = results.get(id(pattern.pattern))
        if item is None or item.homogeneous or len(item.items) != 1:
            return None
        return _ListItems(True, item.items, item.decodable)
    if isinstance(pattern, Choice):
        first = results.get(id(pattern.first))
        second = results.get(id(pattern.second))
        if first is None or second is None:
            return None
        if not first.homogeneous and not first.items and second.homogeneous:
            return second
        if not second.homogeneous and not second.items and first.homogeneous:
            return first
        return None
    return None


def _list_items(pattern: Pattern) -> _ListItems | None:
    """Analyse one list pattern without Python recursion or hidden guessing.

    A cyclic or non-token list shape returns ``None``: the runtime then keeps
    the whole-list ``AlternativeValue`` interpretation, so the annotation must
    not invent a tuple/list item type.
    """
    if not isinstance(pattern, List):
        return None
    content = pattern.pattern
    results: dict[int, _ListItems | None] = {}
    visiting: set[int] = set()
    pending: list[tuple[Pattern, bool]] = [(content, False)]
    while pending:
        node, ready = pending.pop()
        key = id(node)
        if key in results:
            continue
        if ready:
            visiting.discard(key)
            results[key] = _combine_list(node, results)
            continue
        if key in visiting:
            return None
        visiting.add(key)
        pending.append((node, True))
        for child in _list_children(node):
            if id(child) not in results:
                pending.append((child, False))
    return results.get(id(content))


def _list_ref(plan: ProjectionPlan, pattern: Pattern, used: set[str]) -> _TypeRef:
    """Return the annotation for one unique whole-list contract."""
    items = _list_items(pattern)
    if items is None:
        used.add("alternative")
        return _TypeRef(
            "_AlternativeValue",
            "the whole-list shape is not a statically plain token sequence; "
            "runtime reads return AlternativeValue",
        )
    if not items.decodable:
        used.add("alternative")
        return _TypeRef(
            "_AlternativeValue",
            "a list item datatype declares no decoder; runtime reads return AlternativeValue",
        )
    refs = tuple(_atomic_ref(plan, item, used) for item in items.items)
    reason = next((ref.reason for ref in refs if ref.reason is not None), None)
    if items.homogeneous:
        return _TypeRef(f"list[{refs[0].text}]", reason)
    if not refs:
        return _TypeRef("tuple[()]")
    return _TypeRef(f"tuple[{', '.join(ref.text for ref in refs)}]", reason)


def _list_allows_empty(pattern: Pattern) -> bool:
    """Return whether one compiled list content accepts zero tokens."""
    if isinstance(pattern, Ref):
        return _list_allows_empty(pattern.target)
    if isinstance(pattern, Empty):
        return True
    if isinstance(pattern, (Data, DataExcept, Value, Text)):
        return False
    if isinstance(pattern, Group):
        return _list_allows_empty(pattern.first) and _list_allows_empty(pattern.second)
    if isinstance(pattern, Choice):
        return _list_allows_empty(pattern.first) or _list_allows_empty(pattern.second)
    if isinstance(pattern, OneOrMore):
        return _list_allows_empty(pattern.pattern)
    return True


def _scalar_allows_empty(plan: ProjectionPlan, contract: ScalarContract) -> bool:
    """Return whether one scalar contract accepts an element with no text."""
    pattern = plan.encoding_patterns.get(contract.pattern_identity)
    if isinstance(pattern, Text):
        return True
    if isinstance(pattern, Value):
        return pattern.lexical == ""
    if isinstance(pattern, List):
        return _list_allows_empty(pattern.pattern)
    datatype = _scalar_datatype(plan, contract)
    if datatype is None:
        return True
    context = getattr(datatype, "context", None)
    if not isinstance(context, DatatypeContext):
        context = DatatypeContext(None, {})
    try:
        return bool(datatype.allows("", context))
    except (TypeError, ValueError):
        return True


def _model_allows_empty(plan: ProjectionPlan, model: ContentContract) -> bool:
    """Return whether an element content model permits absent leading text."""
    if model.kind == "empty":
        return True
    if model.kind == "notAllowed":
        return False
    if model.kind == "attribute":
        return True
    if model.kind == "scalar":
        return any(_scalar_allows_empty(plan, item) for item in model.scalar_contracts)
    if model.kind == "choice":
        return any(_model_allows_empty(plan, item) for item in model.operands)
    if model.kind in {"group", "interleave"}:
        return all(_model_allows_empty(plan, item) for item in model.operands)
    if model.kind == "repeat":
        return _model_allows_empty(plan, model.operands[0])
    return True


def _scalar_ref(
    plan: ProjectionPlan,
    contracts: tuple[ScalarContract, ...],
    used: set[str],
    *,
    empty_reason: str,
) -> _TypeRef:
    """Return the annotation for one scalar position's complete contract set."""
    unique = tuple(dict.fromkeys(contracts))
    if not unique:
        return _TypeRef("None", empty_reason)
    if len(unique) > 1:
        used.add("alternative")
        return _TypeRef(
            "_AlternativeValue",
            f"{len(unique)} schema scalar alternatives survive; "
            "runtime reads return AlternativeValue",
        )
    contract = unique[0]
    if contract.kind == "text":
        used.add("alternative")
        return _TypeRef(
            "_AlternativeValue",
            "text is a lexical contract with no decoder; runtime reads return AlternativeValue",
        )
    if contract.kind == "atomic":
        return _atomic_ref(plan, contract, used)
    pattern = plan.encoding_patterns.get(contract.pattern_identity)
    if not isinstance(pattern, List):
        used.add("any")
        return _TypeRef(
            "_Any",
            "the whole-list contract has no registered compiled list pattern; "
            "Any avoids an unproven item shape",
        )
    return _list_ref(plan, pattern, used)


def _optional(ref: _TypeRef) -> _TypeRef:
    """Add the absence arm to one singular annotation."""
    if ref.text == "None":
        return ref
    return _TypeRef(f"{ref.text} | None", ref.reason)


def _child_node_ref(
    plan: ProjectionPlan,
    child_route_id: str | None,
    class_names: dict[str, str],
    depth: int | None,
    used: set[str],
) -> _TypeRef:
    """Return the node annotation for one child route reference."""
    if child_route_id is None:
        used.add("any")
        return _TypeRef(
            "_Any",
            "the child field has no projected route; Any avoids inventing a class",
        )
    child = plan.routes.get(child_route_id)
    if child is None:
        used.add("any")
        return _TypeRef(
            "_Any",
            f"child route {child_route_id} is absent from the projection plan; "
            "Any avoids inventing a class",
        )
    if child.generic_reason is not None:
        return _TypeRef(
            "RelaxNGElement",
            f"generic route {child_route_id}: {_comment_text(child.generic_reason)}",
        )
    name = class_names.get(child_route_id)
    if name is None:
        used.add("any")
        depth_label = "all" if depth is None else str(depth)
        return _TypeRef(
            "_Any",
            f"child route {child_route_id} is outside the component selection "
            f"(depth={depth_label}); its generated class is not emitted, so Any "
            "avoids naming an undefined class",
        )
    return _TypeRef(name)


def _class_block(
    plan: ProjectionPlan,
    route: RouteContract,
    cls: type[Any],
    class_names: dict[str, str],
    depth: int | None,
    used: set[str],
) -> _ClassBlock:
    """Build the member model for one stable route."""
    comments = [f"route: {route.route_id}"]
    if any(item.role == "child" for item in route.wildcards):
        comments.append(
            "wildcard children: ordered MutableSequence[RelaxNGElement] via "
            "WILDCARD_CHILDREN; open names never become fixed descriptors."
        )
    if any(item.role == "attribute" for item in route.wildcards):
        comments.append(
            "wildcard attributes: expanded-name lookup only; no fixed descriptors are generated."
        )
    if route.content.mixed:
        comments.append(
            "mixed content: ordered text and child views; no whole-element scalar "
            "descriptor is generated."
        )
    members: list[_Member] = []
    for field in route.attributes.values():
        ref = _scalar_ref(
            plan,
            field.scalar_contracts,
            used,
            empty_reason=(
                "the attribute content has no scalar interpretation; runtime reads return None"
            ),
        )
        if field.cardinality.minimum == 0:
            ref = _optional(ref)
        members.append(_Member(field.alias, ref))
    for field in route.children.values():
        ref = _child_node_ref(plan, field.child_route_id, class_names, depth, used)
        if field.cardinality.repeatable:
            used.add("mutable")
            ref = _TypeRef(f"_MutableSequence[{ref.text}]", ref.reason)
        elif field.cardinality.minimum == 0:
            ref = _optional(ref)
        members.append(_Member(field.alias, ref))
    scalar_field = cls.__dict__.get("_relaxng_scalar_field_")
    if route.content.scalar_contracts:
        if scalar_field is None:
            raise RuntimeError(
                f"stable route {route.route_id} has scalar contracts but its generated "
                "class has no scalar descriptor"
            )
        ref = _scalar_ref(
            plan,
            route.content.scalar_contracts,
            used,
            empty_reason="the route content has no scalar interpretation",
        )
        if _model_allows_empty(plan, route.content.model):
            ref = _optional(ref)
        members.append(_Member(scalar_field, ref))
    elif scalar_field is not None:
        raise RuntimeError(
            f"stable route {route.route_id} has no scalar contracts but its generated "
            "class has a scalar descriptor"
        )
    return _ClassBlock(cls.__name__, route.route_id, tuple(comments), tuple(members))


def _alias_name(expanded_name: str) -> str:
    """Return the deterministic Python spelling for a start element's local name."""
    local = expanded_name.rsplit("}", 1)[-1]
    text = normalize("NFKC", local)
    candidate = "".join(
        character if ("_" + character).isidentifier() else f"_x{ord(character):x}_"
        for character in text
    )
    if not candidate:
        candidate = "_"
    if not candidate.isidentifier():
        candidate = "_" + candidate
    if keyword.iskeyword(candidate):
        candidate += "_"
    return candidate


def _collision_binding(
    candidate: str, class_names: dict[str, str], private_names: frozenset[str]
) -> str | None:
    """Return what a start alias would duplicate, or ``None`` when free."""
    if candidate in class_names.values():
        return f"class {candidate!r}"
    if candidate in private_names:
        return "a private stub alias"
    if candidate in _RESERVED_START_NAMES:
        return "a reserved module binding"
    return None


def _starts(
    plan: ProjectionPlan,
    class_names: dict[str, str],
    selected_routes: tuple[str, ...],
    filtered: bool,
    private_names: frozenset[str],
    type_aliases: dict[str, str],
) -> tuple[_Start, ...]:
    """Resolve starts only to selected stable classes, commenting every other."""
    selected = set(selected_routes)
    used_aliases: set[str] = set()
    starts: list[_Start] = []
    for route_id in plan.roots:
        route = plan.routes[route_id]
        label = _route_label(route)
        if filtered and route_id not in selected:
            starts.append(
                _Start(
                    route_id,
                    label,
                    None,
                    None,
                    f"start route {route_id} ({label}) is outside the component "
                    "selection; no alias is emitted.",
                )
            )
            continue
        if route.generic_reason is not None:
            starts.append(
                _Start(
                    route_id,
                    label,
                    None,
                    None,
                    f"start route {route_id} ({label}) is generic "
                    f"({_comment_text(route.generic_reason)}); its runtime type is "
                    "RelaxNGElement.",
                )
            )
            continue
        if route.expanded_name is None:
            starts.append(
                _Start(
                    route_id,
                    label,
                    None,
                    None,
                    f"start route {route_id} has an open element name class; "
                    "no Python alias is emitted.",
                )
            )
            continue
        candidate = _alias_name(route.expanded_name)
        binding = _collision_binding(candidate, class_names, private_names)
        if binding is not None:
            starts.append(
                _Start(
                    route_id,
                    label,
                    None,
                    None,
                    f"start route {route_id} ({route.expanded_name}): alias "
                    f"{candidate!r} collides with {binding}; no alias is emitted.",
                )
            )
            continue
        if candidate in used_aliases:
            starts.append(
                _Start(
                    route_id,
                    label,
                    None,
                    None,
                    f"start route {route_id} ({route.expanded_name}): alias "
                    f"{candidate!r} repeats an earlier start alias; no alias is emitted.",
                )
            )
            continue
        used_aliases.add(candidate)
        note = (
            f"start element {route.expanded_name}: alias {candidate!r}"
            if candidate != route.expanded_name.rsplit("}", 1)[-1]
            else None
        )
        starts.append(
            _Start(
                route_id,
                label,
                candidate,
                class_names[route_id],
                note,
                type_aliases[route_id],
            )
        )
    return tuple(starts)


def _empty_notice(
    plan: ProjectionPlan,
    class_names: dict[str, str],
    selected_routes: tuple[str, ...],
    filtered: bool,
) -> str | None:
    """Explain a selection that emitted no class blocks."""
    if filtered and not selected_routes:
        return (
            "The component selection contains no projection routes; "
            "no classes or aliases are emitted."
        )
    if class_names:
        return None
    if not plan.routes:
        return "The projection plan has no routes; no classes or aliases are emitted."
    if filtered:
        return (
            "The component selection contains no stable projection routes; "
            "only generic-route comments are emitted."
        )
    return (
        "No stable projection routes were planned; every route uses the "
        "generic RelaxNGElement view."
    )


def _fingerprint(schema: RelaxNGSchema) -> str:
    """Return the path-independent compiled grammar identity for the header."""
    return schema._grammar_fingerprint


def _full_header(schema: RelaxNGSchema, example: str | None, *, classes: bool) -> list[str]:
    """Return the provenance and consumption header for full mode."""
    lines = [
        f"# Generated by pyxsd {__version__} from RELAX NG grammar {_fingerprint(schema)}.",
        "# Do not edit by hand.",
        "#",
        "# This file is a typing surface for the classes a compiled RELAX NG",
        "# schema creates at runtime; the classes live in",
        "# ``schema.projection_classes``. Save this stub next to your code and",
        "# import it for type checking:",
        "#",
    ]
    if example is not None:
        lines.extend(
            [
                "#     if TYPE_CHECKING:",
                f"#         from schema_stubs import {example}",
                f"#     root = cast({example}, document.root)",
            ]
        )
    elif classes:
        lines.extend(
            [
                "# This selection has no stable start alias; the classes below",
                "# are reachable views, not a document-root cast.",
            ]
        )
    else:
        lines.extend(
            [
                "# This selection emits no projection classes; the comments below",
                "# describe its generic routes and start patterns instead.",
            ]
        )
    lines.extend(
        [
            "#",
            "# Semantics: annotations model valid typed documents. An absent",
            "# optional member reads as None, as does a scalar descriptor when",
            "# the element has no leading character data. A repeated child reads",
            "# as a MutableSequence, including when empty. A schema-ambiguous or",
            "# lexical-only scalar reads as AlternativeValue; a generic child",
            "# reads as RelaxNGElement. Constructs this stub cannot type",
            "# truthfully are Any with a comment.",
        ]
    )
    return lines


def _uses_token(name: str, text: str) -> bool:
    """Return whether *text* uses *name* as a whole identifier."""
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) is not None


def _full_annotation(text: str, names: _FullNames, class_names: dict[str, str]) -> str:
    """Rewrite one canonical annotation into collision-proof full spellings."""
    text = re.sub(
        r"\b(bool|bytes|float|int|list|str|tuple|type)\b",
        lambda match: f"{names.builtins}.{match.group(1)}",
        text,
    )
    text = re.sub(
        r"_xsd\.([A-Za-z_][A-Za-z0-9_]*)",
        lambda match: f"{names.xsd}.{match.group(1)}",
        text,
    )
    for full, private in (
        ("_Any", names.any),
        ("_AlternativeValue", names.alternative),
        ("_MutableSequence", names.mutable),
        ("RelaxNGElement", names.element),
    ):
        text = re.sub(rf"\b{re.escape(full)}\b", private, text)
    class_aliases = {
        class_names[route_id]: names.class_aliases[route_id] for route_id in names.class_order
    }
    for class_name in sorted(class_aliases, key=len, reverse=True):
        text = re.sub(
            rf"(?<![\w.]){re.escape(class_name)}(?!\w)",
            class_aliases[class_name],
            text,
        )
    return text


def _full_imports(body: str, names: _FullNames, *, classes: bool) -> list[str]:
    """Return the import block the rendered full body actually needs."""
    stdlib: list[str] = []
    if re.search(rf"{re.escape(names.builtins)}\.", body):
        stdlib.append(f"import builtins as {names.builtins}")
    if _uses_token(names.mutable, body):
        stdlib.append(f"from collections.abc import MutableSequence as {names.mutable}")
    if _uses_token(names.any, body):
        stdlib.append(f"from typing import Any as {names.any}")
    modules: list[str] = []
    if re.search(rf"{re.escape(names.xsd)}\.", body):
        modules.append(f"from pyxsd import xsd_data_types as {names.xsd}")
    if _uses_token(names.alternative, body):
        modules.append(f"from pyxsd.relaxng import AlternativeValue as {names.alternative}")
    if classes:
        modules.append(f"from pyxsd.relaxng import RelaxNGElement as {names.element}")
    lines = [*stdlib]
    if stdlib and modules:
        lines.append("")
    lines.extend(modules)
    return lines


def _render_full(
    schema: RelaxNGSchema,
    blocks: tuple[_ClassBlock, ...],
    generic_routes: list[_GenericRoute],
    starts: tuple[_Start, ...],
    notice: str | None,
    names: _FullNames,
    class_names: dict[str, str],
) -> str:
    """Assemble the full ``.pyi`` document."""
    example = None
    for start in starts:
        if start.type_name is not None:
            example = start.type_name
            break
    class_blocks: list[str] = []
    for block in blocks:
        lines = [f"    # {comment}" for comment in block.comments]
        for member in block.members:
            if member.annotation.reason is not None:
                lines.append(f"    # {member.annotation.reason}")
            annotation = _full_annotation(member.annotation.text, names, class_names)
            lines.append(f"    {member.name}: {annotation}")
        if not block.members:
            lines.append("    ...")
        class_blocks.append(f"class {block.name}({names.element}):\n" + "\n".join(lines))
    start_lines: list[str] = []
    if starts:
        start_lines.append("# Starts:")
        for start in starts:
            if start.alias is None:
                start_lines.append(f"# {start.comment}")
            else:
                if start.comment is not None:
                    start_lines.append(f"# {start.comment}")
                assert start.type_alias is not None
                start_lines.append(f"{start.alias}: {names.builtins}.type[{start.type_alias}]")
    else:
        start_lines.append("# Starts: none (the compiled start pattern declares no element route)")
    type_aliases = [
        f"{names.class_aliases[route_id]} = {class_names[route_id]}"
        for route_id in names.class_order
    ]
    body = "\n".join([*class_blocks, *type_aliases, *start_lines])
    sections = ["\n".join(_full_header(schema, example, classes=bool(blocks)))]
    import_lines = ["from __future__ import annotations"]
    imports = _full_imports(body, names, classes=bool(blocks))
    if imports:
        import_lines.append("")
        import_lines.extend(imports)
    sections.append("\n".join(import_lines))
    if notice is not None:
        sections.append(f"# {notice}")
    if class_blocks:
        sections.append("\n\n".join(class_blocks))
    if type_aliases:
        sections.append("\n".join(type_aliases))
    if generic_routes:
        lines = ["# Generic routes (no generated class; runtime type RelaxNGElement):"]
        for route in generic_routes:
            lines.append(f"#   {route.route_id} ({route.label}): {route.reason}")
        sections.append("\n".join(lines))
    sections.append("\n".join(start_lines))
    return "\n\n".join(sections) + "\n"


def _compact_symbols(
    class_names: set[str], used: set[str], blocks: tuple[_ClassBlock, ...]
) -> dict[tuple[str, str], str]:
    """Choose collision-free compact spellings for every used alias."""
    xsd_names = sorted(
        {
            match
            for block in blocks
            for member in block.members
            for match in re.findall(r"_xsd\.([A-Za-z_][A-Za-z0-9_]*)", member.annotation.text)
        }
    )
    keys: list[tuple[str, str]] = [
        (key, base) for key, base, _source in _COMPACT_FIXED if key in used
    ]
    keys.extend(("xsd", name) for name in xsd_names)
    taken = set(class_names)
    chosen: dict[tuple[str, str], str] = {}
    for key, base in keys:
        candidate = base
        while candidate in taken:
            candidate = "_" + candidate
        chosen[key, base] = candidate
        taken.add(candidate)
    return chosen


def _compact_legend(used: set[str], symbols: dict[tuple[str, str], str]) -> str:
    """Return the compact alias legend in deterministic order."""
    parts: list[str] = []
    for key, _base, source in _COMPACT_FIXED:
        if key in used:
            parts.append(f"{symbols[key, _base]}={source}")
    for (key, name), spelling in symbols.items():
        if key == "xsd":
            parts.append(f"{spelling}=pyxsd.xsd_data_types.{name}")
    return ", ".join(parts)


def _compact_annotation(text: str, symbols: dict[tuple[str, str], str]) -> str:
    """Rewrite one full annotation into its compact alias spellings."""
    text = re.sub(
        r"_xsd\.([A-Za-z_][A-Za-z0-9_]*)",
        lambda match: symbols[("xsd", match.group(1))],
        text,
    )
    for key, base, full in (
        ("any", "Any", "_Any"),
        ("alternative", "Alt", "_AlternativeValue"),
        ("mutable", "Seq", "_MutableSequence"),
        ("element", "RNG", "RelaxNGElement"),
    ):
        text = re.sub(rf"\b{re.escape(full)}\b", symbols.get((key, base), full), text)
    return text


def _compact_starts(starts: tuple[_Start, ...]) -> str:
    """Return the trailing start line, keeping every start route visible."""
    if not starts:
        return "# starts: none (the compiled start pattern declares no element route)"
    parts: list[str] = []
    for start in starts:
        if start.alias is not None:
            text = f"{start.alias}: type[{start.type_name}]"
            if start.comment is not None:
                text += f" ({start.comment})"
            parts.append(text)
            continue
        reason = f" ({start.comment})" if start.comment is not None else ""
        parts.append(f"{start.route_id}: ?{reason}")
    return "# starts: " + ", ".join(parts)


def _render_compact(
    schema: RelaxNGSchema,
    blocks: tuple[_ClassBlock, ...],
    generic_routes: list[_GenericRoute],
    starts: tuple[_Start, ...],
    notice: str | None,
    used: set[str],
) -> str:
    """Assemble the compact agent view."""
    if blocks:
        used.add("element")
    class_names = {block.name for block in blocks}
    symbols = _compact_symbols(class_names, used, blocks)
    sections = [
        "\n".join(
            [
                f"# Generated by pyxsd {__version__} from RELAX NG grammar "
                f"{_fingerprint(schema)}; compact view, not Python source.",
                f"# aliases: {_compact_legend(used, symbols) or 'none'}",
            ]
        )
    ]
    if notice is not None:
        sections.append(f"# {notice}")
    class_blocks: list[str] = []
    for block in blocks:
        lines = [f"class {block.name}({symbols[('element', 'RNG')]}):"]
        lines.extend(f"  # {comment}" for comment in block.comments)
        for member in block.members:
            annotation = _compact_annotation(member.annotation.text, symbols)
            line = f"  {member.name}: {annotation}"
            if member.annotation.reason is not None:
                line += f"  # {member.annotation.reason}"
            lines.append(line)
        class_blocks.append("\n".join(lines))
    if class_blocks:
        sections.append("\n\n".join(class_blocks))
    if generic_routes:
        sections.append(
            "# generic routes: "
            + "; ".join(
                f"{route.route_id} ({route.label}): {route.reason}" for route in generic_routes
            )
        )
    sections.append(_compact_starts(starts))
    return "\n\n".join(sections) + "\n"


def _render_unavailable(schema: RelaxNGSchema, *, compact: bool) -> str:
    """Return deterministic text for a schema whose report has errors."""
    fingerprint = _fingerprint(schema)
    if compact:
        return (
            "\n".join(
                [
                    f"# Generated by pyxsd {__version__} from RELAX NG grammar "
                    f"{fingerprint}; compact view, not Python source.",
                    "# aliases: none",
                    "",
                    "# The schema report has errors; no projection classes or starts are described.",
                ]
            )
            + "\n"
        )
    return (
        "\n".join(
            [
                f"# Generated by pyxsd {__version__} from RELAX NG grammar {fingerprint}.",
                "# Do not edit by hand.",
                "#",
                "# The schema report has errors; no projection classes, descriptors,",
                "# or start aliases are emitted. Fix the schema report and recompile.",
                "",
                "from __future__ import annotations",
            ]
        )
        + "\n"
    )
