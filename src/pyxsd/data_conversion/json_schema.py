"""Draft 2020-12 JSON Schema projection for the ``pyxsd-json`` profile.

The generator walks compiled schema components, never sample documents:
:func:`~pyxsd.data_conversion.layouts.plan_layout` selects each type's
layout, the shared scalar contract selects each value schema, and the
element/type classes produced by compilation become deterministic,
collision-safe ``$defs``/``$ref`` entries. The result describes the JSON
:meth:`~pyxsd.schema.Schema.from_data` consumes and
``Document.to_data(format="pyxsd-json")`` produces; it never claims full
XSD validation. Rules JSON Schema cannot carry -- particle order, wildcard
namespaces, identity constraints, assertions, and untranslatable pattern
dialects -- are disclosed through the ``x-pyxsd-validation`` annotation.
"""

from __future__ import annotations

import decimal
import math
import re
from typing import Any

from pyxsd import xsd_data_types as _xsd_data_types
from pyxsd.data_conversion.contracts import (
    Fidelity,
    FormatName,
    validate_fidelity,
    validate_format,
)
from pyxsd.data_conversion.layouts import (
    Layout,
    LayoutPlan,
    _compiled_model,
    _is_schema_base,
    _model_facts,
    _possible_element_names,
    plan_layout,
)
from pyxsd.data_conversion.names import eqname_to_expanded, expanded_to_eqname
from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import XSD_NS
from pyxsd.xsd_data_types import (
    AnyType,
    Boolean,
    QName,
    String,
    TypeList,
    XsdDataType,
    XsdList,
    _ListString,
)
from pyxsd.xsi import XSI_NAMESPACE

#: Reserved property key carrying an explicit actual type (profile v1).
TYPE_KEY = "@xsi:type"

#: Reserved property key for simple content and the nil marker.
CONTENT_KEY = "#content"

#: The generated documents' dialect and profile markers.
_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

#: Attributes ``xsi`` allows on any element and the exporter keeps.
_XSI_LOCATION_LOCALS = ("schemaLocation", "noNamespaceSchemaLocation")

#: Canonical fixed-point decimal strings (``f"{value:f}"``, never float).
_DECIMAL_PATTERN = "^-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?$"

#: The profile's EQName values: ``Q{uri}local`` or a bare local name.
_EQNAME_PATTERN = "^(?:Q\\{[\\s\\S]*\\})?[^\\s:]+$"

#: ``str(int)`` spellings an integer union member can export as.
_INTEGER_LEXICAL_PATTERN = "^[+-]?[0-9]+$"

#: A marker object for any explicit actual type (undetermined declarations).
_MARKER_OBJECT: dict[str, Any] = {
    "type": "object",
    "required": [TYPE_KEY],
    "properties": {TYPE_KEY: {"type": "string"}},
}

#: The nil marker item ordered layouts use after their attribute maps.
_NIL_MARKER: dict[str, Any] = {
    "type": "object",
    "required": [CONTENT_KEY],
    "properties": {CONTENT_KEY: {"type": "null"}},
    "additionalProperties": False,
}

# Python regex features whose ECMAScript meaning is not proven identical;
# a pattern using any of them is disclosed rather than translated. ``\U``
# is a Python-only eight-digit escape, ``\Z``/``\A``/``\b`` anchors and
# boundaries, ``\p``/``\P`` property classes, and digits/``\g``/``\K``
# backreferences.
_UNSAFE_ECMASCRIPT = re.compile(r"\(\?|\\[1-9gKpPbBAZU]|\[:|[*+?}]\+")

# The anchor elementpath's translated patterns carry: ``^(?:inner)$`` with
# a Python-only newline guard. Only that exact shape is considered for
# emission; anything else is disclosed as an untranslatable dialect.
_PYTHON_ANCHOR_SUFFIX = ")$(?!\\n\\Z)"


def build_json_schema(
    schema: Any,
    *,
    format: FormatName = "pyxsd-json",
    element: str | None = None,
    fidelity: Fidelity = "strict",
    typed: bool = True,
    always_list: bool = False,
) -> dict[str, object]:
    """Return the Draft 2020-12 schema for *schema*'s ``pyxsd-json`` contract.

    ``element`` accepts the same local, Clark, and EQName root-selection
    spellings as :meth:`~pyxsd.schema.Schema.from_data`; ``None`` describes
    every concrete global root with ``anyOf``. ``typed`` and ``always_list``
    are native-only options and must keep their defaults here. ``fidelity``
    is accepted for interface parity; the ``pyxsd-json`` projection is
    total over supported compiled schemas, so both modes generate the same
    sound shape and neither claims full XSD validation.

    Raises ``ValueError`` for an unknown format or fidelity, for native-only
    options with non-default values, and ``NotImplementedError`` for the
    formats whose projections are not implemented yet.
    """
    requested_format = validate_format(format)
    requested_fidelity = validate_fidelity(fidelity)

    if requested_format != "pyxsd":
        if typed is not True:
            raise ValueError("typed is a native-only JSON Schema option")
        if always_list is not False:
            raise ValueError("always_list is a native-only JSON Schema option")

    if requested_format != "pyxsd-json":
        raise NotImplementedError(
            f"JSON Schema generation for the {requested_format!r} format is not implemented yet"
        )

    builder = _SchemaBuilder(schema, requested_fidelity)
    return builder.build(element)


class _SchemaBuilder:
    """One ``pyxsd-json`` projection run over a compiled schema."""

    def __init__(self, schema: Any, fidelity: Fidelity) -> None:
        self.schema = schema
        self.fidelity = fidelity
        self.defs: dict[str, dict[str, Any]] = {}
        self.unvalidated: set[str] = set()
        self._def_for: dict[tuple[int, bool, bool], str] = {}
        self._used_keys: set[str] = set()
        self._element_cache: dict[int, dict[str, Any]] = {}
        self._marker_cache: dict[int, list[type]] = {}
        self._attribute_cache: dict[int, dict[str, Any]] = {}
        self._scalar_cache: dict[int, dict[str, Any]] = {}
        self._schema_named: list[type] | None = None
        self._named: list[type] | None = None
        self._is_valid_xsi_type: Any = None
        # The binder makes the same assumption: every compiled schema
        # generates the "schema" class that owns the root elements.
        self._globals: dict[str, Any] = {
            descriptor.expandedInstanceName(): descriptor
            for descriptor in schema.classes["schema"]()._getElements()
        }

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def build(self, element: str | None) -> dict[str, object]:
        """Build the envelope and the root schema for one selection."""
        roots = self._root_alternatives(element)
        result: dict[str, Any] = {
            "$schema": _SCHEMA_URI,
            "x-pyxsd-format": "pyxsd-json",
            "x-pyxsd-profile-version": 1,
        }
        if roots:
            alternatives = [self._root_object(eqname, descriptor) for eqname, descriptor in roots]
            if len(alternatives) == 1:
                result.update(alternatives[0])
            else:
                result["anyOf"] = alternatives
        else:
            # No concrete global root exists, so no document can be exported.
            result["not"] = {}
        result["x-pyxsd-validation"] = {
            "level": "structural",
            "unvalidated": sorted(self.unvalidated),
        }
        if self.defs:
            result["$defs"] = self.defs
        return result

    # ------------------------------------------------------------------
    # Root selection
    # ------------------------------------------------------------------

    def _root_alternatives(self, element: str | None) -> list[tuple[str, Any | None]]:
        """The ``(EQName, declaration)`` roots one selection allows."""
        globals_ = self._globals
        if element is None:
            return [
                (expanded_to_eqname(name), descriptor)
                for name, descriptor in sorted(globals_.items())
                if not descriptor.isAbstract()
            ]
        if element.startswith("Q{") or element.startswith("{"):
            expanded = self._expanded_selection(element)
            descriptor = globals_.get(expanded)
            if descriptor is None:
                return [(expanded_to_eqname(expanded), None)]
            return self._declaration_roots(descriptor)
        expanded = self._expanded_selection(element)
        matches = [descriptor for descriptor in globals_.values() if descriptor.name == element]
        if len(matches) > 1:
            raise PyXSDError(
                "name-ambiguous at '': "
                f"the local root name {element!r} matches more than one "
                "global element declaration"
            )
        if len(matches) == 1:
            return self._declaration_roots(matches[0])
        return [(expanded_to_eqname(expanded), None)]

    @staticmethod
    def _declaration_roots(descriptor: Any) -> list[tuple[str, Any | None]]:
        """The head itself plus its substitution members, as import allows."""
        return [
            (expanded_to_eqname(name), decl) for name, decl in _possible_element_names(descriptor)
        ]

    @staticmethod
    def _expanded_selection(element: str) -> str:
        """Validate a local, Clark, or EQName selection into Clark form."""
        if element.startswith("Q{"):
            return eqname_to_expanded(element)
        # A Clark spelling and a bare local are both validated through
        # their EQName form, exactly as the importer's selection does.
        return eqname_to_expanded(expanded_to_eqname(element))

    def _root_object(self, eqname: str, descriptor: Any | None) -> dict[str, Any]:
        """The single-entry root object carrying one root's value schema."""
        value: dict[str, Any]
        if descriptor is None:
            value = self._general_value(False)
        else:
            value = self._element_value_schema(descriptor)
        return {
            "type": "object",
            "properties": {eqname: value},
            "required": [eqname],
            "additionalProperties": False,
            "minProperties": 1,
            "maxProperties": 1,
            "x-pyxsd-qname": eqname,
        }

    # ------------------------------------------------------------------
    # Element values
    # ------------------------------------------------------------------

    def _element_value_schema(self, descriptor: Any) -> dict[str, Any]:
        """The complete value schema for one element declaration.

        The branches cover the declared type, the types its conditional
        alternatives can select, and every type a valid ``xsi:type`` can
        name -- the same relation the binder enforces, so every exported
        document validates.
        """
        cache_key = id(descriptor)
        cached = self._element_cache.get(cache_key)
        if cached is not None:
            return cached

        declared = descriptor.getType()
        nillable = bool(descriptor.isNillable())
        if getattr(descriptor, "identities", None):
            self.unvalidated.add("xsd-identity-constraints")

        cta_types: list[type] = []
        for alternative in getattr(descriptor, "compiledAlternatives", None) or ():
            type_class = getattr(alternative, "type_class", None)
            if isinstance(type_class, type):
                cta_types.append(type_class)
        if cta_types:
            self.unvalidated.add("xsd-conditional-type")

        branches: list[dict[str, Any]] = []
        undetermined = declared is None or self._undetermined(declared)
        if undetermined:
            branches.append(self._general_value(nillable))
            branches.append(_MARKER_OBJECT)
        else:
            branches.append(self._type_schema(declared, explicit=False, nillable=nillable))

        for type_class in cta_types:
            if type_class is declared:
                continue
            branches.append(self._type_schema(type_class, explicit=False, nillable=nillable))
        if not undetermined:
            generated_targets, builtin_targets = self._marker_targets(declared, cta_types)
            for type_class in generated_targets:
                branches.append(self._type_schema(type_class, explicit=True, nillable=nillable))
            if builtin_targets:
                # Builtin derivation targets (the numeric and string
                # families an ``xsi:type`` may name) get one compact,
                # enum-guarded branch instead of a definition per type.
                branches.append(self._generic_marker_branch(builtin_targets))

        value = branches[0] if len(branches) == 1 else {"anyOf": branches}
        self._element_cache[cache_key] = value
        return value

    def _marker_targets(
        self, declared: Any, cta_types: list[type]
    ) -> tuple[list[type], list[type]]:
        """The ``xsi:type`` targets for *declared*, split by origin.

        The first list holds this schema's own named types, which get their
        own precise definitions; the second holds built-in datatypes, which
        share one compact branch. Conditional alternatives can also be
        re-marked by ``xsi:type``, so each alternative's type contributes
        its own derivations (and is itself a marker target).
        """
        schema_ids = {id(type_class) for type_class in self._schema_types()}
        generated: dict[int, type] = {}
        builtins: dict[int, type] = {}
        for base in (declared, *cta_types):
            for type_class in self._marker_alternatives_for_base(
                base, skip_identity=base is declared
            ):
                if id(type_class) in schema_ids:
                    generated.setdefault(id(type_class), type_class)
                else:
                    builtins.setdefault(id(type_class), type_class)
        return (
            sorted(generated.values(), key=self._type_expanded_name),
            sorted(builtins.values(), key=self._type_expanded_name),
        )

    def _marker_alternatives_for_base(self, base: Any, *, skip_identity: bool) -> list[type]:
        """Named types the binder accepts as an ``xsi:type`` for *base*."""
        cache_key = id(base)
        cached = self._marker_cache.get(cache_key)
        if cached is not None:
            return cached
        if self._is_valid_xsi_type is None:
            # Deferred: importing the derivation module before the parser
            # stack is loaded re-enters the element-representative cycle.
            from pyxsd.derivation import is_valid_xsi_type

            self._is_valid_xsi_type = is_valid_xsi_type
        result = [
            type_class
            for type_class in self._named_types()
            if (not skip_identity or type_class is not base)
            and self._is_valid_xsi_type(type_class, base, None) is None
        ]
        self._marker_cache[cache_key] = result
        return result

    def _generic_marker_branch(self, builtin_targets: list[type]) -> dict[str, Any]:
        """One compact branch covering every builtin ``xsi:type`` target.

        Marker-selected builtin types export as objects with the marker
        property (``simple-plus`` shapes); ``xs:anyType`` additionally
        exports as the general mixed array, which the array alternative
        with a required marker attribute map accepts.
        """
        enum = [
            expanded_to_eqname(self._type_expanded_name(type_class))
            for type_class in builtin_targets
        ]
        marker_object: dict[str, Any] = {
            "type": "object",
            "required": [TYPE_KEY],
            "properties": {TYPE_KEY: {"enum": enum}},
        }
        branches = [marker_object]
        if any(self._undetermined(type_class) for type_class in builtin_targets):
            branches.append(
                {
                    "type": "array",
                    "contains": {
                        "type": "object",
                        "required": [TYPE_KEY],
                        "properties": {TYPE_KEY: {"enum": enum}},
                    },
                }
            )
        return {"anyOf": branches}

    def _schema_types(self) -> list[type]:
        """This schema's own named global types."""
        if self._schema_named is None:
            collected: dict[int, type] = {}
            for key, type_class in self.schema.classes.items():
                if key == "schema" or "|" in key or not isinstance(type_class, type):
                    continue
                collected.setdefault(id(type_class), type_class)
            self._schema_named = sorted(collected.values(), key=self._type_expanded_name)
        return self._schema_named

    def _named_types(self) -> list[type]:
        """Named global types and built-ins, the targets ``xsi:type`` allows."""
        if self._named is None:
            collected: dict[int, type] = {
                id(type_class): type_class for type_class in self._schema_types()
            }
            for type_class in vars(_xsd_data_types).values():
                # Only the named built-ins (``name`` is declared on the class
                # itself), never the intermediate helper classes.
                if (
                    isinstance(type_class, type)
                    and issubclass(type_class, XsdDataType)
                    and "name" in type_class.__dict__
                ):
                    collected.setdefault(id(type_class), type_class)
            self._named = sorted(collected.values(), key=self._type_expanded_name)
        return self._named

    @staticmethod
    def _undetermined(type_class: Any) -> bool:
        """Whether the type leaves the content model open (anyType-like)."""
        return getattr(type_class, "_contentKind_", None) is None and (
            (isinstance(type_class, type) and issubclass(type_class, AnyType))
            or _is_schema_base(type_class)
        )

    def _general_value(self, nillable: bool) -> dict[str, Any]:
        """The sound general shape of an undetermined-typed element.

        The exporter always uses the mixed array for these elements; the
        marker object covers any ``xsi:type``-selected actual type.
        """
        items: list[dict[str, Any]] = [
            {
                "type": "object",
                "minProperties": 1,
                "patternProperties": {"^@": {"type": "string"}},
                "additionalProperties": False,
            },
            {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 1,
                "x-pyxsd-wildcard": True,
            },
            {"type": "string"},
        ]
        if nillable:
            items.append(_NIL_MARKER)
        self.unvalidated.add("xsd-wildcard-namespace")
        return {
            "anyOf": [
                {"type": "array", "items": {"anyOf": items}},
                _MARKER_OBJECT,
            ],
            "x-pyxsd-wildcard": True,
        }

    # ------------------------------------------------------------------
    # Type definitions
    # ------------------------------------------------------------------

    def _type_schema(self, type_class: Any, *, explicit: bool, nillable: bool) -> dict[str, Any]:
        """A ``$ref`` to the def for one type, mark, and nil combination."""
        triple = (id(type_class), explicit, nillable)
        existing = self._def_for.get(triple)
        if existing is not None:
            return {"$ref": f"#/$defs/{existing}"}
        key = self._allocate_key(type_class)
        self._def_for[triple] = key
        # The shell closes the recursion cycle: a recursive type's children
        # reference it before its body is complete, and the body is filled
        # in place before the result escapes.
        shell: dict[str, Any] = {}
        self.defs[key] = shell
        shell.update(self._build_type_body(type_class, explicit=explicit, nillable=nillable))
        return {"$ref": f"#/$defs/{key}"}

    def _allocate_key(self, type_class: Any) -> str:
        """A deterministic, collision-safe definition key for one type."""
        base = re.sub(r"[^A-Za-z0-9_]", "_", self._type_expanded_name(type_class))
        candidate = base
        counter = 2
        while candidate in self._used_keys:
            candidate = f"{base}-{counter}"
            counter += 1
        self._used_keys.add(candidate)
        return candidate

    def _build_type_body(
        self, type_class: Any, *, explicit: bool, nillable: bool
    ) -> dict[str, Any]:
        """The schema body for one type under its selected layout."""
        if getattr(type_class, "_assertions_", None):
            self.unvalidated.add("xsd-assertions")
        plan = plan_layout(type_class, explicit_type=explicit)
        layout = plan.layout

        if layout is Layout.EMPTY:
            body: dict[str, Any] = (
                {"anyOf": [{"const": ""}, {"type": "null"}]} if nillable else {"const": ""}
            )
        elif layout is Layout.EMPTY_PLUS:
            body = self._plus_object(plan, type_class, explicit, nillable, content=False)
        elif layout is Layout.SIMPLE:
            scalar = self._scalar_schema(self._scalar_class(type_class))
            body = {"anyOf": [scalar, {"type": "null"}]} if nillable else scalar
        elif layout is Layout.SIMPLE_PLUS:
            body = self._plus_object(plan, type_class, explicit, nillable, content=True)
        elif layout is Layout.LIST:
            array = self._child_array(plan)
            body = {"anyOf": [array, {"type": "null"}]} if nillable else array
        elif layout is Layout.LIST_PLUS:
            body = self._plus_object(plan, type_class, explicit, nillable, content=None)
        elif layout is Layout.RECORD:
            body = self._record_object(plan, type_class, explicit, nillable)
        elif layout is Layout.SEQUENCE:
            body = self._ordered_array(plan, type_class, explicit, nillable, mixed=False)
        else:
            body = self._ordered_array(plan, type_class, explicit, nillable, mixed=True)

        body["x-pyxsd-layout"] = layout.value
        body["x-pyxsd-qname"] = self._type_expanded_name(type_class)
        return body

    def _plus_object(
        self,
        plan: LayoutPlan,
        type_class: Any,
        explicit: bool,
        nillable: bool,
        *,
        content: bool | None,
    ) -> dict[str, Any]:
        """An attribute-carrying object layout, with or without content.

        ``content=False`` is empty-plus; ``content=True`` is simple-plus;
        ``content=None`` is list-plus with its fixed child property.
        """
        properties, required, additional = self._attribute_properties(type_class, explicit)
        normal_properties = dict(properties)
        normal_required = list(required)
        if content is True:
            normal_properties[CONTENT_KEY] = self._scalar_schema(self._scalar_class(type_class))
            if nillable:
                normal_properties[CONTENT_KEY] = {
                    "anyOf": [normal_properties[CONTENT_KEY], {"type": "null"}]
                }
            normal_required.append(CONTENT_KEY)
        elif content is None:
            child = plan.children[0]
            child_key = expanded_to_eqname(child.name)
            normal_properties[child_key] = self._child_array(plan)
            normal_required.append(child_key)
        normal = {
            "type": "object",
            "properties": normal_properties,
            "required": normal_required,
            "additionalProperties": additional,
        }
        if not nillable:
            return normal
        nil_properties = dict(properties)
        nil_properties[CONTENT_KEY] = {"type": "null"}
        nil = {
            "type": "object",
            "properties": nil_properties,
            "required": [*required, CONTENT_KEY],
            "additionalProperties": additional,
        }
        return {"anyOf": [normal, nil]}

    def _record_object(
        self,
        plan: LayoutPlan,
        type_class: Any,
        explicit: bool,
        nillable: bool,
    ) -> dict[str, Any]:
        """A closed ``xs:all``-like object with one property per child."""
        properties, required, additional = self._attribute_properties(type_class, explicit)
        for child in plan.children:
            key = expanded_to_eqname(child.name)
            properties[key] = self._element_value_schema(child.declaration)
            if child.min_occurs >= 1:
                required.append(key)
        normal = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": additional,
        }
        if not nillable:
            return normal
        nil_properties, nil_required, nil_additional = self._attribute_properties(
            type_class, explicit
        )
        nil_properties[CONTENT_KEY] = {"type": "null"}
        nil = {
            "type": "object",
            "properties": nil_properties,
            "required": [*nil_required, CONTENT_KEY],
            "additionalProperties": nil_additional,
        }
        return {"anyOf": [normal, nil]}

    def _ordered_array(
        self,
        plan: LayoutPlan,
        type_class: Any,
        explicit: bool,
        nillable: bool,
        *,
        mixed: bool,
    ) -> dict[str, Any]:
        """The sound general array shape for sequence and mixed layouts.

        JSON Schema cannot enforce attribute-first order, particle order,
        occurrence, or interleaving without a fragile regular-expression
        encoding, so those rules are disclosed instead.
        """
        self.unvalidated.add("xsd-mixed-order" if mixed else "xsd-particle-order")
        attribute_map = self._attribute_map_item(type_class, explicit)
        child_map = self._child_map_item(plan, type_class)
        items: list[dict[str, Any]] = [attribute_map, child_map]
        if mixed:
            items.append({"type": "string"})
        if nillable:
            items.append(_NIL_MARKER)
        return {"type": "array", "items": {"anyOf": items}}

    def _attribute_map_item(self, type_class: Any, explicit: bool) -> dict[str, Any]:
        """The leading singleton attribute map of an ordered layout."""
        properties, required, additional = self._attribute_properties(type_class, explicit)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": additional,
            "minProperties": 1,
        }

    def _child_map_item(self, plan: LayoutPlan, type_class: Any) -> dict[str, Any]:
        """A singleton child map; unknown names stay open only for wildcards."""
        properties = {
            expanded_to_eqname(child.name): self._element_value_schema(child.declaration)
            for child in plan.children
        }
        child_map: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "minProperties": 1,
            "maxProperties": 1,
        }
        if self._element_wildcard(type_class):
            child_map["additionalProperties"] = True
            child_map["x-pyxsd-wildcard"] = True
            self.unvalidated.add("xsd-wildcard-namespace")
        else:
            child_map["additionalProperties"] = False
        return child_map

    @staticmethod
    def _element_wildcard(type_class: Any) -> bool:
        """Whether the type's own model admits wildcard child names."""
        model = _compiled_model(type_class)
        if model is None:
            return True
        return _model_facts(model).wildcard

    def _child_array(self, plan: LayoutPlan) -> dict[str, Any]:
        """The repeatable-child array of a list or list-plus layout."""
        child = plan.children[0]
        array: dict[str, Any] = {
            "type": "array",
            "items": self._element_value_schema(child.declaration),
        }
        if child.min_occurs > 0:
            array["minItems"] = child.min_occurs
        if child.max_occurs is not None:
            array["maxItems"] = child.max_occurs
        return array

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------

    def _attribute_properties(
        self, type_class: Any, explicit: bool
    ) -> tuple[dict[str, Any], list[str], Any]:
        """Declared attribute properties, required keys, and openness."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for expanded, descriptor in self._declared_attributes(type_class).items():
            use = descriptor.getUse()
            if use == "prohibited":
                continue
            key = "@" + expanded_to_eqname(expanded)
            properties[key] = self._scalar_schema(self._declared_type(descriptor))
            if use == "required":
                required.append(key)
        # ``xsi`` location attributes may appear on any element and survive
        # the exporter's reserved-marker filtering, so every attribute map
        # keeps them available.
        for local in _XSI_LOCATION_LOCALS:
            properties["@" + expanded_to_eqname(f"{{{XSI_NAMESPACE}}}{local}")] = {"type": "string"}
        if explicit:
            properties[TYPE_KEY] = {
                "const": expanded_to_eqname(self._type_expanded_name(type_class))
            }
            required.insert(0, TYPE_KEY)
        additional: Any = False
        if getattr(type_class, "effectiveAttributeWildcard_", None) is not None:
            # Wildcard attribute values keep their raw lexical string form.
            additional = {"type": "string"}
            self.unvalidated.add("xsd-wildcard-namespace")
        return properties, required, additional

    def _declared_attributes(self, type_class: Any) -> dict[str, Any]:
        """Effective attribute descriptors by expanded instance name.

        Walks the MRO collecting each class's own attribute uses, least
        derived first, so a derived class's redeclaration shadows its
        base's -- the same collection rule as the importer.
        """
        cache_key = id(type_class)
        cached = self._attribute_cache.get(cache_key)
        if cached is not None:
            return cached
        table: dict[str, Any] = {}
        for klass in type_class.__mro__:
            for key in klass.__dict__.get("_attributeNames_", ()):
                if key in table:
                    continue
                descriptor = klass.__dict__[key]
                table[descriptor.expandedInstanceName(is_attribute=True)] = descriptor
        self._attribute_cache[cache_key] = table
        return table

    @staticmethod
    def _declared_type(descriptor: Any) -> Any:
        """The descriptor's type class, or the string default."""
        getter = getattr(descriptor, "getType", None)
        type_class = getter() if callable(getter) else None
        return type_class if isinstance(type_class, type) else String

    # ------------------------------------------------------------------
    # Scalars
    # ------------------------------------------------------------------

    @staticmethod
    def _scalar_class(type_class: Any) -> Any:
        """The datatype class carrying one type's simple content."""
        if getattr(type_class, "_contentKind_", "simple") != "complex":
            return type_class
        return getattr(type_class, "_simpleContentType_", None)

    def _scalar_schema(self, scalar_class: Any) -> dict[str, Any]:
        """The JSON Schema of one datatype's ``pyxsd-json`` values."""
        cache_key = id(scalar_class)
        cached = self._scalar_cache.get(cache_key)
        if cached is not None:
            return cached
        name = getattr(scalar_class, "name", None) or scalar_class.__name__

        members = getattr(scalar_class, "_unionMembers", None)
        if members:
            schema = {
                "anyOf": [self._member_schema(member) for member in members],
                "x-pyxsd-type": name,
            }
        elif issubclass(scalar_class, Boolean):
            schema = {"type": "boolean", "x-pyxsd-type": name}
        elif issubclass(scalar_class, (XsdList, TypeList, _ListString)):
            schema = self._list_schema(scalar_class, name)
        elif issubclass(scalar_class, int):
            schema = self._integer_schema(scalar_class, name)
        elif issubclass(scalar_class, decimal.Decimal):
            schema = self._decimal_schema(scalar_class, name)
        elif issubclass(scalar_class, float):
            schema = self._float_schema(scalar_class, name)
        elif issubclass(scalar_class, QName):
            # A QName enumeration's lexical values are not the EQName
            # strings the profile exports, so enums are disclosed rather
            # than emitted for the value space.
            schema = self._string_schema(
                scalar_class, name, pattern=_EQNAME_PATTERN, lexical_enum=False
            )
        else:
            schema = self._string_schema(scalar_class, name)

        self._scalar_cache[cache_key] = schema
        return schema

    def _member_schema(self, member: Any) -> dict[str, Any]:
        """The lexical string form one union member's values export as."""
        if issubclass(member, Boolean):
            return {"enum": ["true", "false"]}
        if issubclass(member, int):
            return {"type": "string", "pattern": _INTEGER_LEXICAL_PATTERN}
        if issubclass(member, String) and not issubclass(member, (QName, _ListString)):
            # String-family members export their own lexical value, so the
            # member's facets still apply. QName and list members export
            # differently inside a union and fall back to plain strings.
            return self._string_schema(member, getattr(member, "name", member.__name__))
        return {"type": "string"}

    def _list_schema(self, list_class: Any, name: str) -> dict[str, Any]:
        """XSD list values are arrays, with the list-level length facets."""
        if issubclass(list_class, XsdList):
            item_type = getattr(list_class, "itemType", None) or String
            items: dict[str, Any] = self._scalar_schema(item_type)
        else:
            items = {"type": "string"}
        schema: dict[str, Any] = {"type": "array", "items": items, "x-pyxsd-type": name}
        constraints = self._constraints(list_class)
        if constraints is not None:
            if constraints.length is not None:
                schema["minItems"] = constraints.length
                schema["maxItems"] = constraints.length
            if constraints.min_length is not None:
                schema["minItems"] = constraints.min_length
            if constraints.max_length is not None:
                schema["maxItems"] = constraints.max_length
            if constraints.patterns or constraints.enumerations:
                self.unvalidated.add("xsd-facet")
        return schema

    def _integer_schema(self, integer_class: Any, name: str) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "integer", "x-pyxsd-type": name}
        constraints = self._constraints(integer_class)
        if constraints is None:
            return schema
        self._numeric_bounds(schema, constraints)
        values = constraints.enumeration_values
        if values and all(
            isinstance(value, int) and not isinstance(value, bool) for value in values
        ):
            schema["enum"] = [int(value) for value in values]
        elif constraints.patterns or values:
            self.unvalidated.add("xsd-facet")
        return schema

    def _decimal_schema(self, decimal_class: Any, name: str) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "string",
            "pattern": _DECIMAL_PATTERN,
            "x-pyxsd-type": name,
        }
        constraints = self._constraints(decimal_class)
        if constraints is None:
            return schema
        # Decimal bounds and digit facets have no sound JSON Schema form:
        # the values are canonical strings, not JSON numbers.
        if (
            constraints.min_inclusive is not None
            or constraints.max_inclusive is not None
            or constraints.min_exclusive is not None
            or constraints.max_exclusive is not None
            or constraints.total_digits is not None
            or constraints.fraction_digits is not None
            or constraints.patterns
        ):
            self.unvalidated.add("xsd-facet")
        return schema

    def _float_schema(self, float_class: Any, name: str) -> dict[str, Any]:
        number: dict[str, Any] = {"type": "number"}
        constraints = self._constraints(float_class)
        if constraints is not None:
            self._numeric_bounds(number, constraints)
            if constraints.patterns or constraints.enumerations:
                self.unvalidated.add("xsd-facet")
        return {
            "anyOf": [number, {"enum": ["INF", "-INF", "NaN"]}],
            "x-pyxsd-type": name,
        }

    def _string_schema(
        self, string_class: Any, name: str, *, pattern: str | None = None, lexical_enum: bool = True
    ) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "string", "x-pyxsd-type": name}
        constraints = self._constraints(string_class)
        if constraints is not None:
            if constraints.length is not None:
                schema["minLength"] = constraints.length
                schema["maxLength"] = constraints.length
            if constraints.min_length is not None:
                schema["minLength"] = constraints.min_length
            if constraints.max_length is not None:
                schema["maxLength"] = constraints.max_length
            values = constraints.enumeration_values
            if values and lexical_enum and all(isinstance(value, str) for value in values):
                schema["enum"] = [str(value) for value in values]
            elif values:
                self.unvalidated.add("xsd-facet")
            # A caller-forced pattern (the QName EQName form) replaces the
            # lexical pattern entirely, so facet patterns are disclosed.
            emitted = None if pattern is not None else _pattern_keyword(constraints)
            if emitted is not None:
                schema["pattern"] = emitted
            elif constraints.patterns:
                self.unvalidated.add("xsd-regex-dialect")
        if pattern is not None:
            schema["pattern"] = pattern
        return schema

    @staticmethod
    def _constraints(type_class: Any) -> Any:
        """The type's effective facet constraints, when it has any."""
        constraints = getattr(type_class, "_facetConstraints_", None)
        return constraints if constraints is not None and not constraints.is_empty else None

    def _numeric_bounds(self, schema: dict[str, Any], constraints: Any) -> None:
        """Translate the four bound facets when their values are JSON-safe.

        Bound literals are parsed through the restriction's base datatype,
        so integer families produce ints and float families produce
        floats; a non-finite float bound has no JSON number spelling and
        is disclosed instead.
        """
        pairs = (
            ("minimum", "min_inclusive"),
            ("maximum", "max_inclusive"),
            ("exclusiveMinimum", "min_exclusive"),
            ("exclusiveMaximum", "max_exclusive"),
        )
        for keyword, facet in pairs:
            value = getattr(constraints, facet, None)
            if value is None:
                continue
            if isinstance(value, int):
                schema[keyword] = int(value)
            elif math.isfinite(value):
                schema[keyword] = value
            else:
                self.unvalidated.add("xsd-facet")

    # ------------------------------------------------------------------
    # Names
    # ------------------------------------------------------------------

    @staticmethod
    def _type_expanded_name(type_class: Any) -> str:
        """The Clark name of a compiled type or built-in datatype."""
        name = getattr(type_class, "name", None) or type_class.__name__
        namespace = getattr(type_class, "_targetNamespace_", None)
        if (
            not namespace
            and issubclass(type_class, XsdDataType)
            and type_class.__module__ == "pyxsd.xsd_data_types"
        ):
            namespace = XSD_NS
        return f"{{{namespace}}}{name}" if namespace else name


def _pattern_keyword(constraints: Any) -> str | None:
    """One ECMAScript-safe pattern, or ``None`` when none is provable.

    Only a single restriction step whose translated patterns use the
    plain anchored form is considered; each character is checked
    against the constructs whose dialects differ. Everything else is
    the caller's ``xsd-regex-dialect`` disclosure.
    """
    steps = constraints.patterns
    if len(steps) != 1:
        return None
    inner: list[str] = []
    for compiled in steps[0]:
        text = compiled.pattern
        if not text.startswith("^(?:") or not text.endswith(_PYTHON_ANCHOR_SUFFIX):
            return None
        candidate = text[4 : -len(_PYTHON_ANCHOR_SUFFIX)]
        if not candidate.isascii() or _UNSAFE_ECMASCRIPT.search(candidate):
            return None
        inner.append(candidate)
    if not inner:
        return None
    return "^(?:" + "|".join(inner) + ")$"
