"""Canonical comparison of a finite RELAX NG structural projection.

The projection erases reference indirection, generated definition names, source
evidence and pool sharing. Choice and interleave are associative/commutative;
group is associative but keeps non-attribute order. Attribute-only group terms
commute, not arbitrary child/scalar terms. Interleave multiplicity is retained.

Pattern/name/datatype IDs are content addresses: replacement is removal plus
addition. Starts, unambiguous named definitions and projection routes have
semantic anchors and report changed fields. Ambiguous same-name definitions
and routes use content discriminators, never traversal or source ordinals.

Recursive pattern identities use the finite bisimulation quotient of the
labelled normalized graph. Partition refinement only splits classes and stops
after at most the number of reachable nodes; the rooted quotient is serialized
in canonical class order. This is structural bisimulation, not a decision
procedure for RELAX NG language equivalence or inclusion.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from hashlib import sha256
from typing import cast

from pyxsd.data_conversion.contracts import JsonValue
from pyxsd.relaxng.contracts import ScalarContract, scalar_contract
from pyxsd.relaxng.datatypes import ScalarDatatype
from pyxsd.relaxng.names import AnyName, ExceptName, Name, NameChoice, NameClass, NsName
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Data,
    DataExcept,
    Element,
    Group,
    Interleave,
    List,
    OneOrMore,
    Pattern,
    Ref,
    Value,
)
from pyxsd.relaxng.projection import ContentContract, ProjectionPlan, RouteContract
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.simplify import Definition

__all__ = ["compare_relaxng_structure"]

_STATEMENT = (
    "This compares a canonical semantic structural projection, not serialized schema bytes. "
    "An empty diff means no difference in this projection; it proves neither language "
    "equivalence nor inclusion, and makes no universal compatibility claim."
)
_CATEGORIES = (
    "starts",
    "definitions",
    "patterns",
    "name_classes",
    "datatypes",
    "dependencies",
    "projections",
)


def _text(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return sha256(_text(value).encode("ascii")).hexdigest()


def _unique(values: list[JsonValue]) -> list[JsonValue]:
    return [value for _, value in sorted({_text(value): value for value in values}.items())]


def _name(name: NameClass) -> JsonValue:
    if isinstance(name, NameChoice):
        pending = list(name.alternatives)
        leaves: list[JsonValue] = []
        while pending:
            part = pending.pop()
            if isinstance(part, NameChoice):
                pending.extend(part.alternatives)
            else:
                leaves.append(_name(part))
        return ["choice", _unique(leaves)]
    if isinstance(name, ExceptName):
        return ["except", _name(name.base), _name(name.excluded)]
    if isinstance(name, Name):
        return ["name", name.uri, name.local]
    if isinstance(name, NsName):
        return ["nsName", name.uri]
    assert isinstance(name, AnyName)
    return ["anyName"]


def _target(node: Pattern) -> Pattern:
    # Checked grammars bind refs directly to elements, including recursive ones.
    return node.target if isinstance(node, Ref) else node


def _children(node: Pattern) -> tuple[Pattern, ...]:
    if isinstance(node, (Choice, Group, Interleave)):
        return node.first, node.second
    if isinstance(node, (Attribute, Element, List, OneOrMore)):
        return (node.pattern,)
    if isinstance(node, DataExcept):
        return (node.excluded,)
    return ()


def _attribute_only(node: Pattern) -> bool:
    pending = [node]
    while pending:
        part = _target(pending.pop())
        if isinstance(part, Attribute):
            continue
        if isinstance(part, (Choice, Group, Interleave, OneOrMore)):
            pending.extend(_children(part))
        elif type(part).__name__ not in {"Empty", "NotAllowed"}:
            return False
    return True


def _operands(node: Pattern) -> tuple[list[Pattern], list[Pattern]]:
    ordered: list[Pattern] = []
    unordered: list[Pattern] = []
    pending = list(reversed(_children(node)))
    while pending:
        child = _target(pending.pop())
        if isinstance(node, (Choice, Group, Interleave)) and type(child) is type(node):
            pending.extend(reversed(_children(child)))
        elif isinstance(node, (Choice, Interleave)) or (
            isinstance(node, Group) and _attribute_only(child)
        ):
            unordered.append(child)
        else:
            ordered.append(child)
    return ordered, unordered


class _Graph:
    """A finite normalized graph; live IDs are only private lookup keys."""

    def __init__(self) -> None:
        self.nodes: dict[int, tuple[dict[str, JsonValue], list[int], list[int]]] = {}
        self.identities: dict[int, str] = {}

    def identity(self, root: int) -> str:
        if root in self.identities:
            return self.identities[root]
        reachable: set[int] = set()
        pending = [root]
        while pending:
            key = pending.pop()
            if key not in reachable:
                reachable.add(key)
                _, ordered, unordered = self.nodes[key]
                pending.extend(ordered + unordered)
        heads = {key: _text(self.nodes[key][0]) for key in reachable}
        labels = {head: i for i, head in enumerate(sorted(set(heads.values())))}
        colors = {key: labels[heads[key]] for key in reachable}
        while True:
            signatures = {}
            for key in reachable:
                head, ordered, unordered = self.nodes[key]
                bag = [colors[child] for child in unordered]
                if head["kind"] == "Choice":
                    bag = list(set(bag))
                signatures[key] = (
                    colors[key],
                    heads[key],
                    tuple(colors[child] for child in ordered),
                    tuple(sorted(bag)),
                )
            ranks = {signature: i for i, signature in enumerate(sorted(set(signatures.values())))}
            refined = {key: ranks[signatures[key]] for key in reachable}
            stable = len(set(refined.values())) == len(set(colors.values()))
            colors = refined
            if stable:
                break
        quotient = {}
        for key in reachable:
            head, ordered, unordered = self.nodes[key]
            bag = [colors[child] for child in unordered]
            if head["kind"] == "Choice":
                bag = list(set(bag))
            quotient[colors[key]] = [head, [colors[c] for c in ordered], sorted(bag)]
        result = _digest([colors[root], [quotient[c] for c in sorted(quotient)]])
        self.identities[root] = result
        return result


class _Patterns:
    def __init__(self, schema: RelaxNGSchema) -> None:
        self.graph = _Graph()
        self.patterns: dict[int, Pattern] = {}
        pending: list[Pattern] = [
            schema.grammar.start,
            *(d.pattern for d in schema.grammar.definitions.values()),
        ]
        while pending:
            node = _target(pending.pop())
            key = id(node)
            if key in self.patterns:
                continue
            self.patterns[key] = node
            head: dict[str, JsonValue] = {"kind": type(node).__name__}
            if isinstance(node, (Attribute, Element)):
                head["name_class"] = _name(node.name_class)
            if isinstance(node, (Data, DataExcept, Value)):
                contract = scalar_contract(Data(node.datatype))
                head["datatype"] = {
                    "library": contract.library_uri,
                    "type": contract.type_name,
                    "parameters": [[p.name, p.value] for p in contract.parameters],
                    "white_space": getattr(node.datatype, "white_space", None),
                    # Built-in QName context is used only to resolve a value;
                    # data uses the instance's namespace bindings instead.
                    "context": []
                    if isinstance(node.datatype, ScalarDatatype)
                    else [list(p) for p in getattr(node.datatype, "semantic_context", ())],
                }
            if isinstance(node, Value):
                datatype = node.datatype
                if isinstance(datatype, ScalarDatatype) and datatype.type_name in {
                    "QName",
                    "NOTATION",
                }:
                    value = datatype.decode(node.lexical, datatype.context)
                    head["value"] = value.to_expanded_name(strict=True)
                else:
                    head["value"] = node.lexical
            ordered, unordered = _operands(node)
            self.graph.nodes[key] = (head, [id(c) for c in ordered], [id(c) for c in unordered])
            pending.extend(ordered + unordered)

    def identity(self, node: Pattern) -> str:
        return "pattern:" + self.graph.identity(id(_target(node)))


def _anchors(
    prefix: str, names: Mapping[str, JsonValue], contents: Mapping[str, JsonValue]
) -> dict[str, str]:
    counts = Counter(_text(name) for name in names.values())
    return {
        key: prefix
        + _digest(name)
        + (":" + _digest(contents[key]) if counts[_text(name)] > 1 else "")
        for key, name in names.items()
    }


def _projection_value(
    value: object,
    patterns: _Patterns,
    plan: ProjectionPlan,
    routes: dict[str, str],
    assignments: dict[int | None, str],
) -> JsonValue:
    if isinstance(value, NameClass):
        return _name(value)
    if isinstance(value, ScalarContract):
        encoding = _target(plan.encoding_patterns[value.pattern_identity])
        return {
            "kind": value.kind,
            "pattern": patterns.identity(encoding),
            "contract": patterns.graph.nodes[id(encoding)][0],
        }
    if isinstance(value, ContentContract):
        data = {
            f.name: _projection_value(getattr(value, f.name), patterns, plan, routes, assignments)
            for f in fields(value)
            if f.name not in {"operands", "assignment_id"}
        }
        if value.kind == "child":
            # Replace the planner-local ordinal with the original assignment's
            # canonical pattern, preserving same-name ordering correlations.
            data["assignment"] = assignments[value.assignment_id]
        pending = list(reversed(value.operands))
        ordered: list[JsonValue] = []
        unordered: list[JsonValue] = []
        while pending:
            operand = pending.pop()
            if value.kind in {"choice", "group", "interleave"} and operand.kind == value.kind:
                pending.extend(reversed(operand.operands))
                continue
            item = _projection_value(operand, patterns, plan, routes, assignments)
            attribute = _content_attribute_only(operand)
            if value.kind in {"choice", "interleave"} or (value.kind == "group" and attribute):
                unordered.append(item)
            else:
                ordered.append(item)
        data["ordered"] = ordered
        data["unordered"] = (
            _unique(unordered) if value.kind == "choice" else sorted(unordered, key=_text)
        )
        # A one-operand choice/group/interleave is its operand.  Equivalent
        # generated definitions can merge into such a wrapper in one schema and
        # not another; the wrapper itself carries no semantics.
        operands = cast(list[JsonValue], data["ordered"]) + cast(list[JsonValue], data["unordered"])
        if (
            value.kind in {"choice", "group", "interleave"}
            and len(operands) == 1
            and not data["fields"]
            and not data["scalar_contracts"]
            and data["name_class"] is None
            and data["generic_reason"] is None
        ):
            return operands[0]
        return data
    if isinstance(value, RouteContract):
        route_data: dict[str, JsonValue] = {
            f.name: _projection_value(getattr(value, f.name), patterns, plan, routes, assignments)
            for f in fields(value)
            if f.name not in {"route_id", "occurrence", "origins", "patterns", "constraints"}
        }
        # Route constraints are a derived view of the canonical content model.
        # Recompute them after assignment/definition merging instead of
        # exposing planner pool sharing as a semantic difference.
        model = cast(
            dict[str, JsonValue], cast(dict[str, JsonValue], route_data["content"])["model"]
        )
        model_fields = cast(list[JsonValue], model["fields"])
        route_data["constraints"] = (
            [model]
            if model["kind"] in {"choice", "group", "interleave", "repeat"} or len(model_fields) > 1
            else []
        )
        return route_data
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: (
                routes[getattr(value, f.name)]
                if f.name == "child_route_id" and getattr(value, f.name) is not None
                else _projection_value(getattr(value, f.name), patterns, plan, routes, assignments)
            )
            for f in fields(value)
            if f.name not in {"route_id", "occurrence", "origins", "patterns"}
        }
    if isinstance(value, Mapping):
        # Assignment ordinals carry no semantics; retain their name/content
        # pairs so same-name ordering correlations are not erased.
        if all(isinstance(key, int) for key in value):
            return _unique(
                [_projection_value(v, patterns, plan, routes, assignments) for v in value.values()]
            )
        return {
            str(k): _projection_value(v, patterns, plan, routes, assignments)
            for k, v in value.items()
        }
    if isinstance(value, tuple):
        return _unique([_projection_value(v, patterns, plan, routes, assignments) for v in value])
    return cast(JsonValue, value)


def _content_attribute_only(content: ContentContract) -> bool:
    return content.kind in {"attribute", "empty", "notAllowed"} or (
        content.kind in {"choice", "group", "interleave", "repeat"}
        and all(_content_attribute_only(c) for c in content.operands)
    )


def _inventory(schema: RelaxNGSchema) -> dict[str, dict[str, JsonValue]]:
    patterns = _Patterns(schema)
    result: dict[str, dict[str, JsonValue]] = {}
    result["start"] = {"category": "starts", "pattern": patterns.identity(schema.grammar.start)}
    result["datatype-registry"] = {
        "category": "datatypes",
        "fingerprint": schema.datatype_fingerprint,
    }
    # Generated definitions are a set: interning may merge identical
    # same-name alternatives in one schema and not another.  Ordered child
    # occurrences live in the pattern graph, not in this definition set.
    definitions: dict[str, Definition] = {}
    signatures: set[tuple[str, str]] = set()
    for key, definition in schema.grammar.definitions.items():
        signature = (
            _text(_name(definition.pattern.name_class)),
            patterns.identity(definition.pattern),
        )
        if signature not in signatures:
            signatures.add(signature)
            definitions[key] = definition
    definition_keys = _anchors(
        "definition:",
        {k: _name(d.pattern.name_class) for k, d in definitions.items()},
        {k: patterns.identity(d.pattern) for k, d in definitions.items()},
    )
    for key, definition in definitions.items():
        result[definition_keys[key]] = {
            "category": "definitions",
            "pattern": patterns.identity(definition.pattern),
        }
    for node_id, node in patterns.patterns.items():
        head, ordered, unordered = patterns.graph.nodes[node_id]
        alternatives = [patterns.identity(patterns.patterns[c]) for c in unordered]
        result[patterns.identity(node)] = {
            "category": "patterns",
            **head,
            "ordered": [patterns.identity(patterns.patterns[c]) for c in ordered],
            "unordered": cast(
                JsonValue, sorted(set(alternatives) if isinstance(node, Choice) else alternatives)
            ),
        }
        for field, category, prefix in (
            ("name_class", "name_classes", "name-class:"),
            ("datatype", "datatypes", "datatype:"),
        ):
            if field in head:
                result[prefix + _digest(head[field])] = {"category": category, field: head[field]}
    # Resource identities describe their resolved contribution, not authored
    # bytes. Source IDs are used only to join origins to closure edges.
    resources: dict[str, str] = {}
    for dependency in schema.grammar.dependencies:
        contribution = sorted(
            {
                patterns.identity(node)
                for node in patterns.patterns.values()
                if any(
                    o.source_id == dependency.source_id for o in schema.grammar.graph.origins(node)
                )
            }
        )
        resource = _digest([dependency.purpose, contribution])
        resources[dependency.canonical_id] = resource
    for dependency in schema.grammar.dependencies:
        for reference in dependency.references:
            edge: dict[str, JsonValue] = {
                "category": "dependencies",
                "purpose": dependency.purpose,
                "from": resources[reference.containing_id],
                "to": resources[dependency.canonical_id],
            }
            result["dependency:" + _digest(edge)] = edge
    plan = schema.projection
    route_keys = _anchors(
        "projection:",
        {k: _name(r.name_class) for k, r in plan.routes.items()},
        {
            k: cast(JsonValue, sorted({patterns.identity(p) for p in r.patterns}))
            for k, r in plan.routes.items()
        },
    )
    assignments: dict[int | None, str] = {
        ordinal: patterns.identity(pattern)
        for route in plan.routes.values()
        for ordinal, pattern in zip(route.assignments, route.patterns, strict=True)
    }
    for key, route in plan.routes.items():
        data = cast(
            dict[str, JsonValue], _projection_value(route, patterns, plan, route_keys, assignments)
        )
        result[route_keys[key]] = {
            "category": "projections",
            **data,
            "root": key in plan.roots,
        }
    return result


def compare_relaxng_structure(
    old: RelaxNGSchema,
    new: RelaxNGSchema,
    *,
    detail_limit: int | None = None,
) -> dict[str, JsonValue]:
    """Compare valid schemas, bounding field details but never counts or IDs.

    ``detail_limit`` must be non-negative. The result includes a category
    breakdown, complete added/removed/changed ID lists and field-level details.
    A changed anchored component may refer to added/removed content addresses.
    Provenance, serialized RNG and raw resource hashes never enter the diff.
    """
    if detail_limit is not None and detail_limit < 0:
        raise ValueError("detail_limit must be non-negative")
    old.require_valid()
    new.require_valid()
    before, after = _inventory(old), _inventory(new)
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    details: list[JsonValue] = [
        {
            "id": key,
            "changes": [
                {"field": field, "old": before[key].get(field), "new": after[key].get(field)}
                for field in sorted(before[key].keys() | after[key].keys())
                if before[key].get(field) != after[key].get(field)
            ],
        }
        for key in changed[:detail_limit]
    ]
    return {
        "status": "compared",
        "statement": _STATEMENT,
        "added": list(added),
        "removed": list(removed),
        "changed": list(changed),
        "changed_details": details,
        "changed_details_truncated": len(details) < len(changed),
        "totals": {"added": len(added), "removed": len(removed), "changed": len(changed)},
        "categories": {
            category: {
                "added": [key for key in added if after[key]["category"] == category],
                "removed": [key for key in removed if before[key]["category"] == category],
                "changed": [key for key in changed if after[key]["category"] == category],
            }
            for category in _CATEGORIES
        },
    }
