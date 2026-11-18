"""Draft 2020-12 JSON Schema for every RELAX NG data format.

The projection plan, not an instance, selects every shape here.  A route body
describes the exact root-preserving, attribute/keyed, ordered-array, or tagged
scalar envelope the matching RELAX NG exporter emits.  Route definitions are
allocated in sorted route-ID order and filled through shells, so recursive
resources become ``$ref`` cycles instead of unbounded expansions.

This is a structural data contract, never a RELAX NG validator.  Grammar
validity, name-class membership, datatype lexical spaces and namespaces,
candidate identity and completeness, lexical prefix binding, and any ordering a
conservative array cannot express stay native; they are listed under
``x-pyxsd-validation.unvalidated``.  Tagged scalar metadata is shape-only: no
grammar fingerprint, candidate ID, or occurrence-specific candidate list is
frozen into the generated schema.
"""

from __future__ import annotations

import re
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
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    XSD_LIBRARY,
    datatype_supports_decode,
)
from pyxsd.relaxng.json_profile import (
    _layout as _exporter_layout,
)
from pyxsd.relaxng.json_profile import (
    _plan_route,
    _select_expanded_root,
    _unique_admitted_root,
)
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

#: One NCName-shaped token as the lexical formats spell local names.
_NCNAME = "[^\\s:{}#@/$]+"

#: A lexical QName prefix; ``xmlns`` is reserved for declarations.
_PREFIX = f"(?!xmlns:){_NCNAME}"

#: A lexical QName: an optional prefix and one local name.
_QNAME_PATTERN = f"^(?:{_PREFIX}:)?{_NCNAME}$"

#: A native Clark name: ``{uri}local`` or a bare local name.
_CLARK_PATTERN = "^(?:\\{[\\s\\S]*\\})?[^\\s:{}#@/$]+$"

#: Native attribute keys: ``@`` plus a Clark name, never a namespace declaration.
_CLARK_ATTRIBUTE_PATTERN = (
    "^@(?!xmlns$)(?!Q\\{http://www\\.w3\\.org/2000/xmlns/\\})"
    "(?!\\{http://www\\.w3\\.org/2000/xmlns/\\})(?:\\{[\\s\\S]*\\})?[^\\s:{}#@/$]+$"
)

#: BadgerFish attribute keys: ``@`` plus a lexical QName.
_LEXICAL_ATTRIBUTE_PATTERN = f"^@(?!xmlns(?::|$))(?:{_NCNAME}:)?{_NCNAME}$"

#: JsonML attribute keys: a lexical QName, never a namespace declaration.
_JSONML_ATTRIBUTE_PATTERN = f"^(?!xmlns(?::|$))(?:{_NCNAME}:)?{_NCNAME}$"

#: One namespace-declaration prefix (the default binding is the empty string).
_NAMESPACE_PREFIX_PATTERN = f"^(?!xmlns$)(?:{_NCNAME})?$"

_XML_NS = "http://www.w3.org/XML/1998/namespace"
_XMLNS_NS = "http://www.w3.org/2000/xmlns/"

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
    """Return the Draft 2020-12 schema for this schema's data contract.

    ``format`` selects one of the five RELAX NG data formats.  ``element``
    accepts the same local, Clark, and EQName root spellings as
    :meth:`RelaxNGSchema.from_data`; the root-absorbed native and Parker
    formats need one deterministic start name, while ``pyxsd-json``,
    BadgerFish, and JsonML keep every admitted root and open classes symbolic.
    ``typed`` and ``always_list`` are native-only options and must keep their
    defaults for every other format, like the shared data-export and XSD JSON
    Schema rules.  ``fidelity`` is accepted for interface parity; each mode
    describes every export that mode can produce.
    """
    requested_format = validate_format(format)
    validate_fidelity(fidelity)
    if requested_format != "pyxsd":
        if typed is not True:
            raise ValueError("typed is a native-only JSON Schema option")
        if always_list is not False:
            raise ValueError("always_list is a native-only JSON Schema option")

    if requested_format == "pyxsd-json":
        return _RelaxNGJSONSchemaBuilder(schema.projection).build(element)
    return _FormatJSONSchemaBuilder(
        schema.projection,
        format=requested_format,
        typed=typed,
        always_list=always_list,
    ).build(element)


class _ScalarSchemaMixin:
    """Scalar and tag fragments shared by every RELAX NG format.

    The projection plan supplies the complete static contract set for a site.
    ``_tagged`` selects the native/profile wire form that carries version-1
    tags for alternatives, while conventional formats keep the exporter's
    lexical string.  ``_qname_eqname`` distinguishes the profile's expanded
    EQName spelling from the lexical QName every other format emits.
    """

    plan: ProjectionPlan
    defs: dict[str, _Schema]
    unvalidated: set[str]
    _tagged: bool
    _qname_eqname: bool
    _opaque_key: str
    _opaque_shell: _Schema | None

    def _scalar_site(self, contracts: tuple[ScalarContract, ...], *, may_be_empty: bool) -> _Schema:
        """Describe one scalar slot from its complete static contract set."""
        unique = tuple(dict.fromkeys(sorted(contracts, key=lambda item: item.pattern_identity)))
        assert unique
        if len(unique) == 1:
            value = self._contract_schema(unique[0])
        else:
            branches = [self._aggregate_scalar_schema()]
            branches.extend(self._contract_schema(contract) for contract in unique)
            value = _any_of(branches)
        if may_be_empty:
            value = _any_of([value, {"const": ""}])
        return value

    def _aggregate_scalar_schema(self) -> _Schema:
        """A scalar alternative the exporter cannot decode to one value."""
        if self._tagged:
            return self._tag_schema()
        return {"type": "string"}

    def _contract_schema(self, contract: ScalarContract) -> _Schema:
        """One contract's non-tag shape, falling back to the aggregate form."""
        if contract.kind == "text":
            # The exporter returns a tag for a text-only scalar position in the
            # tagged formats, while the lexical string remains a sound
            # description of the same data.
            if self._tagged:
                return _any_of([{"type": "string"}, self._tag_schema()])
            return {"type": "string"}
        if contract.kind == "list":
            return self._list_schema(contract)
        return self._atomic_schema(contract)

    def _list_schema(self, contract: ScalarContract) -> _Schema:
        """A plain fixed or homogeneous array, or the aggregate form."""
        pattern = self.plan.encoding_patterns[contract.pattern_identity]
        assert isinstance(pattern, List)
        shape = list_shape(pattern)
        if shape == "interpretation" or not list_items_decodable(pattern):
            return self._aggregate_scalar_schema()
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
        """One atomic scalar: its plain value, or the aggregate form."""
        pattern = self.plan.encoding_patterns[contract.pattern_identity]
        if not _pattern_decodable(pattern):
            return self._aggregate_scalar_schema()
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
            if self._qname_eqname:
                return {
                    "type": "string",
                    "pattern": _EQNAME_PATTERN,
                    "x-pyxsd-qname": True,
                }
            return {
                "type": "string",
                "pattern": _QNAME_PATTERN,
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
            self.defs[self._opaque_key] = self._opaque_shell
            self._opaque_shell.update(
                {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "number"},
                        {"type": "boolean"},
                        {
                            "type": "array",
                            "items": {"$ref": f"#/$defs/{self._opaque_key}"},
                        },
                    ]
                }
            )
        return {"$ref": f"#/$defs/{self._opaque_key}"}

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


class _RelaxNGJSONSchemaBuilder(_ScalarSchemaMixin):
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
        self._tagged = True
        self._qname_eqname = True
        self._opaque_key = _OPAQUE_KEY
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
            properties[key] = self._attribute_value(field)
            if field.cardinality.minimum > 0:
                required.append(key)
        wildcards = tuple(
            sorted(
                (
                    wildcard
                    for wildcard in route.wildcards
                    if wildcard.role == "attribute" and wildcard.cardinality.maximum != 0
                ),
                key=lambda wildcard: repr(wildcard.name_class),
            )
        )
        return properties, required, wildcards

    def _attribute_value(self, field: FieldContract) -> _Schema:
        """One attribute value from that field's own scalar contracts only.

        Attribute evidence never borrows the element's content patterns: a
        mixed-text or whole-element scalar branch cannot make an unrelated
        attribute schema accept a tag or a non-decodable lexical value.
        """
        if not field.scalar_contracts:
            return {"type": "string"}
        return self._scalar_site(field.scalar_contracts, may_be_empty=False)

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


# ---------------------------------------------------------------------------
# Native, Parker, BadgerFish, and JsonML envelopes
# ---------------------------------------------------------------------------


class _FormatJSONSchemaBuilder(_ScalarSchemaMixin):
    """One finite projection of a compiled plan into a conventional envelope.

    Each format keeps the exact root and body container its exporter emits:
    native and Parker absorb the root, BadgerFish retains one lexical root
    property, and JsonML is one ordered tag array.  Definitions are allocated
    lazily, so a scalar root emits no unused ``$defs``, while every recursive
    route still closes through one ``$ref`` cycle.
    """

    def __init__(
        self,
        plan: ProjectionPlan,
        *,
        format: str,
        typed: bool,
        always_list: bool,
    ) -> None:
        self.plan = plan
        self.format = format
        self.typed = typed
        self.always_list = always_list
        self.defs: dict[str, _Schema] = {}
        self.unvalidated: set[str] = set(_BASE_UNVALIDATED)
        self.keys = {
            route_id: f"{format}-route-{index}"
            for index, route_id in enumerate(sorted(plan.routes), start=1)
        }
        self._tagged = format == "pyxsd" and typed
        self._qname_eqname = False
        self._opaque_key = f"{format}-opaque-scalar"
        self._opaque_shell: _Schema | None = None
        self._pending: list[tuple[str, str, str | None, bool]] = []
        self._declarations_possible = _plan_has_declarations(plan)
        if format == "parker" and _plan_has_attributes(plan):
            self.unvalidated.add("relaxng-parker-attribute-loss")
        if format == "parker" and _plan_has_namespaced_names(plan):
            self.unvalidated.add("relaxng-local-name-mapping")
        if format in ("parker", "badgerfish"):
            self.unvalidated.add("relaxng-particle-order")
        if format == "badgerfish":
            self.unvalidated.add("relaxng-lexical-name-binding")
            self.unvalidated.add("relaxng-prefix-scope")
        if format == "jsonml":
            self.unvalidated.add("relaxng-lexical-name-binding")
            self.unvalidated.add("relaxng-prefix-scope")
        if format in ("badgerfish", "jsonml") and _plan_has_custom_scalars(plan):
            self.unvalidated.add("relaxng-custom-qname-declarations")
        if _plan_has_unenforced_wildcard_maximum(plan, format):
            self.unvalidated.add("relaxng-wildcard-cardinality")

    # ------------------------------------------------------------------
    # Entry point, definitions, and root selection
    # ------------------------------------------------------------------

    def build(self, element: str | None) -> dict[str, object]:
        """Build the envelope around the selected format's root contract."""
        root = self._build_root(element)
        self._process_pending()
        envelope: dict[str, object] = {
            "$schema": _SCHEMA_URI,
            "x-pyxsd-format": self.format,
            "x-pyxsd-profile": "relaxng",
            "x-pyxsd-profile-version": 1,
            **root,
            "x-pyxsd-validation": {
                "level": "structural",
                "unvalidated": sorted(self.unvalidated),
            },
        }
        if self.defs:
            envelope["$defs"] = self._ordered_defs()
        return envelope

    def _build_root(self, element: str | None) -> _Schema:
        if self.format in ("pyxsd", "parker"):
            name, route = self._absorbed_root(element)
            body = (
                self._native_root_body(route)
                if self.format == "pyxsd"
                else self._parker_body(route, generic=False)
            )
            body = dict(body)
            body["x-pyxsd-root"] = _root_metadata(name)
            return body
        if self.format == "badgerfish":
            return self._badgerfish_roots(element)
        return self._jsonml_roots(element)

    def _absorbed_root(self, element: str | None) -> tuple[str, RouteContract]:
        """The one deterministic start name a root-absorbing format needs."""
        name = self._selected_root_name(element)
        route = _plan_route(self.plan, name)
        assert route is not None
        return name, route

    def _selected_root_name(self, element: str | None) -> str:
        """Apply the importer's exact root-selection contract."""
        if element is None:
            return _unique_admitted_root(self.plan)
        return _select_expanded_root(self.plan, element)

    def _route_ref(self, route_id: str, *, generic: bool = False) -> _Schema:
        key = self.keys[route_id] + ("-generic" if generic else "")
        if key not in self.defs:
            self.defs[key] = {}
            self._pending.append(("route", key, route_id, generic))
        return {"$ref": f"#/$defs/{key}"}

    def _generic_ref(self) -> _Schema:
        key = f"{self.format}-generic-route"
        if key not in self.defs:
            self.defs[key] = {}
            self._pending.append(("generic", key, None, False))
        return {"$ref": f"#/$defs/{key}"}

    def _scalar_union_ref(self) -> _Schema:
        """A recursive JSON-scalar union for undetermined wildcard values."""
        key = f"{self.format}-scalar-union"
        if key not in self.defs:
            shell: _Schema = {}
            self.defs[key] = shell
            branches: list[_Schema] = [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "array", "items": {"$ref": f"#/$defs/{key}"}},
            ]
            if self._tagged:
                branches.append(self._tag_schema())
            shell.update({"anyOf": branches})
        return {"$ref": f"#/$defs/{key}"}

    def _generic_value(self, route: RouteContract | None) -> _Schema:
        """One value under an open child name, mirroring ``generic=True``."""
        if route is None:
            return self._generic_ref()
        if _exporter_layout(route, True) == _exporter_layout(route, False):
            return self._route_ref(route.route_id)
        return self._route_ref(route.route_id, generic=True)

    def _process_pending(self) -> None:
        """Fill every reachable definition once, in discovery order."""
        index = 0
        while index < len(self._pending):
            kind, key, route_id, generic = self._pending[index]
            index += 1
            if kind == "generic":
                body = self._generic_body()
            else:
                assert route_id is not None
                body = self._format_route_body(self.plan.routes[route_id], generic)
            self.defs[key].update(body)

    def _ordered_defs(self) -> dict[str, _Schema]:
        """Deterministic route-first definition insertion order."""

        def order(key: str) -> tuple[int, int, int, str]:
            match = re.fullmatch(rf"{re.escape(self.format)}-route-(\d+)(-generic)?", key)
            if match is not None:
                return (0, int(match.group(1)), 1 if match.group(2) else 0, key)
            if key == f"{self.format}-generic-route":
                return (1, 0, 0, key)
            return (2, 0, 0, key)

        return {key: self.defs[key] for key in sorted(self.defs, key=order)}

    def _format_route_body(self, route: RouteContract, generic: bool) -> _Schema:
        if self.format == "pyxsd":
            return self._native_child_value(route, generic=generic)
        if self.format == "parker":
            return self._parker_body(route, generic=generic)
        if self.format == "badgerfish":
            return self._badgerfish_body(route, generic=generic)
        return self._jsonml_node(route, self._route_tag_schema(route), generic=generic)

    def _generic_body(self) -> _Schema:
        """The disclosed recursive envelope for a route-less open name."""
        if self.format == "pyxsd":
            return self._native_generic_value()
        if self.format == "parker":
            return self._parker_generic_value()
        if self.format == "badgerfish":
            return self._badgerfish_generic_value()
        return self._jsonml_generic_value()

    # ------------------------------------------------------------------
    # Shared field and scalar helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _attribute_wildcards(route: RouteContract) -> tuple[WildcardContract, ...]:
        return tuple(
            sorted(
                (
                    wildcard
                    for wildcard in route.wildcards
                    if wildcard.role == "attribute" and wildcard.cardinality.maximum != 0
                ),
                key=lambda wildcard: repr(wildcard.name_class),
            )
        )

    @staticmethod
    def _child_wildcards(route: RouteContract) -> tuple[WildcardContract, ...]:
        return tuple(
            sorted(
                (
                    wildcard
                    for wildcard in route.wildcards
                    if wildcard.role == "child" and wildcard.cardinality.maximum != 0
                ),
                key=lambda wildcard: repr(wildcard.name_class),
            )
        )

    @staticmethod
    def _fits(field: FieldContract) -> bool:
        """Whether a compiled field can occur in a valid instance."""
        return field.cardinality.maximum != 0

    def _attributes_possible(self, route: RouteContract) -> bool:
        return bool(route.attributes or self._attribute_wildcards(route))

    def _children_possible(self, route: RouteContract) -> bool:
        return bool(route.children or self._child_wildcards(route))

    def _can_be_empty(self, route: RouteContract) -> bool:
        """Whether an element can omit every attribute and child."""
        if any(field.cardinality.minimum > 0 for field in route.attributes.values()):
            return False
        return self._children_can_be_absent(route)

    def _children_can_be_absent(self, route: RouteContract) -> bool:
        """Whether every child can be absent, ignoring dropped attributes."""
        if any(field.cardinality.minimum > 0 for field in route.children.values()):
            return False
        return not any(
            wildcard.role == "child" and wildcard.cardinality.minimum > 0
            for wildcard in route.wildcards
        )

    @staticmethod
    def _require_property(body: _Schema, pattern: str) -> None:
        """Require at least one member whose name matches *pattern*."""
        body.setdefault("allOf", []).append(
            {"not": {"propertyNames": {"not": {"pattern": pattern}}}}
        )

    def _attribute_site(self, field: FieldContract) -> _Schema:
        """One attribute value from its own field contracts only.

        An attribute with no contracts is lexical even when the element's
        content is typed.  The element's content patterns are never appended:
        they belong to a different occurrence and cannot make this attribute
        accept a tag or another scalar branch.
        """
        if not field.scalar_contracts:
            return {"type": "string"}
        if self.format == "pyxsd" and not self.typed:
            return {"type": "string"}
        return self._scalar_site(field.scalar_contracts, may_be_empty=False)

    def _wildcard_attribute_site(self, wildcard: WildcardContract) -> _Schema:
        # The open-name pattern also covers declared attribute keys, whose own
        # property schema stays authoritative.  A sound scalar union keeps that
        # overlap from rejecting a declared typed value.
        del wildcard
        if self.format == "pyxsd" and not self.typed:
            return {"type": "string"}
        return self._scalar_union_ref()

    def _child_field_value(self, field: FieldContract) -> _Schema:
        route = (
            self.plan.routes.get(field.child_route_id) if field.child_route_id is not None else None
        )
        if route is None:
            return self._generic_ref()
        return self._route_ref(route.route_id)

    def _wildcard_child_value(self, wildcard: WildcardContract) -> _Schema:
        route = (
            self.plan.routes.get(wildcard.child_route_id)
            if wildcard.child_route_id is not None
            else None
        )
        return self._generic_value(route)

    def _native_scalar(
        self, contracts: tuple[ScalarContract, ...], *, may_be_empty: bool
    ) -> _Schema:
        if not self.typed:
            return {"type": "string"}
        return self._scalar_site(contracts, may_be_empty=may_be_empty)

    @staticmethod
    def _observed_group(value: _Schema, cardinality: Cardinality) -> _Schema:
        """One observed-count group: a bare value or an explicit array."""
        if not cardinality.repeatable:
            return value
        branches: list[_Schema] = []
        if cardinality.minimum <= 1:
            branches.append(value)
        array: _Schema = {
            "type": "array",
            "items": value,
            "minItems": max(2, cardinality.minimum),
        }
        if cardinality.maximum is not None:
            array["maxItems"] = cardinality.maximum
        branches.append(array)
        return _any_of(branches)

    @staticmethod
    def _always_list_group(value: _Schema, cardinality: Cardinality) -> _Schema:
        """Every present native child group becomes an array."""
        array: _Schema = {
            "type": "array",
            "items": value,
            "minItems": max(1, cardinality.minimum),
        }
        if cardinality.maximum is not None:
            array["maxItems"] = cardinality.maximum
        return array

    # ------------------------------------------------------------------
    # Native
    # ------------------------------------------------------------------

    def _native_leaf_value(self, route: RouteContract, *, generic: bool) -> _Schema:
        layout = _exporter_layout(route, generic)
        if layout == "scalar":
            return self._native_scalar(
                route.content.scalar_contracts,
                may_be_empty=_may_be_empty(route.content.model),
            )
        if layout == "mixed":
            if not self.typed:
                return {"type": "string"}
            return self._scalar_union_ref()
        if not self.typed:
            return {"type": "string"}
        return {"type": "null"}

    def _native_root_body(self, route: RouteContract) -> _Schema:
        layout = _exporter_layout(route, False)
        if layout == "scalar":
            return self._native_object(route, layout)
        branches: list[_Schema] = []
        if self._can_be_empty(route):
            branches.append(self._native_wrapped_leaf(route, layout))
        if self._children_possible(route) or self._attributes_possible(route):
            branches.append(self._native_object(route, layout))
        return _any_of(branches)

    def _native_wrapped_leaf(self, route: RouteContract, layout: str) -> _Schema:
        """A childless native root wraps its leaf value under ``$``."""
        return {
            "type": "object",
            "properties": {"$": self._native_leaf_value(route, generic=False)},
            "required": ["$"],
            "additionalProperties": False,
            "minProperties": 1,
            "maxProperties": 1,
            "x-pyxsd-layout": layout,
        }

    def _native_child_value(self, route: RouteContract, *, generic: bool) -> _Schema:
        layout = _exporter_layout(route, generic)
        branches: list[_Schema] = []
        if self._can_be_empty(route):
            branches.append(self._native_leaf_value(route, generic=generic))
        if layout == "scalar":
            if self._attributes_possible(route):
                branches.append(self._native_object(route, layout))
        elif self._children_possible(route) or self._attributes_possible(route):
            branches.append(self._native_object(route, layout))
        body = _any_of(branches)
        body.setdefault("x-pyxsd-layout", layout)
        return body

    def _native_object(self, route: RouteContract, layout: str) -> _Schema:
        if layout == "sequence":
            self.unvalidated.add("relaxng-particle-order")
        elif layout == "mixed":
            self.unvalidated.add("relaxng-mixed-order")
        properties: dict[str, Any] = {}
        required: list[str] = []
        for expanded in sorted(route.attributes):
            field = route.attributes[expanded]
            key = "@" + expanded
            properties[key] = self._attribute_site(field)
            if field.cardinality.minimum > 0:
                required.append(key)
        for expanded in sorted(route.children):
            field = route.children[expanded]
            if not self._fits(field):
                continue
            key = expanded
            value = self._child_field_value(field)
            properties[key] = (
                self._always_list_group(value, field.cardinality)
                if self.always_list
                else self._observed_group(value, field.cardinality)
            )
            if field.cardinality.minimum > 0:
                required.append(key)
        if layout == "scalar":
            properties["$"] = self._native_scalar(
                route.content.scalar_contracts,
                may_be_empty=_may_be_empty(route.content.model),
            )
            required.append("$")
        elif layout == "mixed":
            properties["$"] = {"type": "string"}
        body: _Schema = {
            "type": "object",
            "properties": properties,
            "minProperties": 1,
            "x-pyxsd-layout": layout,
        }
        if required:
            body["required"] = sorted(set(required))
        if layout == "mixed":
            # A lone "$" is the childless leaf; the object form has at least
            # one attribute or child beside the grouped text.
            body["not"] = {"required": ["$"], "maxProperties": 1}
        self._native_openness(body, route)
        return body

    def _native_openness(self, body: _Schema, route: RouteContract) -> None:
        attribute_wildcards = self._attribute_wildcards(route)
        child_wildcards = self._child_wildcards(route)
        if not attribute_wildcards and not child_wildcards:
            body["additionalProperties"] = False
            return
        patterns: dict[str, _Schema] = {}
        if attribute_wildcards:
            values = _any_of(
                [self._wildcard_attribute_site(wildcard) for wildcard in attribute_wildcards]
            )
            patterns[_CLARK_ATTRIBUTE_PATTERN] = values
            self.unvalidated.add("relaxng-attribute-name-class")
        if child_wildcards:
            values = _any_of(
                [
                    self._native_wildcard_group(
                        self._wildcard_child_value(wildcard),
                        wildcard.cardinality,
                        tolerant=bool(route.children),
                    )
                    for wildcard in child_wildcards
                ]
            )
            patterns[_CLARK_PATTERN] = values
            self.unvalidated.add("relaxng-wildcard-names")
        body["patternProperties"] = patterns
        body["additionalProperties"] = False
        body["x-pyxsd-wildcard"] = True
        body["x-pyxsd-name-class"] = [
            _name_class_metadata(wildcard.name_class)
            for wildcard in (*attribute_wildcards, *child_wildcards)
        ]
        if any(wildcard.cardinality.minimum > 0 for wildcard in child_wildcards):
            self._require_property(body, _CLARK_PATTERN)

    def _native_wildcard_group(
        self, value: _Schema, cardinality: Cardinality, *, tolerant: bool
    ) -> _Schema:
        """A wildcard child value, tolerant of declared keys sharing the pattern."""
        if self.always_list:
            items = self._generic_ref() if tolerant else value
            return {
                "type": "array",
                "items": items,
                "minItems": max(1, cardinality.minimum),
            }
        if tolerant:
            # The open-name pattern also covers declared child keys, whose own
            # property schema stays authoritative.  The generic envelope is the
            # only sound shape broad enough to admit every declared child.
            return self._generic_ref()
        return self._observed_group(value, cardinality)

    def _native_generic_value(self) -> _Schema:
        """The native tree shape for one route-less open child."""
        self.unvalidated.add("relaxng-generic-route")
        self.unvalidated.add("relaxng-wildcard-names")
        self.unvalidated.add("relaxng-attribute-name-class")
        self.unvalidated.add("relaxng-mixed-order")
        leaf: _Schema = {"type": "string"} if not self.typed else self._scalar_union_ref()
        obj: _Schema = {
            "type": "object",
            "properties": {"$": {"type": "string"}},
            "patternProperties": {
                _CLARK_ATTRIBUTE_PATTERN: self._scalar_union_ref(),
                _CLARK_PATTERN: self._generic_ref(),
            },
            "additionalProperties": False,
            "minProperties": 1,
            "not": {"required": ["$"], "maxProperties": 1},
            "x-pyxsd-layout": "mixed",
            "x-pyxsd-wildcard": True,
        }
        branches: list[_Schema] = [
            leaf,
            obj,
            {"type": "array", "items": self._generic_ref()},
        ]
        if self.typed:
            branches.append({"type": "null"})
        return {"anyOf": branches, "x-pyxsd-wildcard": True}

    # ------------------------------------------------------------------
    # Parker
    # ------------------------------------------------------------------

    def _parker_body(self, route: RouteContract, *, generic: bool) -> _Schema:
        layout = _exporter_layout(route, generic)
        if layout == "empty":
            return {
                "type": "object",
                "maxProperties": 0,
                "additionalProperties": False,
                "x-pyxsd-layout": "empty",
            }
        if layout == "scalar":
            body = self._scalar_site(
                route.content.scalar_contracts,
                may_be_empty=_may_be_empty(route.content.model),
            )
            return {**body, "x-pyxsd-layout": "scalar"}
        if layout == "mixed":
            scalar: _Schema = (
                self._scalar_union_ref()
                if generic or route.generic_reason is not None
                else {"type": "string"}
            )
            branches: list[_Schema] = []
            if self._children_can_be_absent(route):
                branches.extend([{"type": "object", "maxProperties": 0}, scalar])
            branches.append(self._parker_object(route, layout))
            return {
                "anyOf": branches,
                "x-pyxsd-layout": "mixed",
                "x-pyxsd-mixed": True,
            }
        return self._parker_object(route, layout)

    def _parker_object(self, route: RouteContract, layout: str) -> _Schema:
        groups: dict[str, list[FieldContract]] = {}
        for expanded in sorted(route.children):
            field = route.children[expanded]
            if not self._fits(field):
                continue
            groups.setdefault(_local_name(expanded), []).append(field)
        child_wildcards = self._child_wildcards(route)
        declared_locals = set(groups)
        exclusive_minimums: dict[str, int] = {}
        wildcard_patterns: list[str] = []
        wildcard_property_required = False
        ambiguous_minimum = False
        for wildcard in child_wildcards:
            if wildcard.cardinality.minimum == 0:
                continue
            admitted = _admitted_names(wildcard.name_class)
            if admitted is None:
                if declared_locals:
                    ambiguous_minimum = True
                else:
                    wildcard_property_required = True
                if wildcard.cardinality.minimum > 1:
                    ambiguous_minimum = True
                continue
            locals_ = {_split_expanded(name)[1] for name in admitted}
            colliding = locals_ & declared_locals
            disjoint = locals_ - declared_locals
            if colliding:
                if disjoint or len(locals_) > 1:
                    # The wildcard may merge into a declared local or appear
                    # under a disjoint one; neither key is individually
                    # required without rejecting a valid mixed export.
                    ambiguous_minimum = True
                else:
                    only = next(iter(locals_))
                    exclusive_minimums[only] = (
                        exclusive_minimums.get(only, 0) + wildcard.cardinality.minimum
                    )
            elif disjoint:
                wildcard_patterns.append(
                    "^(?:" + "|".join(re.escape(local) for local in sorted(disjoint)) + ")$"
                )
                if wildcard.cardinality.minimum > 1:
                    ambiguous_minimum = True
            else:
                ambiguous_minimum = True
        if ambiguous_minimum:
            self.unvalidated.add("relaxng-wildcard-cardinality")
        properties: dict[str, Any] = {}
        required: list[str] = []
        for local in sorted(groups):
            entries = groups[local]
            overlapping = [
                wildcard
                for wildcard in child_wildcards
                if _name_class_admits_local(wildcard.name_class, local)
            ]
            values = _any_of(
                [
                    *(self._child_field_value(field) for field in entries),
                    *(self._wildcard_child_value(wildcard) for wildcard in overlapping),
                ]
            )
            extra_minimum = exclusive_minimums.get(local, 0)
            if overlapping:
                value = self._parker_wildcard_group(entries, overlapping, values, extra_minimum)
            elif len(entries) == 1:
                value = self._observed_group(values, entries[0].cardinality)
            else:
                value = self._parker_merged_group(entries, values)
            properties[local] = value
            if any(field.cardinality.minimum > 0 for field in entries) or extra_minimum:
                required.append(local)
        if layout == "mixed":
            self.unvalidated.add("relaxng-mixed-order")
            properties["$"] = {"type": "string"}
        if not properties and not child_wildcards:
            return {
                "type": "object",
                "maxProperties": 0,
                "additionalProperties": False,
                "x-pyxsd-layout": layout,
            }
        body: _Schema = {
            "type": "object",
            "properties": properties,
            "x-pyxsd-layout": layout,
        }
        if required:
            body["required"] = sorted(set(required))
        if child_wildcards:
            body["propertyNames"] = {
                "anyOf": [
                    {"const": "$"},
                    {"pattern": f"^{_NCNAME}$"},
                ]
                if layout == "mixed"
                else {"pattern": f"^{_NCNAME}$"}
            }
            body["additionalProperties"] = _any_of(
                [
                    self._observed_group(self._wildcard_child_value(wildcard), wildcard.cardinality)
                    for wildcard in child_wildcards
                ]
            )
            body["x-pyxsd-wildcard"] = True
            body["x-pyxsd-name-class"] = [
                _name_class_metadata(wildcard.name_class) for wildcard in child_wildcards
            ]
            if wildcard_property_required:
                self._require_property(body, f"^{_NCNAME}$")
            for pattern in wildcard_patterns:
                self._require_property(body, pattern)
            self.unvalidated.add("relaxng-wildcard-names")
        else:
            body["additionalProperties"] = False
        return body

    def _parker_wildcard_group(
        self,
        entries: list[FieldContract],
        wildcards: list[WildcardContract],
        values: _Schema,
        extra_minimum: int,
    ) -> _Schema:
        """A declared local that best-effort Parker merges with wildcard names."""
        self.unvalidated.add("relaxng-local-name-mapping")
        minimum = sum(field.cardinality.minimum for field in entries) + extra_minimum
        declared_maximum = (
            None
            if any(field.cardinality.maximum is None for field in entries)
            else sum(field.cardinality.maximum or 0 for field in entries)
        )
        maximum = (
            None
            if declared_maximum is None
            or any(wildcard.cardinality.maximum is None for wildcard in wildcards)
            else declared_maximum + sum(wildcard.cardinality.maximum or 0 for wildcard in wildcards)
        )
        branches: list[_Schema] = []
        if minimum <= 1:
            branches.append(values)
        array: _Schema = {
            "type": "array",
            "items": values,
            "minItems": max(2, minimum),
        }
        if maximum is not None:
            array["maxItems"] = maximum
        branches.append(array)
        return _any_of(branches)

    def _parker_merged_group(self, entries: list[FieldContract], values: _Schema) -> _Schema:
        """One local key shared by several expanded names."""
        self.unvalidated.add("relaxng-local-name-mapping")
        minimum = sum(field.cardinality.minimum for field in entries)
        maximum = (
            None
            if any(field.cardinality.maximum is None for field in entries)
            else sum(field.cardinality.maximum or 0 for field in entries)
        )
        branches: list[_Schema] = []
        if minimum <= 1:
            branches.append(values)
        array: _Schema = {
            "type": "array",
            "items": values,
            "minItems": max(2, minimum),
        }
        if maximum is not None:
            array["maxItems"] = maximum
        branches.append(array)
        return _any_of(branches)

    def _parker_generic_value(self) -> _Schema:
        """Parker's route-less mixed object or scalar."""
        self.unvalidated.add("relaxng-generic-route")
        self.unvalidated.add("relaxng-wildcard-names")
        self.unvalidated.add("relaxng-mixed-order")
        return {
            "anyOf": [
                {"type": "object", "maxProperties": 0},
                self._scalar_union_ref(),
                {
                    "type": "object",
                    "properties": {"$": {"type": "string"}},
                    "propertyNames": {"anyOf": [{"const": "$"}, {"pattern": f"^{_NCNAME}$"}]},
                    "additionalProperties": self._generic_ref(),
                    "x-pyxsd-wildcard": True,
                },
                {"type": "array", "items": self._generic_ref()},
            ],
            "x-pyxsd-wildcard": True,
        }

    # ------------------------------------------------------------------
    # BadgerFish
    # ------------------------------------------------------------------

    def _badgerfish_roots(self, element: str | None) -> _Schema:
        if element is not None:
            name, route = self._absorbed_root(element)
            return self._badgerfish_root_object(name, route)
        alternatives: list[_Schema] = []
        for route_id in sorted(self.plan.roots):
            route = self.plan.routes[route_id]
            names = _admitted_names(route.name_class)
            if names is None:
                alternatives.append(self._badgerfish_open_root(route))
            else:
                alternatives.extend(self._badgerfish_root_object(name, route) for name in names)
        if not alternatives:
            return {"not": {}}
        return _any_of(alternatives)

    def _badgerfish_root_object(self, name: str, route: RouteContract) -> _Schema:
        uri, local = _split_expanded(name)
        value = self._badgerfish_body(route, generic=False)
        body: _Schema = {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 1,
            "x-pyxsd-root": _root_metadata(name),
        }
        if uri is None:
            body["properties"] = {local: value}
            body["required"] = [local]
            body["additionalProperties"] = False
        else:
            body["propertyNames"] = {
                "type": "string",
                "pattern": f"^{_PREFIX}:{re.escape(local)}$",
            }
            body["additionalProperties"] = value
        return body

    def _badgerfish_open_root(self, route: RouteContract) -> _Schema:
        self.unvalidated.add("relaxng-wildcard-names")
        return {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 1,
            "propertyNames": {
                "type": "string",
                "pattern": _QNAME_PATTERN,
                "x-pyxsd-wildcard": True,
            },
            "additionalProperties": self._badgerfish_body(route, generic=False),
            "x-pyxsd-wildcard": True,
            "x-pyxsd-name-class": _name_class_metadata(route.name_class),
        }

    def _badgerfish_body(self, route: RouteContract, *, generic: bool) -> _Schema:
        layout = _exporter_layout(route, generic)
        if layout == "scalar":
            scalar = self._scalar_site(
                route.content.scalar_contracts,
                may_be_empty=_may_be_empty(route.content.model),
            )
            branches: list[_Schema] = []
            if self._can_be_empty(route):
                branches.append(scalar)
            if self._attributes_possible(route) or self._declarations_possible:
                branches.append(self._badgerfish_object(route, layout, scalar=scalar))
            return _any_of(branches)
        if layout == "mixed":
            mixed_scalar: _Schema = (
                self._scalar_union_ref()
                if generic or route.generic_reason is not None
                else {"type": "string"}
            )
            mixed_branches: list[_Schema] = []
            if self._can_be_empty(route):
                mixed_branches.append(mixed_scalar)
            mixed_branches.append(self._badgerfish_object(route, layout))
            body = _any_of(mixed_branches)
            body.setdefault("x-pyxsd-mixed", True)
            return body
        return self._badgerfish_object(route, layout)

    def _badgerfish_object(
        self,
        route: RouteContract,
        layout: str,
        *,
        scalar: _Schema | None = None,
    ) -> _Schema:
        properties: dict[str, Any] = {}
        required: list[str] = []
        patterns: dict[str, _Schema] = {}
        pattern_requirements: list[str] = []
        if self._declarations_possible:
            properties["@xmlns"] = self._xmlns_schema()
        attribute_groups: dict[tuple[bool, str], list[FieldContract]] = {}
        for expanded in sorted(route.attributes):
            field = route.attributes[expanded]
            attribute_groups.setdefault(
                (expanded.startswith("{"), _local_name(expanded)), []
            ).append(field)
        for (namespaced, local), entries in sorted(attribute_groups.items()):
            value = _any_of([self._attribute_site(field) for field in entries])
            if namespaced:
                pattern = f"^@{_PREFIX}:{re.escape(local)}$"
                patterns[pattern] = value
                if any(field.cardinality.minimum > 0 for field in entries):
                    pattern_requirements.append(pattern)
            else:
                properties["@" + local] = value
                if any(field.cardinality.minimum > 0 for field in entries):
                    required.append("@" + local)
        groups: dict[tuple[bool, str], list[FieldContract]] = {}
        for expanded in sorted(route.children):
            field = route.children[expanded]
            if self._fits(field):
                groups.setdefault((expanded.startswith("{"), _local_name(expanded)), []).append(
                    field
                )
        for (namespaced, local), entries in sorted(groups.items()):
            value = _any_of(
                [
                    self._badgerfish_array(self._child_field_value(field), field.cardinality)
                    if field.cardinality.repeatable
                    else self._child_field_value(field)
                    for field in entries
                ]
            )
            if namespaced:
                pattern = f"^{_PREFIX}:{re.escape(local)}$"
                patterns[pattern] = value
                if any(field.cardinality.minimum > 0 for field in entries):
                    pattern_requirements.append(pattern)
            else:
                properties[local] = value
                if any(field.cardinality.minimum > 0 for field in entries):
                    required.append(local)
        if layout == "scalar" and scalar is not None:
            properties["$"] = scalar
            required.append("$")
        elif layout == "mixed":
            self.unvalidated.add("relaxng-mixed-order")
            properties["$"] = {"type": "string"}
        for wildcard in self._attribute_wildcards(route):
            patterns[_LEXICAL_ATTRIBUTE_PATTERN] = self._wildcard_attribute_site(wildcard)
            self.unvalidated.add("relaxng-attribute-name-class")
        for wildcard in self._child_wildcards(route):
            value = self._wildcard_child_value(wildcard)
            if route.children:
                # The open-name pattern also covers declared keys, whose own
                # property schema stays authoritative.
                value = _any_of([value, self._generic_ref()])
            elif wildcard.cardinality.repeatable:
                value = self._badgerfish_array(value, wildcard.cardinality)
            patterns[_QNAME_PATTERN] = value
            self.unvalidated.add("relaxng-wildcard-names")
        body: _Schema = {
            "type": "object",
            "x-pyxsd-layout": layout,
        }
        if properties:
            body["properties"] = properties
        if patterns:
            body["patternProperties"] = patterns
        if required:
            body["required"] = sorted(set(required))
        body["additionalProperties"] = False
        if patterns:
            body["x-pyxsd-wildcard"] = True
            metadata = [
                _name_class_metadata(wildcard.name_class)
                for wildcard in (
                    *self._attribute_wildcards(route),
                    *self._child_wildcards(route),
                )
            ]
            if metadata:
                body["x-pyxsd-name-class"] = metadata
        for pattern in pattern_requirements:
            self._require_property(body, pattern)
        child_wildcards = self._child_wildcards(route)
        if any(wildcard.cardinality.minimum > 0 for wildcard in child_wildcards):
            self._require_property(body, _QNAME_PATTERN)
        if self._declarations_possible:
            body["x-pyxsd-prefix-scope"] = True
        return body

    @staticmethod
    def _badgerfish_array(value: _Schema, cardinality: Cardinality) -> _Schema:
        array: _Schema = {
            "type": "array",
            "items": value,
            "minItems": max(1, cardinality.minimum),
        }
        if cardinality.maximum is not None:
            array["maxItems"] = cardinality.maximum
        return array

    def _xmlns_schema(self) -> _Schema:
        return {
            "type": "object",
            "propertyNames": {"pattern": _NAMESPACE_PREFIX_PATTERN},
            "patternProperties": {
                "^$": {
                    "type": "string",
                    "not": {"enum": [_XML_NS, _XMLNS_NS]},
                },
                "^xml$": {"const": _XML_NS},
                "^(?!xml$)(?!$).+$": {
                    "type": "string",
                    "minLength": 1,
                    "not": {"enum": [_XML_NS, _XMLNS_NS]},
                },
            },
            "additionalProperties": False,
            "minProperties": 1,
        }

    def _badgerfish_generic_value(self) -> _Schema:
        """BadgerFish's route-less scalar or lexical object."""
        self.unvalidated.add("relaxng-generic-route")
        self.unvalidated.add("relaxng-wildcard-names")
        self.unvalidated.add("relaxng-attribute-name-class")
        self.unvalidated.add("relaxng-mixed-order")
        attrs: _Schema = {
            "type": "object",
            "minProperties": 1,
            "properties": {"@xmlns": self._xmlns_schema()},
            "propertyNames": {
                "anyOf": [
                    {"const": "@xmlns"},
                    {"pattern": _LEXICAL_ATTRIBUTE_PATTERN},
                ]
            },
            "additionalProperties": self._scalar_union_ref(),
        }
        obj: _Schema = {
            "type": "object",
            "properties": {"$": {"type": "string"}, "@xmlns": self._xmlns_schema()},
            "patternProperties": {
                _LEXICAL_ATTRIBUTE_PATTERN: self._scalar_union_ref(),
                _QNAME_PATTERN: self._generic_ref(),
            },
            "additionalProperties": False,
            "x-pyxsd-layout": "mixed",
            "x-pyxsd-wildcard": True,
        }
        return {
            "anyOf": [
                attrs,
                self._scalar_union_ref(),
                obj,
                {"type": "array", "items": self._generic_ref()},
            ],
            "x-pyxsd-wildcard": True,
        }

    # ------------------------------------------------------------------
    # JsonML
    # ------------------------------------------------------------------

    def _jsonml_roots(self, element: str | None) -> _Schema:
        if element is not None:
            name, route = self._absorbed_root(element)
            body = self._jsonml_node(
                route,
                _finite_tag_schema(name),
                generic=False,
            )
            body["x-pyxsd-root"] = _root_metadata(name)
            return body
        alternatives: list[_Schema] = []
        for route_id in sorted(self.plan.roots):
            route = self.plan.routes[route_id]
            names = _admitted_names(route.name_class)
            if names is None:
                alternatives.append(self._jsonml_open_root(route))
            else:
                for name in names:
                    body = self._jsonml_node(
                        route,
                        _finite_tag_schema(name),
                        generic=False,
                    )
                    body["x-pyxsd-root"] = _root_metadata(name)
                    alternatives.append(body)
        if not alternatives:
            return {"not": {}}
        return _any_of(alternatives)

    def _jsonml_open_root(self, route: RouteContract) -> _Schema:
        self.unvalidated.add("relaxng-wildcard-names")
        body = self._jsonml_node(route, _open_tag_schema(), generic=False)
        body["x-pyxsd-wildcard"] = True
        body["x-pyxsd-name-class"] = _name_class_metadata(route.name_class)
        return body

    def _route_tag_schema(self, route: RouteContract) -> _Schema:
        names = _admitted_names(route.name_class)
        if names is None:
            return _open_tag_schema()
        return _any_of([_finite_tag_schema(name) for name in names])

    def _jsonml_node(
        self,
        route: RouteContract,
        tag: _Schema,
        *,
        generic: bool,
    ) -> _Schema:
        layout = _exporter_layout(route, generic)
        attributes, _required_attributes, attributes_possible, required = self._jsonml_attributes(
            route
        )
        content = self._jsonml_content(route, layout)
        branches: list[_Schema] = []
        if not required:
            branches.append(self._jsonml_branch(tag, None, content, layout))
        if attributes_possible:
            branches.append(self._jsonml_branch(tag, attributes, content, layout))
        body: _Schema = {
            "type": "array",
            "minItems": 1,
            "anyOf": branches,
            "x-pyxsd-layout": layout,
        }
        if layout == "mixed":
            body["x-pyxsd-mixed"] = True
        contains = self._jsonml_contains(route)
        if contains:
            body["allOf"] = contains
        self._jsonml_disclosures(route, layout)
        return body

    def _jsonml_branch(
        self,
        tag: _Schema,
        attributes: _Schema | None,
        content: _Schema | None,
        layout: str,
    ) -> _Schema:
        if layout == "scalar":
            assert content is not None
            if attributes is None:
                return {
                    "prefixItems": [tag, content],
                    "minItems": 2,
                    "maxItems": 2,
                    "items": False,
                }
            return {
                "prefixItems": [tag, attributes, content],
                "minItems": 3,
                "maxItems": 3,
                "items": False,
            }
        if layout == "empty":
            assert content is not None
            if attributes is None:
                return {
                    "prefixItems": [tag],
                    "items": content,
                    "maxItems": 2,
                }
            return {
                "prefixItems": [tag, attributes],
                "items": content,
                "minItems": 2,
                "maxItems": 3,
            }
        if attributes is None:
            return {
                "prefixItems": [tag],
                "items": content if content is not None else False,
            }
        return {
            "prefixItems": [tag, attributes],
            "items": content if content is not None else False,
            "minItems": 2,
        }

    def _jsonml_content(self, route: RouteContract, layout: str) -> _Schema | None:
        if layout == "scalar":
            return self._scalar_site(
                route.content.scalar_contracts,
                may_be_empty=_may_be_empty(route.content.model),
            )
        if layout == "mixed":
            branches: list[_Schema] = [{"type": "string"}]
        elif layout == "empty":
            branches = [{"type": "string", "pattern": "^\\s*$"}]
        else:
            branches = [{"type": "string", "pattern": "^\\s*$"}]
        for expanded in sorted(route.children):
            field = route.children[expanded]
            if not self._fits(field):
                continue
            branches.append(self._child_field_value(field))
        for wildcard in self._child_wildcards(route):
            branches.append(self._wildcard_child_value(wildcard))
        return _any_of(branches)

    def _jsonml_contains(self, route: RouteContract) -> list[_Schema]:
        fields = [field for field in route.children.values() if self._fits(field)]
        if any(field.child_route_id is None for field in fields):
            # A route-less child has no tag identity to count against.  One
            # aggregate child count stays sound; distinct contains maxima
            # would count every generic array against every field.
            minimum = sum(field.cardinality.minimum for field in fields)
            maximum = (
                None
                if any(field.cardinality.maximum is None for field in fields)
                else sum(field.cardinality.maximum or 0 for field in fields)
            )
            if minimum == 0 and maximum is None:
                return []
            aggregate: _Schema = {
                "contains": self._generic_ref(),
                "minContains": minimum,
            }
            if maximum is not None:
                aggregate["maxContains"] = maximum
            return [aggregate]
        groups: dict[tuple[bool, str], list[FieldContract]] = {}
        for expanded in sorted(route.children):
            field = route.children[expanded]
            if not self._fits(field):
                continue
            groups.setdefault((expanded.startswith("{"), _local_name(expanded)), []).append(field)
        child_wildcards = self._child_wildcards(route)
        declared_locals = {
            _local_name(expanded)
            for expanded in route.children
            if self._fits(route.children[expanded])
        }
        overlapping_locals = {
            local
            for local in declared_locals
            if any(
                _name_class_admits_local(wildcard.name_class, local) for wildcard in child_wildcards
            )
        }
        if overlapping_locals:
            # A lexical tag pattern cannot distinguish a wildcard name from a
            # declared same-local name, so counts over either set would falsely
            # reject a valid merged array.
            self.unvalidated.add("relaxng-wildcard-cardinality")
        overlapping_wildcards: set[int] = set()
        for index, wildcard in enumerate(child_wildcards):
            for other in child_wildcards[index + 1 :]:
                if _wildcards_overlap(wildcard, other):
                    overlapping_wildcards.add(id(wildcard))
                    overlapping_wildcards.add(id(other))
                    if any(
                        item.cardinality.minimum > 0 or item.cardinality.maximum is not None
                        for item in (wildcard, other)
                    ):
                        self.unvalidated.add("relaxng-wildcard-cardinality")
        constraints: list[_Schema] = []
        for wildcard in child_wildcards:
            if id(wildcard) in overlapping_wildcards:
                continue
            if wildcard.cardinality.minimum == 0 and wildcard.cardinality.maximum is None:
                continue
            if any(
                _name_class_admits_local(wildcard.name_class, local) for local in declared_locals
            ):
                continue
            wildcard_constraint: _Schema = {
                "contains": self._jsonml_wildcard_contains_item(wildcard),
                "minContains": wildcard.cardinality.minimum,
            }
            if wildcard.cardinality.maximum is not None:
                wildcard_constraint["maxContains"] = wildcard.cardinality.maximum
            constraints.append(wildcard_constraint)
        for (_namespaced, local), entries in groups.items():
            minimum = sum(field.cardinality.minimum for field in entries)
            maximum = (
                None
                if any(field.cardinality.maximum is None for field in entries)
                else sum(field.cardinality.maximum or 0 for field in entries)
            )
            if minimum == 0 and maximum is None:
                continue
            constraint: _Schema = {
                "contains": _any_of([self._jsonml_contains_item(field) for field in entries]),
                "minContains": minimum,
            }
            if maximum is not None and local not in overlapping_locals:
                constraint["maxContains"] = maximum
            constraints.append(constraint)
        return constraints

    def _jsonml_contains_item(self, field: FieldContract) -> _Schema:
        """One named child array, pinned to its own finite tag."""
        assert field.expanded_name is not None
        return self._jsonml_tagged_array(
            self._child_field_value(field),
            _finite_tag_schema(field.expanded_name),
        )

    def _jsonml_wildcard_contains_item(self, wildcard: WildcardContract) -> _Schema:
        """One wildcard child array, pinned to the wildcard's finite tags."""
        return self._jsonml_tagged_array(
            self._wildcard_child_value(wildcard),
            _wildcard_tag_schema(wildcard.name_class),
        )

    @staticmethod
    def _jsonml_tagged_array(value: _Schema, tag: _Schema) -> _Schema:
        return {
            "allOf": [
                value,
                {"type": "array", "prefixItems": [tag]},
            ]
        }

    def _jsonml_attributes(self, route: RouteContract) -> tuple[_Schema, list[str], bool, bool]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        patterns: dict[str, _Schema] = {}
        requirements: list[str] = []
        groups: dict[tuple[bool, str], list[FieldContract]] = {}
        for expanded in sorted(route.attributes):
            groups.setdefault((expanded.startswith("{"), _local_name(expanded)), []).append(
                route.attributes[expanded]
            )
        for (namespaced, local), entries in sorted(groups.items()):
            value = _any_of([self._attribute_site(field) for field in entries])
            if namespaced:
                pattern = f"^{_PREFIX}:{re.escape(local)}$"
                patterns[pattern] = value
                if any(field.cardinality.minimum > 0 for field in entries):
                    requirements.append(pattern)
            else:
                properties[local] = value
                if any(field.cardinality.minimum > 0 for field in entries):
                    required.append(local)
        if self._declarations_possible:
            properties["xmlns"] = {
                "type": "string",
                "not": {"enum": [_XML_NS, _XMLNS_NS]},
            }
            patterns["^xmlns:xml$"] = {"const": _XML_NS}
            patterns[f"^xmlns:(?!xml$)(?!xmlns$){_NCNAME}$"] = {
                "type": "string",
                "minLength": 1,
                "not": {"enum": [_XML_NS, _XMLNS_NS]},
            }
        for wildcard in self._attribute_wildcards(route):
            patterns[_JSONML_ATTRIBUTE_PATTERN] = self._wildcard_attribute_site(wildcard)
            self.unvalidated.add("relaxng-attribute-name-class")
        possible = bool(properties or patterns)
        body: _Schema = {
            "type": "object",
            "minProperties": 1,
        }
        if properties:
            body["properties"] = properties
        if patterns:
            body["patternProperties"] = patterns
        if required:
            body["required"] = sorted(set(required))
        if requirements:
            body["allOf"] = [
                {"not": {"propertyNames": {"not": {"pattern": pattern}}}}
                for pattern in requirements
            ]
        body["additionalProperties"] = False
        if self._attribute_wildcards(route):
            body["x-pyxsd-wildcard"] = True
            body["x-pyxsd-name-class"] = [
                _name_class_metadata(wildcard.name_class)
                for wildcard in self._attribute_wildcards(route)
            ]
        return body, required, possible, bool(required or requirements)

    def _jsonml_disclosures(self, route: RouteContract, layout: str) -> None:
        if self._child_wildcards(route):
            self.unvalidated.add("relaxng-wildcard-names")
        if layout in ("record", "sequence", "repeated"):
            self.unvalidated.add("relaxng-particle-order")
        if any(_contracts_include_list(contract) for contract in route.content.scalar_contracts):
            self.unvalidated.add("relaxng-jsonml-scalar-array-ambiguity")
        for field in route.children.values():
            route_contract = (
                self.plan.routes.get(field.child_route_id)
                if field.child_route_id is not None
                else None
            )
            if route_contract is not None and any(
                _contracts_include_list(contract)
                for contract in route_contract.content.scalar_contracts
            ):
                self.unvalidated.add("relaxng-jsonml-scalar-array-ambiguity")

    def _jsonml_generic_value(self) -> _Schema:
        """A route-less ordered JsonML child array."""
        self.unvalidated.add("relaxng-generic-route")
        self.unvalidated.add("relaxng-wildcard-names")
        self.unvalidated.add("relaxng-mixed-order")
        tag = _open_tag_schema()
        attrs, _required, _possible = self._jsonml_attributes_for_generic()
        content = {
            "anyOf": [
                {"type": "string"},
                self._generic_ref(),
            ]
        }
        branches = [
            {
                "prefixItems": [tag],
                "items": content,
                "minItems": 1,
            },
            {
                "prefixItems": [tag, attrs],
                "items": content,
                "minItems": 2,
            },
        ]
        return {
            "type": "array",
            "minItems": 1,
            "anyOf": branches,
            "x-pyxsd-layout": "mixed",
            "x-pyxsd-mixed": True,
            "x-pyxsd-wildcard": True,
        }

    def _jsonml_attributes_for_generic(self) -> tuple[_Schema, list[str], bool]:
        self.unvalidated.add("relaxng-attribute-name-class")
        return (
            {
                "type": "object",
                "minProperties": 1,
                "propertyNames": {
                    "anyOf": [
                        {"pattern": _JSONML_ATTRIBUTE_PATTERN},
                        {"pattern": f"^(?:xmlns|xmlns:(?!xmlns$){_NCNAME})$"},
                    ]
                },
                "properties": {
                    "xmlns": {
                        "type": "string",
                        "not": {"enum": [_XML_NS, _XMLNS_NS]},
                    }
                },
                "patternProperties": {
                    "^xmlns:xml$": {"const": _XML_NS},
                    f"^xmlns:(?!xml$)(?!xmlns$){_NCNAME}$": {
                        "type": "string",
                        "minLength": 1,
                        "not": {"enum": [_XML_NS, _XMLNS_NS]},
                    },
                },
                "additionalProperties": self._scalar_union_ref(),
            },
            [],
            True,
        )


def _root_metadata(name: str) -> Any:
    """The symbolic root metadata for one expanded name."""
    uri, local = _split_expanded(name)
    return _name_class_metadata(Name(uri, local))


def _local_name(name: str) -> str:
    return _split_expanded(name)[1]


def _finite_tag_schema(name: str) -> _Schema:
    """The exact or prefix-symbolic tag for one admitted expanded name."""
    uri, local = _split_expanded(name)
    if uri is None:
        return {"const": local}
    return {"type": "string", "pattern": f"^{_PREFIX}:{re.escape(local)}$"}


def _open_tag_schema() -> _Schema:
    return {
        "type": "string",
        "pattern": _QNAME_PATTERN,
        "x-pyxsd-wildcard": True,
    }


def _name_class_admits_local(name_class: NameClass, local: str) -> bool:
    """Whether a name class can admit a name with this local name."""
    names = _admitted_names(name_class)
    if names is None:
        return True
    return any(_split_expanded(name)[1] == local for name in names)


def _wildcards_overlap(left: WildcardContract, right: WildcardContract) -> bool:
    """Whether two wildcard classes can share a lexical local name."""
    left_names = _admitted_names(left.name_class)
    right_names = _admitted_names(right.name_class)
    if left_names is None or right_names is None:
        return True
    left_locals = {_split_expanded(name)[1] for name in left_names}
    right_locals = {_split_expanded(name)[1] for name in right_names}
    return bool(left_locals & right_locals)


def _wildcard_tag_schema(name_class: NameClass) -> _Schema:
    """A symbolic tag schema for the names one wildcard class admits."""
    names = _admitted_names(name_class)
    if names is None:
        return _open_tag_schema()
    if not names:
        return {"not": {}}
    return _any_of([_finite_tag_schema(name) for name in names])


def _pattern_has_custom_datatype(pattern: Pattern, seen: set[int]) -> bool:
    """Whether a list pattern contains a custom datatype token."""
    if id(pattern) in seen:
        return False
    seen.add(id(pattern))
    if isinstance(pattern, (Data, DataExcept, Value)):
        return scalar_contract(pattern).library_uri not in (
            BUILTIN_LIBRARY,
            XSD_LIBRARY,
        )
    if isinstance(pattern, Ref):
        return _pattern_has_custom_datatype(pattern.target, seen)
    if isinstance(pattern, List):
        return _pattern_has_custom_datatype(pattern.pattern, seen)
    if isinstance(pattern, (Choice, Group)):
        return _pattern_has_custom_datatype(pattern.first, seen) or _pattern_has_custom_datatype(
            pattern.second, seen
        )
    if isinstance(pattern, OneOrMore):
        return _pattern_has_custom_datatype(pattern.pattern, seen)
    return False


def _contract_has_custom_library(contract: ScalarContract, plan: ProjectionPlan) -> bool:
    if contract.kind == "atomic":
        return contract.library_uri not in (BUILTIN_LIBRARY, XSD_LIBRARY)
    if contract.kind != "list":
        return False
    pattern = plan.encoding_patterns[contract.pattern_identity]
    assert isinstance(pattern, List)
    return _pattern_has_custom_datatype(pattern, set())


def _plan_has_custom_scalars(plan: ProjectionPlan) -> bool:
    """Whether any scalar site can carry custom-datatype output."""
    for route in plan.routes.values():
        if any(
            _contract_has_custom_library(contract, plan)
            for contract in route.content.scalar_contracts
        ):
            return True
        for field in (*route.attributes.values(), *route.children.values()):
            if any(
                _contract_has_custom_library(contract, plan) for contract in field.scalar_contracts
            ):
                return True
        for wildcard in route.wildcards:
            if any(
                _contract_has_custom_library(contract, plan)
                for contract in wildcard.scalar_contracts
            ):
                return True
    return False


def _plan_has_namespaced_names(plan: ProjectionPlan) -> bool:
    """Whether any element or attribute name in the plan carries a namespace."""
    for route in plan.routes.values():
        if _name_class_namespaced(route.name_class):
            return True
        if any(_split_expanded(name)[0] for name in (*route.attributes, *route.children)):
            return True
        if any(_name_class_namespaced(wildcard.name_class) for wildcard in route.wildcards):
            return True
    return False


def _plan_has_unenforced_wildcard_maximum(plan: ProjectionPlan, format: str) -> bool:
    """Whether a finite wildcard maximum cannot be counted across its keys."""
    for route in plan.routes.values():
        declared_locals = {
            _local_name(expanded)
            for expanded, field in route.children.items()
            if field.cardinality.maximum != 0
        }
        generic_fields = any(
            field.child_route_id is None
            for field in route.children.values()
            if field.cardinality.maximum != 0
        )
        child_wildcards = [
            wildcard
            for wildcard in route.wildcards
            if wildcard.role == "child" and wildcard.cardinality.maximum not in (None, 0)
        ]
        for index, wildcard in enumerate(child_wildcards):
            for other in child_wildcards[index + 1 :]:
                if _wildcards_overlap(wildcard, other) and any(
                    item.cardinality.maximum is not None for item in (wildcard, other)
                ):
                    return True
        for wildcard in route.wildcards:
            maximum = wildcard.cardinality.maximum
            if maximum is None or maximum == 0:
                continue
            if format != "jsonml":
                if format == "parker" and wildcard.role == "attribute":
                    continue
                return True
            if wildcard.role == "attribute" or generic_fields:
                return True
            if any(
                _name_class_admits_local(wildcard.name_class, local) for local in declared_locals
            ):
                return True
    return False


def _plan_has_attributes(plan: ProjectionPlan) -> bool:
    for route in plan.routes.values():
        if route.attributes:
            return True
        if any(wildcard.role == "attribute" for wildcard in route.wildcards):
            return True
    return False


def _plan_has_declarations(plan: ProjectionPlan) -> bool:
    """Whether any efficient lexical export can need a namespace declaration."""
    for route in plan.routes.values():
        if _name_class_namespaced(route.name_class):
            return True
        if any(_split_expanded(name)[0] for name in (*route.attributes, *route.children)):
            return True
        if any(_name_class_namespaced(wildcard.name_class) for wildcard in route.wildcards):
            return True
        if any(
            _contracts_include_qname(contract, plan) for contract in route.content.scalar_contracts
        ):
            return True
        for field in (*route.attributes.values(), *route.children.values()):
            if any(_contracts_include_qname(contract, plan) for contract in field.scalar_contracts):
                return True
        for wildcard in route.wildcards:
            if any(
                _contracts_include_qname(contract, plan) for contract in wildcard.scalar_contracts
            ):
                return True
    return False


def _name_class_namespaced(name_class: NameClass) -> bool:
    if isinstance(name_class, AnyName):
        # An open name can admit a namespaced name, which needs a prefix.
        return True
    if isinstance(name_class, Name):
        return name_class.uri is not None
    if isinstance(name_class, NsName):
        return bool(name_class.uri)
    if isinstance(name_class, NameChoice):
        return any(_name_class_namespaced(item) for item in name_class.alternatives)
    if isinstance(name_class, ExceptName):
        return _name_class_namespaced(name_class.base) or _name_class_namespaced(
            name_class.excluded
        )
    return False


def _contracts_include_qname(contract: ScalarContract, plan: ProjectionPlan) -> bool:
    if contract.kind in ("atomic", "list") and contract.library_uri not in (
        BUILTIN_LIBRARY,
        XSD_LIBRARY,
    ):
        # A custom decoder may return a QName or a list of QNames, which the
        # conventional exporters spell with a declared prefix.
        return True
    if contract.kind == "atomic":
        return contract.type_name in _XSD_QNAME_TYPES
    if contract.kind != "list":
        return False
    pattern = plan.encoding_patterns[contract.pattern_identity]
    assert isinstance(pattern, List)
    tokens = _token_patterns(pattern)
    if tokens is None:
        # An interpreted list keeps lexical text; any QName token can still
        # bring an authored prefix into the format's declaration scope.
        return True
    for token in tokens[1]:
        token_contract = scalar_contract(token)
        if token_contract.library_uri not in (BUILTIN_LIBRARY, XSD_LIBRARY):
            return True
        if token_contract.type_name in _XSD_QNAME_TYPES:
            return True
    return False


def _contracts_include_list(contract: ScalarContract) -> bool:
    return contract.kind == "list" or (
        contract.kind == "atomic" and contract.type_name in _XSD_LIST_VALUED_TYPES
    )


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
