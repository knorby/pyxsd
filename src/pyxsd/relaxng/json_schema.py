"""Draft 2020-12 JSON Schema for the RELAX NG ``pyxsd-json`` profile.

The projection plan, not an instance, selects every shape here.  A route body
describes the exact root-preserving, attribute/keyed, ordered-array, or tagged
scalar envelope :func:`~pyxsd.relaxng.json_profile.export_profile` emits.  Route
definitions are allocated in sorted route-ID order and filled through shells, so
recursive resources become ``$ref`` cycles instead of unbounded expansions.

This is a structural data contract, never a RELAX NG validator.  Grammar
validity, name-class membership, datatype lexical spaces and namespaces,
candidate identity and completeness, and any ordering a conservative array
cannot express stay native; they are listed under
``x-pyxsd-validation.unvalidated``.  Tagged scalar metadata is shape-only: no
grammar fingerprint, candidate ID, or occurrence-specific candidate list is
frozen into the generated schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pyxsd.data_conversion.contracts import (
    Fidelity,
    FormatName,
    validate_fidelity,
    validate_format,
)
from pyxsd.data_conversion.names import expanded_to_eqname
from pyxsd.relaxng.contracts import (
    ScalarContract,
    list_items_decodable,
    list_shape,
    scalar_contract,
)
from pyxsd.relaxng.conversion import RelaxNGUnsupportedAdapterError
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    XSD_LIBRARY,
    datatype_supports_decode,
)
from pyxsd.relaxng.json_profile import _select_expanded_root
from pyxsd.relaxng.names import AnyName, ExceptName, Name, NameChoice, NameClass, NsName
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
    Value,
)
from pyxsd.relaxng.projection import (
    Cardinality,
    ContentContract,
    FieldContract,
    ProjectionPlan,
    RouteContract,
    WildcardContract,
)

if TYPE_CHECKING:
    from pyxsd.relaxng.schema import RelaxNGSchema

__all__ = ["build_relaxng_json_schema"]

#: The JSON Schema dialect every envelope advertises.
_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

#: The profile's root and attribute EQNames: ``Q{uri}local`` or a bare local.
_EQNAME_PATTERN = "^(?:Q\\{[\\s\\S]*\\})?[^\\s:{}#@/]+$"

#: Canonical fixed-point decimal strings (``f"{value:f}"``, never a float).
_DECIMAL_PATTERN = "^-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?$"

#: Profile attribute keys: ``@EQName``, without namespace declarations.
_ATTRIBUTE_KEY_PATTERN = (
    "^@(?!xmlns$)(?!Q\\{http://www\\.w3\\.org/2000/xmlns/\\})(?:Q\\{[\\s\\S]*\\})?[^\\s:{}#@/]+$"
)

_CONTENT_KEY = "#content"
_TAG_KEY = "#value"

#: XSD 1.0 atomic families whose decoded values share one JSON representation.
_XSD_INTEGER_TYPES = frozenset(
    {
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
_XSD_FLOAT_TYPES = frozenset({"double", "float"})
_XSD_QNAME_TYPES = frozenset({"NOTATION", "QName"})
#: XSD atomics whose decoded value is an array of lexical tokens.
_XSD_LIST_VALUED_TYPES = frozenset({"ENTITIES", "IDREFS", "NMTOKENS"})

#: Reasons every profile envelope carries, independent of the selected root.
_BASE_UNVALIDATED = (
    "relaxng-datatype-context",
    "relaxng-grammar-validity",
    "relaxng-name-class",
    "relaxng-scalar-candidates",
)

_Layout = Literal["empty", "scalar", "repeated", "record", "sequence", "mixed"]

#: A JSON Schema fragment.
_Schema = dict[str, Any]

#: One plain list token plan and its minimum required token count.
_TokenPlan = tuple[Literal["fixed", "list"], tuple[Pattern, ...], int]

#: The shared generic child/route envelope definition key.
_GENERIC_KEY = "generic-route"

#: The recursive sound union used for opaque custom scalar values.
_OPAQUE_KEY = "opaque-scalar"


def build_relaxng_json_schema(
    schema: RelaxNGSchema,
    *,
    format: FormatName = "pyxsd-json",
    element: str | None = None,
    fidelity: Fidelity = "strict",
    typed: bool = True,
    always_list: bool = False,
) -> dict[str, object]:
    """Return the Draft 2020-12 schema for this schema's profile data contract.

    ``format="pyxsd-json"`` is the RELAX NG profile described here; the other
    four formats raise :class:`~pyxsd.relaxng.conversion.RelaxNGUnsupportedAdapterError`
    until their envelopes land.  ``element`` accepts the same local, Clark, and
    EQName root spellings as :meth:`RelaxNGSchema.from_data` and narrows the
    start name class without inventing a global declaration.  ``typed`` and
    ``always_list`` are native-only options and must keep their defaults, like
    the shared data-export and XSD JSON Schema rules.  ``fidelity`` is accepted
    for interface parity; both modes describe the same sound structure.
    """
    requested_format = validate_format(format)
    validate_fidelity(fidelity)
    if requested_format != "pyxsd":
        if typed is not True:
            raise ValueError("typed is a native-only JSON Schema option")
        if always_list is not False:
            raise ValueError("always_list is a native-only JSON Schema option")
    if requested_format != "pyxsd-json":
        raise RelaxNGUnsupportedAdapterError(requested_format)

    return _RelaxNGJSONSchemaBuilder(schema.projection).build(element)


class _RelaxNGJSONSchemaBuilder:
    """One finite projection of a compiled plan into a JSON Schema envelope."""

    def __init__(self, plan: ProjectionPlan) -> None:
        self.plan = plan
        self.defs: dict[str, _Schema] = {}
        self.unvalidated: set[str] = set(_BASE_UNVALIDATED)
        # Sorted IDs make both keys and definition insertion order independent
        # of planner insertion order, source paths, and process hash seeds.
        self.keys = {
            route_id: f"route-{index}"
            for index, route_id in enumerate(sorted(plan.routes), start=1)
        }
        self.layouts: dict[str, _Layout] = {}
        self._generic_shell: _Schema | None = None
        self._opaque_shell: _Schema | None = None

    # ------------------------------------------------------------------
    # Entry point and root selection
    # ------------------------------------------------------------------

    def build(self, element: str | None) -> dict[str, object]:
        """Build the envelope around the selected root alternatives."""
        root = self._selected_root(element) if element is not None else self._all_roots()
        self._emit_defs()
        envelope: dict[str, object] = {
            "$schema": _SCHEMA_URI,
            "x-pyxsd-format": "pyxsd-json",
            "x-pyxsd-profile": "relaxng",
            "x-pyxsd-profile-version": 1,
            **root,
            "x-pyxsd-validation": {
                "level": "structural",
                "unvalidated": sorted(self.unvalidated),
            },
        }
        if self.defs:
            envelope["$defs"] = self.defs
        return envelope

    def _selected_root(self, element: str) -> _Schema:
        """One exact root object after the exporter's admission check."""
        expanded = _select_expanded_root(self.plan, element)
        route_id = self._root_route_id(expanded)
        return self._root_object(expanded_to_eqname(expanded), route_id)

    def _root_route_id(self, expanded: str) -> str:
        uri, local = _split_expanded(expanded)
        for route_id in self.plan.roots:
            if self.plan.routes[route_id].name_class.contains(uri, local):
                return route_id
        raise AssertionError("root selection escaped the start name class")

    def _all_roots(self) -> _Schema:
        """Every admitted start name, keeping open classes symbolic."""
        alternatives: list[_Schema] = []
        named: list[tuple[str, str]] = []
        open_routes: list[str] = []
        for route_id in self.plan.roots:
            name_class = self.plan.routes[route_id].name_class
            names = _admitted_names(name_class)
            if names is None:
                open_routes.append(route_id)
            else:
                named.extend((expanded_to_eqname(name), route_id) for name in names)
        for eqname, route_id in sorted(named):
            alternatives.append(self._root_object(eqname, route_id))
        excluded = [eqname for eqname, _ in sorted(named)]
        for route_id in sorted(open_routes):
            alternatives.append(self._open_root(route_id, excluded))
        if not alternatives:
            # No reachable document exists, so no value satisfies the contract.
            return {"not": {}}
        return _any_of(alternatives)

    def _root_object(self, eqname: str, route_id: str) -> _Schema:
        """The single-entry object for one admitted root name."""
        return {
            "type": "object",
            "properties": {eqname: self._route_ref(route_id)},
            "required": [eqname],
            "additionalProperties": False,
            "minProperties": 1,
            "maxProperties": 1,
            "x-pyxsd-qname": eqname,
        }

    def _open_root(self, route_id: str, excluded: list[str]) -> _Schema:
        """A symbolic root object for an infinite start name class."""
        name_class = self.plan.routes[route_id].name_class
        names: _Schema = {
            "type": "string",
            "pattern": _EQNAME_PATTERN,
            "x-pyxsd-wildcard": True,
        }
        omitted = sorted({*excluded, *_excluded_eqnames(name_class)})
        if omitted:
            names["not"] = {"enum": omitted}
        self.unvalidated.add("relaxng-wildcard-names")
        return {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 1,
            "propertyNames": names,
            "additionalProperties": self._route_ref(route_id),
            "x-pyxsd-wildcard": True,
            "x-pyxsd-name-class": _name_class_metadata(name_class),
        }

    # ------------------------------------------------------------------
    # Definitions and route layouts
    # ------------------------------------------------------------------

    def _emit_defs(self) -> None:
        """Allocate every key first, then fill shells in sorted ID order.

        Allocation before filling is what turns a recursive child reference
        into a ``$ref`` cycle; the bodies never recurse through Python.
        """
        routes = [self.plan.routes[route_id] for route_id in sorted(self.plan.routes)]
        for route in routes:
            self.layouts[route.route_id] = _route_layout(route)
            self.defs[self.keys[route.route_id]] = {}
        for route in routes:
            layout = self.layouts[route.route_id]
            body = self._route_body(route, layout)
            body["x-pyxsd-layout"] = layout
            if route.generic_reason is not None or (
                layout == "mixed" and _has_child_wildcard(route)
            ):
                body["x-pyxsd-wildcard"] = True
            self.defs[self.keys[route.route_id]] = body

    def _route_ref(self, route_id: str) -> _Schema:
        return {"$ref": f"#/$defs/{self.keys[route_id]}"}

    def _route_body(self, route: RouteContract, layout: _Layout) -> _Schema:
        if layout == "empty":
            return self._empty_body(route)
        if layout == "scalar":
            return self._scalar_body(route)
        if layout == "repeated":
            return self._repeated_body(route)
        if layout == "record":
            return self._record_body(route)
        if layout == "sequence":
            return self._sequence_body(route)
        return self._mixed_body(route)

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------

    def _attribute_shape(
        self, route: RouteContract
    ) -> tuple[_Schema, list[str], tuple[WildcardContract, ...]]:
        """Declared ``@EQName`` properties, required keys, and wildcards."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for expanded in sorted(route.attributes):
            field = route.attributes[expanded]
            key = "@" + expanded_to_eqname(expanded)
            properties[key] = self._attribute_value(field, route)
            if field.cardinality.minimum > 0:
                required.append(key)
        wildcards = tuple(
            sorted(
                (wildcard for wildcard in route.wildcards if wildcard.role == "attribute"),
                key=lambda wildcard: repr(wildcard.name_class),
            )
        )
        return properties, required, wildcards

    def _attribute_value(self, field: FieldContract, route: RouteContract) -> _Schema:
        """One attribute value: typed scalar or raw lexical string.

        Attribute evidence carries every scalar pattern reachable in the
        element's own content, so a mixed text or whole-element scalar branch
        can make an otherwise unique attribute value a tagged alternative.
        """
        if not field.scalar_contracts:
            return {"type": "string"}
        contracts = (
            *field.scalar_contracts,
            *_content_scalar_contracts(route.content.model),
        )
        return self._scalar_site(contracts, may_be_empty=False)

    def _apply_attribute_openness(
        self, body: _Schema, wildcards: tuple[WildcardContract, ...]
    ) -> None:
        """Close an object, or admit undeclared attributes as lexical strings.

        ``additionalProperties`` covers only names absent from ``properties``,
        so declared typed attributes keep their own schema.  ``propertyNames``
        admits the declared non-attribute members (children and ``#content``)
        or a valid ``@EQName``; namespace declarations and invalid keys stay
        rejected.
        """
        if not wildcards:
            body["additionalProperties"] = False
            return
        declared = [name for name in body.get("properties", {}) if not name.startswith("@")]
        names: list[_Schema] = []
        if declared:
            names.append({"enum": sorted(declared)})
        names.append({"pattern": _ATTRIBUTE_KEY_PATTERN})
        body["propertyNames"] = names[0] if len(names) == 1 else {"anyOf": names}
        body["additionalProperties"] = {"type": "string"}
        body["x-pyxsd-wildcard"] = True
        body["x-pyxsd-name-class"] = [
            _name_class_metadata(wildcard.name_class) for wildcard in wildcards
        ]
        self.unvalidated.add("relaxng-attribute-name-class")

    # ------------------------------------------------------------------
    # Route layouts
    # ------------------------------------------------------------------

    def _empty_body(self, route: RouteContract) -> _Schema:
        """Empty content is ``""`` or an attribute-only object."""
        properties, required, wildcard = self._attribute_shape(route)
        if not properties and not wildcard:
            return {"const": ""}
        body: _Schema = {"type": "object", "properties": properties}
        if required:
            body["required"] = required
        self._apply_attribute_openness(body, wildcard)
        return body

    def _scalar_body(self, route: RouteContract) -> _Schema:
        """One whole-element scalar, optionally under an attribute object."""
        value = self._scalar_site(
            route.content.scalar_contracts,
            may_be_empty=_may_be_empty(route.content.model),
        )
        properties, required, wildcard = self._attribute_shape(route)
        if not properties and not wildcard and route.generic_reason is None:
            return value
        generic = route.generic_reason is not None
        return self._content_object(value, properties, required, wildcard, generic)

    def _generic_scalar_body(self, route: RouteContract) -> _Schema:
        """A scalar reached through an open name always uses the envelope."""
        value = self._scalar_site(
            route.content.scalar_contracts,
            may_be_empty=_may_be_empty(route.content.model),
        )
        properties, required, wildcard = self._attribute_shape(route)
        return self._content_object(value, properties, required, wildcard, True)

    def _content_object(
        self,
        value: _Schema,
        properties: _Schema,
        required: list[str],
        wildcards: tuple[WildcardContract, ...],
        generic: bool,
    ) -> _Schema:
        body: _Schema = {
            "type": "object",
            "properties": {**properties, _CONTENT_KEY: value},
            "required": [*required, _CONTENT_KEY],
        }
        if generic:
            body["x-pyxsd-wildcard"] = True
            self.unvalidated.add("relaxng-generic-route")
        self._apply_attribute_openness(body, wildcards)
        return body

    def _repeated_body(self, route: RouteContract) -> _Schema:
        """One repeatable field as an array, bare or inside an object."""
        field = next(iter(route.children.values()))
        assert field.expanded_name is not None
        key = expanded_to_eqname(field.expanded_name)
        array = self._array_schema(field)
        properties, required, wildcard = self._attribute_shape(route)
        if not properties and not wildcard:
            return array
        body: _Schema = {
            "type": "object",
            "properties": {**properties, key: array},
            "required": [*required, key],
        }
        self._apply_attribute_openness(body, wildcard)
        return body

    def _array_schema(self, field: FieldContract) -> _Schema:
        """The repeated field array with static cardinality bounds."""
        array: _Schema = {"type": "array", "items": self._field_value(field)}
        _apply_cardinality(array, field.cardinality)
        return array

    def _record_body(self, route: RouteContract) -> _Schema:
        """An interleave of max-one fields as a closed object."""
        properties, required, wildcard = self._attribute_shape(route)
        for expanded in sorted(route.children):
            field = route.children[expanded]
            key = expanded_to_eqname(expanded)
            properties[key] = self._field_value(field)
            if field.cardinality.minimum > 0:
                required.append(key)
        body: _Schema = {"type": "object", "properties": properties}
        if required:
            body["required"] = sorted(required)
        self._apply_attribute_openness(body, wildcard)
        return body

    def _sequence_body(self, route: RouteContract) -> _Schema:
        """A conservative ordered array of attribute and child maps."""
        self.unvalidated.add("relaxng-particle-order")
        items: list[_Schema] = []
        properties, required, wildcard = self._attribute_shape(route)
        if properties or wildcard:
            items.append(self._attribute_map(properties, required, wildcard))
        for expanded in sorted(route.children):
            field = route.children[expanded]
            key = expanded_to_eqname(expanded)
            items.append(self._singleton_map(key, self._field_value(field)))
        if not items:
            # A plan with an unrecognized child model still exports singleton
            # child maps; describe them as a generic ordered remainder.
            self.unvalidated.add("relaxng-generic-route")
            items.append(self._generic_child_map())
        return self._ordered_array(items, mixed=False)

    def _generic_child_map(self) -> _Schema:
        """One singleton EQName child map with the generic recursive value."""
        return {
            "type": "object",
            "propertyNames": {"pattern": _EQNAME_PATTERN},
            "additionalProperties": self._generic_mixed_ref(),
            "minProperties": 1,
            "maxProperties": 1,
            "x-pyxsd-wildcard": True,
        }

    def _mixed_body(self, route: RouteContract) -> _Schema:
        """Mixed content: optional attrs, text, and singleton child maps."""
        self.unvalidated.add("relaxng-mixed-order")
        items: list[_Schema] = []
        properties, required, wildcard = self._attribute_shape(route)
        if properties or wildcard:
            items.append(self._attribute_map(properties, required, wildcard))
        items.append({"type": "string"})
        for expanded in sorted(route.children):
            field = route.children[expanded]
            key = expanded_to_eqname(expanded)
            items.append(self._singleton_map(key, self._field_value(field)))
        child_wildcards = [wildcard for wildcard in route.wildcards if wildcard.role == "child"]
        if child_wildcards:
            items.append(self._wildcard_child_map(route, child_wildcards))
        if _is_generic(route):
            self.unvalidated.add("relaxng-generic-route")
        return self._ordered_array(items, mixed=True)

    def _generic_mixed_ref(self) -> _Schema:
        """A recursive reference to one shared generic envelope definition."""
        if self._generic_shell is None:
            # Allocate the shell before filling the self-reference.
            self._generic_shell = {}
            self.defs[_GENERIC_KEY] = self._generic_shell
            self.unvalidated.add("relaxng-generic-route")
            self.unvalidated.add("relaxng-mixed-order")
            self.unvalidated.add("relaxng-attribute-name-class")
            self._generic_shell.update(self._generic_mixed_definition())
        return {"$ref": f"#/$defs/{_GENERIC_KEY}"}

    def _generic_mixed_definition(self) -> _Schema:
        """The fully generic ordered envelope used outside every compiled route."""
        attribute_map: _Schema = {
            "type": "object",
            "propertyNames": {"pattern": _ATTRIBUTE_KEY_PATTERN},
            "additionalProperties": {"type": "string"},
            "minProperties": 1,
            "x-pyxsd-wildcard": True,
            "x-pyxsd-name-class": [{"kind": "anyName"}],
        }
        child_map: _Schema = {
            "type": "object",
            "propertyNames": {"pattern": _EQNAME_PATTERN},
            "additionalProperties": {"$ref": f"#/$defs/{_GENERIC_KEY}"},
            "minProperties": 1,
            "maxProperties": 1,
            "x-pyxsd-wildcard": True,
        }
        return {
            "type": "array",
            "items": {"anyOf": [attribute_map, {"type": "string"}, child_map]},
            "x-pyxsd-wildcard": True,
        }

    def _attribute_map(
        self,
        properties: _Schema,
        required: list[str],
        wildcards: tuple[WildcardContract, ...],
    ) -> _Schema:
        body: _Schema = {
            "type": "object",
            "properties": properties,
            "minProperties": 1,
        }
        if required:
            body["required"] = required
        self._apply_attribute_openness(body, wildcards)
        return body

    @staticmethod
    def _singleton_map(key: str, value: _Schema) -> _Schema:
        return {
            "type": "object",
            "properties": {key: value},
            "required": [key],
            "additionalProperties": False,
            "minProperties": 1,
            "maxProperties": 1,
        }

    def _wildcard_child_map(
        self, route: RouteContract, wildcards: list[WildcardContract]
    ) -> _Schema:
        """A singleton child map over the route's open child-name remainder."""
        self.unvalidated.add("relaxng-wildcard-names")
        properties = {
            expanded_to_eqname(expanded): self._field_value(field)
            for expanded, field in sorted(route.children.items())
        }
        branches: list[_Schema] = []
        for wildcard in wildcards:
            child = (
                self.plan.routes.get(wildcard.child_route_id)
                if wildcard.child_route_id is not None
                else None
            )
            branch = self._generic_value(child)
            if branch not in branches:
                branches.append(branch)
        return {
            "type": "object",
            "properties": properties,
            "additionalProperties": _any_of(branches),
            "propertyNames": {"pattern": _EQNAME_PATTERN},
            "minProperties": 1,
            "maxProperties": 1,
            "x-pyxsd-wildcard": True,
            "x-pyxsd-name-class": [
                _name_class_metadata(wildcard.name_class)
                for wildcard in sorted(wildcards, key=lambda item: repr(item.name_class))
            ],
        }

    @staticmethod
    def _ordered_array(items: list[_Schema], *, mixed: bool) -> _Schema:
        body: _Schema = {
            "type": "array",
            "items": items[0] if len(items) == 1 else {"anyOf": items},
        }
        if mixed:
            body["x-pyxsd-mixed"] = True
        return body

    def _field_value(self, field: FieldContract) -> _Schema:
        """One named child value, or the generic envelope without a route."""
        child = (
            self.plan.routes.get(field.child_route_id) if field.child_route_id is not None else None
        )
        if child is None:
            return self._generic_mixed_ref()
        return self._route_ref(child.route_id)

    def _generic_value(self, route: RouteContract | None) -> _Schema:
        """One value under an open child name, mirroring ``generic=True``.

        A route whose normal layout is already mixed has the same body with or
        without the generic flag, so its normal ``$ref`` is reused and closes
        recursive wildcard cycles.  Every other layout needs a separate
        generic-variant definition whose shell is registered before filling,
        because the flag forces ordered mixed content or the scalar envelope.
        """
        if route is None:
            return self._generic_mixed_ref()
        if self.layouts[route.route_id] == "mixed":
            return self._route_ref(route.route_id)
        return self._generic_route_ref(route)

    def _generic_route_ref(self, route: RouteContract) -> _Schema:
        """A recursive reference to one route's generic-variant definition."""
        key = f"{self.keys[route.route_id]}-generic"
        shell = self.defs.get(key)
        if shell is None:
            shell = {}
            self.defs[key] = shell
            body = self._generic_route_body(route)
            body["x-pyxsd-wildcard"] = True
            self.unvalidated.add("relaxng-generic-route")
            shell.update(body)
        return {"$ref": f"#/$defs/{key}"}

    def _generic_route_body(self, route: RouteContract) -> _Schema:
        """The generic-flag body for a route whose normal layout is not mixed."""
        if self.layouts[route.route_id] == "scalar":
            body = self._generic_scalar_body(route)
            body["x-pyxsd-layout"] = "scalar"
            return body
        body = self._mixed_body(route)
        body["x-pyxsd-layout"] = "mixed"
        return body

    # ------------------------------------------------------------------
    # Scalars and tags
    # ------------------------------------------------------------------

    def _scalar_site(self, contracts: tuple[ScalarContract, ...], *, may_be_empty: bool) -> _Schema:
        """Describe one scalar slot from its complete static contract set."""
        unique = tuple(dict.fromkeys(sorted(contracts, key=lambda item: item.pattern_identity)))
        assert unique
        if len(unique) == 1:
            value = self._contract_schema(unique[0])
        else:
            branches = [self._tag_schema()]
            branches.extend(self._contract_schema(contract) for contract in unique)
            value = _any_of(branches)
        if may_be_empty:
            value = _any_of([value, {"const": ""}])
        return value

    def _contract_schema(self, contract: ScalarContract) -> _Schema:
        """One contract's non-tag shape, falling back to the tag when needed."""
        if contract.kind == "text":
            # The exporter returns a tag for a text-only scalar position, while
            # the lexical string remains a sound description of the same data.
            return _any_of([{"type": "string"}, self._tag_schema()])
        if contract.kind == "list":
            return self._list_schema(contract)
        return self._atomic_schema(contract)

    def _list_schema(self, contract: ScalarContract) -> _Schema:
        """A plain fixed or homogeneous array, or the whole-list tag."""
        pattern = self.plan.encoding_patterns[contract.pattern_identity]
        assert isinstance(pattern, List)
        shape = list_shape(pattern)
        if shape == "interpretation" or not list_items_decodable(pattern):
            return self._tag_schema()
        token_patterns = _token_patterns(pattern)
        assert token_patterns is not None
        kind, patterns, minimum = token_patterns
        if kind == "fixed":
            body: _Schema = {
                "type": "array",
                "items": False,
                "minItems": len(patterns),
                "maxItems": len(patterns),
            }
            if patterns:
                body["prefixItems"] = [self._token_schema(token) for token in patterns]
        else:
            body = {
                "type": "array",
                "items": self._token_schema(patterns[0]),
            }
            if minimum > 0:
                body["minItems"] = minimum
        body["x-pyxsd-list-shape"] = shape
        return body

    def _token_schema(self, pattern: Pattern) -> _Schema:
        """One fixed or repeated list token's plain scalar schema.

        A whole-list contract already required stable metadata from every
        token, so this cannot be an opaque datatype.
        """
        return self._plain_atomic_schema(scalar_contract(pattern))

    def _atomic_schema(self, contract: ScalarContract) -> _Schema:
        """One atomic scalar: its plain value, or the nondecodable tag."""
        pattern = self.plan.encoding_patterns[contract.pattern_identity]
        if not _pattern_decodable(pattern):
            return self._tag_schema()
        return self._plain_atomic_schema(contract)

    def _plain_atomic_schema(self, contract: ScalarContract) -> _Schema:
        """The JSON value a decodable atomic contract exports."""
        if contract.library_uri == BUILTIN_LIBRARY:
            return {"type": "string", "x-pyxsd-type": contract.type_name}
        if contract.library_uri != XSD_LIBRARY:
            return self._opaque_scalar()
        name = contract.type_name
        assert name is not None
        if name == "boolean":
            return {"type": "boolean"}
        if name in _XSD_INTEGER_TYPES:
            return {"type": "integer"}
        if name == "decimal":
            return {
                "type": "string",
                "pattern": _DECIMAL_PATTERN,
                "x-pyxsd-type": "decimal",
            }
        if name in _XSD_FLOAT_TYPES:
            return {
                "anyOf": [
                    {"type": "number"},
                    {"enum": ["INF", "-INF", "NaN"]},
                ]
            }
        if name in _XSD_QNAME_TYPES:
            return {
                "type": "string",
                "pattern": _EQNAME_PATTERN,
                "x-pyxsd-qname": True,
            }
        if name in _XSD_LIST_VALUED_TYPES:
            return {"type": "array", "items": {"type": "string"}, "minItems": 1}
        return {"type": "string", "x-pyxsd-type": name}

    def _opaque_scalar(self) -> _Schema:
        """A disclosed recursive sound union for a custom decoded value."""
        self.unvalidated.add("relaxng-custom-scalar")
        if self._opaque_shell is None:
            self._opaque_shell = {}
            self.defs[_OPAQUE_KEY] = self._opaque_shell
            self._opaque_shell.update(
                {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "number"},
                        {"type": "boolean"},
                        {
                            "type": "array",
                            "items": {"$ref": f"#/$defs/{_OPAQUE_KEY}"},
                        },
                    ]
                }
            )
        return {"$ref": f"#/$defs/{_OPAQUE_KEY}"}

    def _tag_schema(self) -> _Schema:
        """The exact version-1 tagged-scalar wire shape, without frozen IDs."""
        self.unvalidated.add("relaxng-scalar-tag-order")
        self.unvalidated.add("relaxng-scalar-tag-grammar-identity")
        context: _Schema = {
            "type": "object",
            "properties": {
                "base_uri": {"type": ["string", "null"]},
                "namespaces": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "not": {"required": ["xml"]},
                },
            },
            "required": ["base_uri", "namespaces"],
            "additionalProperties": False,
        }
        body: _Schema = {
            "type": "object",
            "properties": {
                "version": {"type": "integer", "const": 1},
                "grammar": {"type": "string", "minLength": 1},
                "lexical": {"type": "string"},
                "context": context,
                "candidates": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "required": ["version", "grammar", "lexical", "context", "candidates"],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {_TAG_KEY: body},
            "required": [_TAG_KEY],
            "additionalProperties": False,
        }


# ---------------------------------------------------------------------------
# Static route and content analysis
# ---------------------------------------------------------------------------


def _route_layout(route: RouteContract) -> _Layout:
    """The exporter's compiled layout, independent of any observation."""
    if route.content.scalar_contracts:
        return "scalar"
    if route.generic_reason is not None or _has_child_wildcard(route):
        return "mixed"
    if route.content.mixed:
        return "mixed"
    model = _child_model(route.content.model)
    if model is None:
        return "empty"
    children = list(route.children.values())
    if len(children) == 1 and children[0].cardinality.repeatable:
        return "repeated"
    if model.kind == "interleave" and all(not field.cardinality.repeatable for field in children):
        return "record"
    return "sequence"


def _has_child_wildcard(route: RouteContract) -> bool:
    return any(wildcard.role == "child" for wildcard in route.wildcards)


def _is_generic(route: RouteContract) -> bool:
    """Whether this route has a whole-element representation loss."""
    return route.generic_reason is not None or _has_child_wildcard(route)


def _child_model(model: ContentContract) -> ContentContract | None:
    """Erase attribute-only content once per shared DAG node."""
    results: dict[int, ContentContract | None] = {}
    pending: list[tuple[ContentContract, bool]] = [(model, False)]
    while pending:
        node, ready = pending.pop()
        if id(node) in results:
            continue
        if not ready:
            pending.append((node, True))
            for operand in node.operands:
                if id(operand) not in results:
                    pending.append((operand, False))
            continue
        if node.kind in ("attribute", "empty"):
            results[id(node)] = None
            continue
        operands = tuple(
            part for operand in node.operands if (part := results[id(operand)]) is not None
        )
        if node.kind in ("group", "interleave") and len(operands) == 1:
            result = operands[0]
        elif node.operands and not operands:
            result = None
        else:
            result = ContentContract(node.kind, operands)
        results[id(node)] = result
    return results[id(model)]


def _content_scalar_contracts(model: ContentContract) -> tuple[ScalarContract, ...]:
    """Every scalar pattern in one element's own content model.

    Attribute sites see these contracts in addition to their own declared
    contract: the mixed text pattern or a whole-element scalar alternative is
    part of the complete grammar-level evidence at the same element.
    """
    result: dict[ScalarContract, None] = {}
    pending: list[ContentContract] = [model]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if node.kind == "scalar":
            result.update(dict.fromkeys(node.scalar_contracts))
        pending.extend(node.operands)
    return tuple(sorted(result, key=lambda item: item.pattern_identity))


def _may_be_empty(model: ContentContract) -> bool:
    """Whether a valid content model can contribute no scalar occurrence."""
    results: dict[int, bool] = {}
    pending: list[tuple[ContentContract, bool]] = [(model, False)]
    while pending:
        node, ready = pending.pop()
        if id(node) in results:
            continue
        if not ready:
            pending.append((node, True))
            for operand in node.operands:
                if id(operand) not in results:
                    pending.append((operand, False))
            continue
        if node.kind in ("attribute", "element", "empty"):
            value = True
        elif node.kind in ("scalar", "notAllowed"):
            value = False
        elif node.kind in ("group", "interleave"):
            value = all(results[id(operand)] for operand in node.operands)
        elif node.kind == "choice":
            value = any(results[id(operand)] for operand in node.operands)
        elif node.kind == "repeat":
            value = all(results[id(operand)] for operand in node.operands)
        else:
            value = False
        results[id(node)] = value
    return results[id(model)]


def _token_patterns(pattern: List) -> _TokenPlan | None:
    """The fixed or homogeneous token patterns of one plain list shape."""
    results: dict[int, _TokenPlan | None] = {}
    pending: list[tuple[Pattern, bool]] = [(pattern.pattern, False)]
    while pending:
        node, ready = pending.pop()
        if id(node) in results:
            continue
        if not ready:
            pending.append((node, True))
            for child in _token_children(node):
                if id(child) not in results:
                    pending.append((child, False))
            continue
        results[id(node)] = _combine_tokens(node, results)
    return results[id(pattern.pattern)]


def _token_children(pattern: Pattern) -> tuple[Pattern, ...]:
    if isinstance(pattern, Ref):
        return (pattern.target,)
    if isinstance(pattern, (Choice, Group)):
        return (pattern.first, pattern.second)
    if isinstance(pattern, OneOrMore):
        return (pattern.pattern,)
    return ()


def _combine_tokens(pattern: Pattern, results: dict[int, _TokenPlan | None]) -> _TokenPlan | None:
    if isinstance(pattern, Ref):
        return results[id(pattern.target)]
    if isinstance(pattern, Empty):
        return ("fixed", (), 0)
    if isinstance(pattern, (Data, DataExcept, Value)):
        return ("fixed", (pattern,), 1)
    if isinstance(pattern, Group):
        first = results[id(pattern.first)]
        second = results[id(pattern.second)]
        if first is None or second is None:
            return None
        if first[0] == second[0] == "fixed":
            return ("fixed", (*first[1], *second[1]), first[2] + second[2])
        return None
    if isinstance(pattern, OneOrMore):
        item = results[id(pattern.pattern)]
        if item is not None and item[0] == "fixed" and len(item[1]) == 1:
            return ("list", item[1], 1)
        return None
    if isinstance(pattern, Choice):
        first = results[id(pattern.first)]
        second = results[id(pattern.second)]
        if first is not None and second is not None:
            if first[0] == "fixed" and not first[1] and second[0] == "list":
                return ("list", second[1], 0)
            if second[0] == "fixed" and not second[1] and first[0] == "list":
                return ("list", first[1], 0)
        return None
    return None


def _pattern_decodable(pattern: Pattern) -> bool:
    """Whether one atomic contract's compiled datatype can provide a value."""
    assert isinstance(pattern, (Data, DataExcept, Value))
    return datatype_supports_decode(pattern.datatype)


def _apply_cardinality(schema: _Schema, cardinality: Cardinality) -> None:
    if cardinality.minimum > 0:
        schema["minItems"] = cardinality.minimum
    if cardinality.maximum is not None:
        schema["maxItems"] = cardinality.maximum


def _any_of(branches: list[_Schema]) -> _Schema:
    """Deduplicate branches in a deterministic order."""
    unique: list[_Schema] = []
    for branch in branches:
        if branch not in unique:
            unique.append(branch)
    return unique[0] if len(unique) == 1 else {"anyOf": unique}


def _admitted_names(name_class: NameClass) -> tuple[str, ...] | None:
    """Every expanded name a closed class admits, or ``None`` when infinite."""
    if isinstance(name_class, Name):
        return (_expanded(name_class.uri, name_class.local),)
    if isinstance(name_class, NameChoice):
        result: list[str] = []
        for alternative in name_class.alternatives:
            names = _admitted_names(alternative)
            if names is None:
                return None
            result.extend(names)
        return tuple(sorted(set(result)))
    if isinstance(name_class, ExceptName):
        base = _admitted_names(name_class.base)
        if base is None:
            return None
        return tuple(name for name in base if name_class.contains(*_split_expanded(name)))
    return None


def _expanded(uri: str | None, local: str) -> str:
    return f"{{{uri}}}{local}" if uri else local


def _excluded_eqnames(name_class: NameClass) -> tuple[str, ...]:
    """Finite names an open class explicitly removes from its base."""
    if isinstance(name_class, ExceptName):
        mentioned = _mentioned_names(name_class.excluded)
        return (*mentioned, *_excluded_eqnames(name_class.base))
    if isinstance(name_class, NameChoice):
        return tuple(
            sorted(
                {
                    name
                    for alternative in name_class.alternatives
                    for name in _excluded_eqnames(alternative)
                }
            )
        )
    return ()


def _mentioned_names(name_class: NameClass) -> tuple[str, ...]:
    """Finite names a closed class mentions, even inside an exclusion."""
    names = _admitted_names(name_class)
    if names is not None:
        return names
    if isinstance(name_class, ExceptName):
        return (
            *_mentioned_names(name_class.base),
            *_mentioned_names(name_class.excluded),
        )
    if isinstance(name_class, NameChoice):
        return tuple(
            sorted(
                {
                    name
                    for alternative in name_class.alternatives
                    for name in _mentioned_names(alternative)
                }
            )
        )
    return ()


def _split_expanded(name: str) -> tuple[str | None, str]:
    if name.startswith("{"):
        uri, local = name[1:].split("}", 1)
        return uri or None, local
    return None, name


def _name_class_metadata(name_class: NameClass) -> Any:
    """A deterministic, JSON-safe symbolic description of one name class."""
    if isinstance(name_class, AnyName):
        return {"kind": "anyName"}
    if isinstance(name_class, NsName):
        return {"kind": "nsName", "uri": name_class.uri}
    if isinstance(name_class, Name):
        return {"kind": "name", "uri": name_class.uri, "local": name_class.local}
    if isinstance(name_class, NameChoice):
        alternatives = sorted(
            (_name_class_metadata(item) for item in name_class.alternatives),
            key=repr,
        )
        return {"kind": "choice", "alternatives": alternatives}
    if isinstance(name_class, ExceptName):
        return {
            "kind": "except",
            "base": _name_class_metadata(name_class.base),
            "excluded": _name_class_metadata(name_class.excluded),
        }
    raise TypeError(f"unknown RELAX NG name class {type(name_class).__name__}")
