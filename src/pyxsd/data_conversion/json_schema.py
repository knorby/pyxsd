"""Draft 2020-12 JSON Schema projection for every pyxsd data format.

The generator walks compiled schema components, never sample documents.
For ``pyxsd-json``, :func:`~pyxsd.data_conversion.layouts.plan_layout`
selects each type's layout and the shared scalar contract selects each
value schema. The native ``pyxsd``, Parker, BadgerFish, and JsonML
projections describe the same contracts their exporters follow: root
absorption or retention, actual markers, array rules, and nil markers.
The result describes the JSON :meth:`~pyxsd.schema.Schema.from_data`
consumes and ``Document.to_data()`` produces; it never claims full XSD
validation. Rules JSON Schema cannot carry -- particle order, wildcard
namespaces, identity constraints, assertions, and untranslatable pattern
dialects -- are disclosed through the ``x-pyxsd-validation`` annotation.
"""

from __future__ import annotations

import decimal
import math
import re
from copy import deepcopy
from typing import Any, NamedTuple

from pyxsd.data_conversion.contracts import (
    Fidelity,
    FormatName,
    validate_fidelity,
    validate_format,
)
from pyxsd.data_conversion.layouts import (
    EffectiveTypes,
    Layout,
    LayoutPlan,
    _compiled_model,
    _model_facts,
    _possible_element_names,
    effective_element_types,
    is_undetermined,
    known_expanded_names,
    marker_target_types,
    plan_layout,
)
from pyxsd.data_conversion.names import PrefixAllocator, eqname_to_expanded, expanded_to_eqname
from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import XSD_NS
from pyxsd.xsd_data_types import (
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

#: Native, Parker, BadgerFish, and JsonML direct-text key.
TEXT_KEY = "$"

#: BadgerFish's namespace-declaration object key.
XMLNS_KEY = "@xmlns"

#: The generated documents' dialect and profile markers.
_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

#: Attributes ``xsi`` allows on any element and the exporter keeps.
_XSI_LOCATION_LOCALS = ("schemaLocation", "noNamespaceSchemaLocation")

#: The XML namespace, whose attributes always keep the ``xml`` prefix.
_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"

#: Canonical fixed-point decimal strings (``f"{value:f}"``, never float).
_DECIMAL_PATTERN = "^-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?$"

#: The profile's EQName values: ``Q{uri}local`` or a bare local name.
_EQNAME_PATTERN = "^(?:Q\\{[\\s\\S]*\\})?[^\\s:]+$"

#: Lexical QName values outside ``pyxsd-json``: ``prefix:local`` or local.
#: Prefixes and locals are NCNames, so braces (EQName spellings), colons,
#: and spaces never occur inside either part.
_LEXICAL_QNAME_PATTERN = "^(?:[^ :{}]+:)?[^ :{}]+$"

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


class _TypeFacts(NamedTuple):
    """The layout plan and scalar class of one effective type."""

    plan: LayoutPlan
    scalar: Any


class _MergedChild:
    """One possible child name with occurrences folded over types."""

    def __init__(
        self,
        name: str,
        declaration: Any,
        min_occurs: int,
        max_occurs: int | None,
        declared_in: int,
    ) -> None:
        self.name = name
        self.declaration = declaration
        self.min_occurs = min_occurs
        self.max_occurs = max_occurs
        self.declared_in = declared_in


# Python regex features whose ECMAScript meaning is not proven identical;
# a pattern using any of them is disclosed rather than translated. ``\d``
# and ``\D`` differ outright (XSD is Unicode Nd, ECMAScript ASCII-only),
# and ``\w``/``\W``/``\s``/``\S`` are kept defensively even though the
# translator expands them today. ``\U`` is a Python-only eight-digit
# escape, ``\Z``/``\A``/``\b`` anchors and boundaries, ``\p``/``\P``
# property classes, and digits/``\g``/``\K`` backreferences.
_UNSAFE_ECMASCRIPT = re.compile(r"\(\?|\\[1-9gKpPbBAZUdDwWsS]|\[:|[*+?}]\+")

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
    """Return the Draft 2020-12 schema for *schema*'s data contract.

    ``element`` accepts the same local, Clark, and EQName root-selection
    spellings as :meth:`~pyxsd.schema.Schema.from_data`; ``None``
    describes every concrete global root with ``anyOf`` for the
    root-carrying formats. The root-absorbed native and Parker formats
    require one unambiguous root selection. ``typed`` and ``always_list``
    apply only to the native ``pyxsd`` format and must match the intended
    export options. ``fidelity`` is accepted for interface parity; every
    projection is total over supported compiled schemas, so both modes
    generate the same sound shape and neither claims full XSD validation.

    Raises ``ValueError`` for an unknown format or fidelity and for
    native-only options with non-default values on another format.
    """
    requested_format = validate_format(format)
    requested_fidelity = validate_fidelity(fidelity)

    if requested_format != "pyxsd":
        if typed is not True:
            raise ValueError("typed is a native-only JSON Schema option")
        if always_list is not False:
            raise ValueError("always_list is a native-only JSON Schema option")

    builder = _SchemaBuilder(
        schema, requested_fidelity, requested_format, typed=typed, always_list=always_list
    )
    return builder.build(element)


class _SchemaBuilder:
    """One projection run over a compiled schema for a single format."""

    def __init__(
        self,
        schema: Any,
        fidelity: Fidelity,
        format: FormatName,
        *,
        typed: bool = True,
        always_list: bool = False,
    ) -> None:
        self.schema = schema
        self.fidelity = fidelity
        self.format = format
        self.typed = typed
        self.always_list = always_list
        self.defs: dict[str, dict[str, Any]] = {}
        self.unvalidated: set[str] = set()
        self._def_for: dict[tuple[int, bool, bool], str] = {}
        self._used_keys: set[str] = set()
        self._element_cache: dict[int, dict[str, Any]] = {}
        self._element_defs: dict[int, str] = {}
        self._native_leaf_cache: dict[int, dict[str, Any] | None] = {}
        self._marker_cache: dict[Any, Any] = {}
        self._attribute_cache: dict[int, dict[str, Any]] = {}
        self._scalar_cache: dict[int, dict[str, Any]] = {}
        self._effective_cache: dict[int, EffectiveTypes] = {}
        self._facts_cache: dict[int, _TypeFacts] = {}
        self._lexical: _SchemaLexicalNames | None = None
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
        if self.format == "pyxsd-json":
            result = self._hybrid_roots(roots)
        elif self.format == "pyxsd":
            result = self._absorbed_roots(roots, element, self._native_root_value)
        elif self.format == "parker":
            result = self._absorbed_roots(roots, element, self._element_value)
        elif self.format == "badgerfish":
            result = self._badgerfish_roots(roots)
        else:
            result = self._jsonml_roots(roots)
        envelope: dict[str, Any] = {
            "$schema": _SCHEMA_URI,
            "x-pyxsd-format": self.format,
        }
        if self.format == "pyxsd-json":
            envelope["x-pyxsd-profile-version"] = 1
        envelope.update(result)
        envelope["x-pyxsd-validation"] = {
            "level": "structural",
            "unvalidated": sorted(self.unvalidated),
        }
        if self.defs:
            envelope["$defs"] = self.defs
        return envelope

    def _hybrid_roots(self, roots: list[tuple[str, Any | None]]) -> dict[str, Any]:
        """The ``pyxsd-json`` root objects for one selection."""
        if not roots:
            # No concrete global root exists, so no document can be exported.
            return {"not": {}}
        alternatives = [self._root_object(eqname, descriptor) for eqname, descriptor in roots]
        if len(alternatives) == 1:
            return alternatives[0]
        return {"anyOf": alternatives}

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
    # Effective types and merged models for the conventional formats
    # ------------------------------------------------------------------

    def _absorbed_roots(
        self,
        roots: list[tuple[str, Any | None]],
        element: str | None,
        value_builder: Any,
    ) -> dict[str, Any]:
        """The root value schema for a root-absorbing format."""
        if element is None:
            if len(roots) != 1:
                raise PyXSDError(
                    f"root-ambiguous at '': the {self.format!r} format absorbs the "
                    "root, so element= must select one unambiguous global element"
                )
            roots = self._declaration_roots(roots[0][1])
        if not roots:
            return {"not": {}}
        if roots[0][1] is None:
            # An unknown selection has no declaration to describe.
            return self._element_body_for(None, self._body_builder())
        alternatives = [value_builder(descriptor) for _, descriptor in roots]
        return alternatives[0] if len(alternatives) == 1 else {"anyOf": alternatives}

    def _element_value(self, descriptor: Any) -> dict[str, Any]:
        """A ``$ref`` to the def holding one element's value schema."""
        cache_key = id(descriptor)
        existing = self._element_defs.get(cache_key)
        if existing is not None:
            return {"$ref": f"#/$defs/{existing}"}
        key = self._allocate_key_name(descriptor.expandedInstanceName())
        self._element_defs[cache_key] = key
        # The shell closes the recursion cycle exactly as type defs do.
        shell: dict[str, Any] = {}
        self.defs[key] = shell
        body_builder = self._body_builder()
        shell.update(self._element_body_for(descriptor, body_builder))
        shell["x-pyxsd-qname"] = descriptor.expandedInstanceName()
        return {"$ref": f"#/$defs/{key}"}

    def _body_builder(self) -> Any:
        """The per-format element-body builder."""
        if self.format == "pyxsd":
            return self._native_element_body
        if self.format == "parker":
            return self._parker_element_body
        if self.format == "badgerfish":
            return self._badgerfish_element_body
        return self._jsonml_element_body

    def _element_body_for(self, descriptor: Any | None, body_builder: Any) -> dict[str, Any]:
        """One element's body; the builder owns its undetermined shape."""
        if descriptor is None:
            # An unknown absorbed root has no declaration to describe.
            return self._general_conventional_body(False)
        if getattr(descriptor, "identities", None):
            self.unvalidated.add("xsd-identity-constraints")
        body = body_builder(descriptor)
        declared = descriptor.getType()
        if declared is not None and getattr(declared, "_assertions_", None):
            self.unvalidated.add("xsd-assertions")
        return body

    def _general_conventional_body(self, nillable: bool) -> dict[str, Any]:
        """The sound general shape of an undetermined-typed element."""
        self.unvalidated.add("xsd-wildcard-namespace")
        branches: list[dict[str, Any]] = [
            {"type": "object", "x-pyxsd-wildcard": True},
            {"type": "string"},
        ]
        if self.format == "pyxsd" or (self.format == "parker" and nillable):
            branches.append({"type": "null"})
        if self.format == "pyxsd" and nillable:
            self.unvalidated.add("native-null-ambiguity")
        return {"anyOf": branches, "x-pyxsd-wildcard": True}

    def _effective_types(self, descriptor: Any) -> EffectiveTypes:
        """The shared effective-type closure for one element declaration."""
        cache_key = id(descriptor)
        cached = self._effective_cache.get(cache_key)
        if cached is not None:
            return cached
        result = effective_element_types(self.schema, descriptor, cache=self._marker_cache)
        if result.alternatives:
            self.unvalidated.add("xsd-conditional-type")
        self._effective_cache[cache_key] = result
        return result

    def _type_facts(self, type_class: Any) -> _TypeFacts:
        """The layout plan and simple scalar class of one effective type."""
        cache_key = id(type_class)
        cached = self._facts_cache.get(cache_key)
        if cached is not None:
            return cached
        plan = plan_layout(type_class)
        scalar = (
            self._scalar_class(type_class)
            if plan.layout in (Layout.SIMPLE, Layout.SIMPLE_PLUS)
            else None
        )
        facts = _TypeFacts(plan, scalar)
        self._facts_cache[cache_key] = facts
        return facts

    def _merged_children(self, types: list[Any]) -> list[_MergedChild]:
        """One entry per possible child name, occurrence-folded over types.

        A type that cannot hold the child makes it optional, so required
        children stay required only across every effective type.
        """
        merged: dict[str, _MergedChild] = {}
        for type_class in types:
            for child in self._type_facts(type_class).plan.children:
                entry = merged.get(child.name)
                if entry is None:
                    merged[child.name] = _MergedChild(
                        child.name, child.declaration, child.min_occurs, child.max_occurs, 1
                    )
                    continue
                entry.min_occurs = min(entry.min_occurs, child.min_occurs)
                if child.max_occurs is None or entry.max_occurs is None:
                    entry.max_occurs = None
                else:
                    entry.max_occurs = max(entry.max_occurs, child.max_occurs)
                entry.declared_in += 1
        for entry in merged.values():
            if entry.declared_in < len(types):
                entry.min_occurs = 0
        return list(merged.values())

    def _merged_attributes(self, types: list[Any]) -> list[tuple[str, Any, bool]]:
        """``(expanded, descriptor, required)`` per usable declared attribute.

        ``required`` holds only when every effective type declares the
        attribute with ``use="required"``; a type that lacks it makes the
        property optional, which the flat object cannot condition on.
        """
        merged: dict[str, list[Any]] = {}
        for type_class in types:
            for expanded, descriptor in self._declared_attributes(type_class).items():
                if descriptor.getUse() == "prohibited":
                    continue
                merged.setdefault(expanded, [descriptor, []])[1].append(descriptor.getUse())
        result: list[tuple[str, Any, bool]] = []
        for expanded, (descriptor, uses) in merged.items():
            required = len(uses) == len(types) and all(use == "required" for use in uses)
            result.append((expanded, descriptor, required))
        return result

    def _native_leaf_allows(self, type_class: Any) -> bool:
        """Whether an instance of *type_class* can export as a bare leaf."""
        for descriptor in self._declared_attributes(type_class).values():
            use = descriptor.getUse()
            if use == "prohibited":
                continue
            if use == "required":
                return False
        return all(child.min_occurs == 0 for child in self._type_facts(type_class).plan.children)

    # ------------------------------------------------------------------
    # Native ``pyxsd``
    # ------------------------------------------------------------------

    def _native_element_body(self, descriptor: Any) -> dict[str, Any]:
        """The flat native shape: bare leaf values or one merged object."""
        declared = descriptor.getType()
        if declared is None or self._undetermined(declared):
            return self._general_conventional_body(bool(descriptor.isNillable()))
        effective = self._effective_types(descriptor)
        types = [*effective.bindings, *effective.targets]
        # The flat object groups children by name, so ordered models and
        # mixed positions cannot be reconstructed from the JSON alone.
        self._disclose_grouped_order(types)
        leaf = self._native_leaf_union(descriptor)
        obj = self._native_object_form(types, effective, bool(descriptor.isNillable()))
        if leaf is None:
            return obj
        return {"anyOf": [leaf, obj]}

    def _native_leaf_union(self, descriptor: Any) -> dict[str, Any] | None:
        """The bare-leaf value union of one element, or ``None``.

        Leaf-ability is per binding type: any type whose attributes are all
        optional and whose children all have minimum zero can produce an
        instance without attributes or children, which exports bare.
        """
        cache_key = id(descriptor)
        if cache_key in self._native_leaf_cache:
            return self._native_leaf_cache[cache_key]
        nillable = bool(descriptor.isNillable())
        leaf_types = [
            type_class
            for type_class in self._effective_types(descriptor).bindings
            if self._native_leaf_allows(type_class)
        ]
        result = self._native_leaf_branches(leaf_types, nillable)
        self._native_leaf_cache[cache_key] = result
        return result

    def _native_leaf_branches(self, leaf_types: list[Any], nillable: bool) -> dict[str, Any] | None:
        """The leaf-value schema for the types that can export bare."""
        if not leaf_types:
            return None
        branches: list[dict[str, Any]] = []
        if self.typed:
            non_simple = False
            for type_class in leaf_types:
                facts = self._type_facts(type_class)
                if facts.scalar is not None:
                    branches.append(self._scalar_schema(facts.scalar))
                if facts.plan.layout is Layout.MIXED:
                    # A mixed leaf carries its raw character data.
                    branches.append({"type": "string"})
                elif facts.scalar is None:
                    non_simple = True
            if non_simple or nillable:
                # An empty element exports null; a nilled element without
                # its marker exports the same null.
                branches.append({"type": "null"})
            if nillable:
                self.unvalidated.add("native-null-ambiguity")
        else:
            branches.append({"type": "string"})
            if nillable:
                branches.append({"type": "null"})
        branches = _dedupe(branches)
        return branches[0] if len(branches) == 1 else {"anyOf": branches}

    def _native_object_form(
        self, types: list[Any], effective: EffectiveTypes, nillable: bool
    ) -> dict[str, Any]:
        """The attribute, child, and text object the native exporter emits."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for expanded, descriptor, required_attr in self._merged_attributes(types):
            if self.typed:
                properties["@" + expanded] = self._scalar_schema(self._declared_type(descriptor))
            else:
                properties["@" + expanded] = {"type": "string"}
            if required_attr:
                required.append("@" + expanded)
        if nillable:
            properties["@" + _xsi_lexical("nil")] = {"enum": ["true", "1"]}
        if effective.targets or effective.builtins:
            properties["@" + _xsi_lexical("type")] = {"type": "string"}
        for local in _XSI_LOCATION_LOCALS:
            properties["@" + _xsi_lexical(local)] = {"type": "string"}
        for child in self._merged_children(types):
            value = self._element_value(child.declaration)
            if self.always_list or child.max_occurs is None or child.max_occurs > 1:
                array = {"type": "array", "items": value}
                value = array if self.always_list else {"anyOf": [value, array]}
            properties[child.name] = value
            if child.min_occurs >= 1 and not nillable:
                # A nilled element carries no children, so a nillable
                # parent cannot make a child unconditionally required.
                required.append(child.name)
        if self._admits_text(types):
            # Inside an object the native exporter always writes the raw
            # text under ``$``, never the typed scalar.
            properties[TEXT_KEY] = {"type": "string"}
        obj: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            obj["required"] = required
        self._apply_openness(obj, types)
        return obj

    def _admits_text(self, types: list[Any]) -> bool:
        """Whether any effective type can contribute character data."""
        return self._has_mixed(types) or any(
            self._type_facts(type_class).scalar is not None for type_class in types
        )

    def _has_mixed(self, types: list[Any]) -> bool:
        """Whether any effective type admits interleaved character data."""
        return any(self._type_facts(type_class).plan.layout is Layout.MIXED for type_class in types)

    def _child_wildcard(self, types: list[Any]) -> bool:
        """Whether any effective type admits wildcard child names.

        Simple-content types have no compiled model but never hold child
        elements, so a missing model alone does not make them open.
        """
        return any(
            self._type_facts(type_class).scalar is None and self._element_wildcard(type_class)
            for type_class in types
        )

    def _apply_openness(self, obj: dict[str, Any], types: list[Any]) -> None:
        """Close properties unless a wildcard rule keeps the object open."""
        if self._child_wildcard(types):
            obj["additionalProperties"] = True
            self.unvalidated.add("xsd-wildcard-namespace")
        elif any(
            getattr(type_class, "effectiveAttributeWildcard_", None) is not None
            for type_class in types
        ):
            obj["patternProperties"] = {"^@": {"type": "string"}}
            obj["additionalProperties"] = False
            self.unvalidated.add("xsd-wildcard-namespace")
        else:
            obj["additionalProperties"] = False

    def _native_root_value(self, descriptor: Any) -> dict[str, Any]:
        """The absorbed native root: the value or its primitive wrap."""
        value = self._element_value(descriptor)
        declared = descriptor.getType()
        if declared is None or self._undetermined(declared):
            # An untyped root exports its character data or null when empty.
            leaf: dict[str, Any] | None = {"anyOf": [{"type": "string"}, {"type": "null"}]}
        else:
            leaf = self._native_leaf_union(descriptor)
        if leaf is None:
            return value
        wrap = {
            "type": "object",
            "properties": {TEXT_KEY: leaf},
            "required": [TEXT_KEY],
            "additionalProperties": False,
            "minProperties": 1,
            "maxProperties": 1,
        }
        return {"anyOf": [value, wrap]}

    # ------------------------------------------------------------------
    # Parker
    # ------------------------------------------------------------------

    def _parker_element_body(self, descriptor: Any) -> dict[str, Any]:
        """Root-absorbed scalars, local-name objects, and null for nil."""
        declared = descriptor.getType()
        if declared is None or self._undetermined(declared):
            return self._general_conventional_body(bool(descriptor.isNillable()))
        effective = self._effective_types(descriptor)
        # Parker drops the xsi:type marker, so marker targets contribute
        # their bare shapes alongside the declared and conditional types.
        types = [*effective.bindings, *effective.targets]
        self._disclose_grouped_order(types)
        branches: list[dict[str, Any]] = []
        for type_class in types:
            scalar = self._type_facts(type_class).scalar
            if scalar is not None:
                branches.append(self._scalar_schema(scalar))
        if self._has_mixed(types):
            branches.append({"type": "string"})
        if any(self._type_facts(type_class).scalar is None for type_class in types):
            # Only a type that can hold children or mixed text produces
            # Parker's grouped object; simple content stays scalar.
            branches.append(self._parker_object_form(types))
        if bool(descriptor.isNillable()):
            branches.append({"type": "null"})
        branches = _dedupe(branches)
        return branches[0] if len(branches) == 1 else {"anyOf": branches}

    def _parker_object_form(self, types: list[Any]) -> dict[str, Any]:
        """Children grouped by local name; actual repeats become arrays."""
        by_local: dict[str, list[_MergedChild]] = {}
        for child in self._merged_children(types):
            by_local.setdefault(_local_of(child.name), []).append(child)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for local, entries in by_local.items():
            values = [self._element_value(child.declaration) for child in entries]
            value: dict[str, Any] = values[0] if len(values) == 1 else {"anyOf": _dedupe(values)}
            unbounded = any(child.max_occurs is None for child in entries)
            bounded_max = max(
                (child.max_occurs for child in entries if child.max_occurs is not None),
                default=1,
            )
            if unbounded or bounded_max > 1:
                # Parker arrays reflect actual repetition, so a single
                # occurrence may stay bare next to the array alternative.
                value = {"anyOf": _dedupe([value, {"type": "array", "items": value}])}
            properties[local] = value
            if len(entries) == 1 and entries[0].min_occurs >= 1:
                required.append(local)
        if self._has_mixed(types):
            properties[TEXT_KEY] = {"type": "string"}
        obj: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            obj["required"] = required
        if self._child_wildcard(types):
            obj["additionalProperties"] = True
            self.unvalidated.add("xsd-wildcard-namespace")
        else:
            obj["additionalProperties"] = False
        return obj

    def _disclose_grouped_order(self, types: list[Any]) -> None:
        """Disclose the order rules a grouped object cannot reconstruct."""
        layouts = {self._type_facts(type_class).plan.layout for type_class in types}
        if Layout.SEQUENCE in layouts:
            self.unvalidated.add("xsd-particle-order")
        if Layout.MIXED in layouts:
            self.unvalidated.add("xsd-mixed-order")

    # ------------------------------------------------------------------
    # BadgerFish
    # ------------------------------------------------------------------

    def _badgerfish_roots(self, roots: list[tuple[str, Any | None]]) -> dict[str, Any]:
        """The single-entry root objects with lexical names."""
        if not roots:
            return {"not": {}}
        alternatives = []
        for eqname, descriptor in roots:
            self._prepare_lexical_names([descriptor])
            alternatives.append(self._badgerfish_root_object(eqname, descriptor))
        return alternatives[0] if len(alternatives) == 1 else {"anyOf": alternatives}

    def _badgerfish_root_object(self, eqname: str, descriptor: Any | None) -> dict[str, Any]:
        """One root's single-entry object with root-scoped declarations."""
        if descriptor is None:
            return {"type": "object", "minProperties": 1, "maxProperties": 1}
        lexical = self._lexical_name(descriptor.expandedInstanceName())
        return {
            "type": "object",
            "properties": {lexical: self._badgerfish_root_value(descriptor)},
            "required": [lexical],
            "additionalProperties": False,
            "minProperties": 1,
            "maxProperties": 1,
            "x-pyxsd-qname": eqname,
        }

    def _badgerfish_root_value(self, descriptor: Any) -> dict[str, Any]:
        """One root's value: bare scalar, its declared wrap, or the object.

        The root body is built inline so ``@xmlns`` declarations -- which
        only the root's exporter injects -- stay a root-only property.
        """
        branches: list[dict[str, Any]] = []
        leaf = self._badgerfish_leaf(descriptor)
        if leaf is not None:
            # Without attributes or namespace declarations the scalar is
            # emitted directly; with declarations it is wrapped under "$".
            branches.append(leaf)
            branches.append(
                {
                    "type": "object",
                    "properties": {TEXT_KEY: leaf, XMLNS_KEY: self._xmlns_schema()},
                    "required": [TEXT_KEY],
                    "additionalProperties": False,
                }
            )
        branches.append(self._badgerfish_build(descriptor, root=True))
        return {"anyOf": branches}

    def _badgerfish_leaf(self, descriptor: Any) -> dict[str, Any] | None:
        """The bare scalar a simple-typed element can export as."""
        declared = descriptor.getType()
        if declared is None or self._undetermined(declared):
            return {"type": "string"}
        scalars: list[dict[str, Any]] = []
        for type_class in self._effective_types(descriptor).bindings:
            scalar = self._type_facts(type_class).scalar
            if scalar is not None and self._native_leaf_allows(type_class):
                scalars.append(self._scalar_schema(scalar))
        scalars = _dedupe(scalars)
        if not scalars:
            return None
        return scalars[0] if len(scalars) == 1 else {"anyOf": scalars}

    def _badgerfish_element_body(self, descriptor: Any) -> dict[str, Any]:
        """One nested element's object with lexical names and typed ``$``."""
        declared = descriptor.getType()
        if declared is None or self._undetermined(declared):
            return self._general_conventional_body(bool(descriptor.isNillable()))
        return self._badgerfish_build(descriptor, root=False)

    def _badgerfish_build(self, descriptor: Any, *, root: bool) -> dict[str, Any]:
        """The BadgerFish object form: ``@`` attributes and grouped children."""
        effective = self._effective_types(descriptor)
        types = [*effective.bindings, *effective.targets]
        self._disclose_grouped_order(types)
        nillable = bool(descriptor.isNillable())
        properties: dict[str, Any] = {}
        required: list[str] = []
        if nillable:
            properties["@" + _xsi_lexical("nil")] = {"const": True}
        if effective.targets or effective.builtins:
            properties["@" + _xsi_lexical("type")] = {"type": "string"}
        for local in _XSI_LOCATION_LOCALS:
            properties["@" + _xsi_lexical(local)] = {"type": "string"}
        for expanded, attr_descriptor, required_attr in self._merged_attributes(types):
            lexical = "@" + self._lexical_name(expanded)
            properties[lexical] = self._scalar_schema(self._declared_type(attr_descriptor))
            if required_attr:
                required.append(lexical)
        for child in self._merged_children(types):
            lexical = self._lexical_name(child.name)
            value = self._element_value(child.declaration)
            if child.max_occurs is None or child.max_occurs > 1:
                # Schema-repeatable children are arrays whenever present,
                # including a single occurrence.
                array: dict[str, Any] = {"type": "array", "items": value}
                if child.min_occurs > 0:
                    array["minItems"] = child.min_occurs
                if child.max_occurs is not None:
                    array["maxItems"] = child.max_occurs
                value = array
            properties[lexical] = value
            if child.min_occurs >= 1 and not nillable:
                # A nilled element carries no children, so a nillable
                # parent cannot make a child unconditionally required.
                required.append(lexical)
        dollar = self._badgerfish_dollar(types)
        if dollar is not None:
            properties[TEXT_KEY] = dollar
            if not nillable and all(
                self._type_facts(type_class).scalar is not None for type_class in types
            ):
                # Every effective type is simple content, so a non-nilled
                # export always carries the scalar under "$".
                required.append(TEXT_KEY)
        if root:
            properties[XMLNS_KEY] = self._xmlns_schema()
        obj: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            obj["required"] = required
        self._apply_openness(obj, types)
        leaf = self._badgerfish_leaf(descriptor)
        if leaf is None or root:
            return obj
        return {"anyOf": [leaf, obj]}

    def _badgerfish_dollar(self, types: list[Any]) -> dict[str, Any] | None:
        """The typed scalar union ``$`` carries for character data."""
        branches: list[dict[str, Any]] = []
        for type_class in types:
            scalar = self._type_facts(type_class).scalar
            if scalar is not None:
                branches.append(self._scalar_schema(scalar))
        if self._has_mixed(types):
            branches.append({"type": "string"})
        branches = _dedupe(branches)
        if not branches:
            return None
        return branches[0] if len(branches) == 1 else {"anyOf": branches}

    def _xmlns_schema(self) -> dict[str, Any]:
        """The ``@xmlns`` declaration object BadgerFish roots may carry."""
        return {"type": "object", "additionalProperties": {"type": "string"}}

    # ------------------------------------------------------------------
    # JsonML
    # ------------------------------------------------------------------

    def _jsonml_roots(self, roots: list[tuple[str, Any | None]]) -> dict[str, Any]:
        """The positional root arrays."""
        if not roots:
            return {"not": {}}
        alternatives = []
        for eqname, descriptor in roots:
            self._prepare_lexical_names([descriptor])
            alternatives.append(self._jsonml_root_array(eqname, descriptor))
        return alternatives[0] if len(alternatives) == 1 else {"anyOf": alternatives}

    def _jsonml_root_array(self, eqname: str, descriptor: Any | None) -> dict[str, Any]:
        """One root's array with its constant tag and root declarations."""
        if descriptor is None:
            return self._jsonml_general_array(None, False)
        declared = descriptor.getType()
        if declared is None or self._undetermined(declared):
            tag = self._lexical_name(descriptor.expandedInstanceName())
            return self._jsonml_general_array(tag, bool(descriptor.isNillable()))
        body = self._jsonml_build(descriptor, root=True)
        body["x-pyxsd-qname"] = eqname
        return body

    def _jsonml_element_body(self, descriptor: Any) -> dict[str, Any]:
        """One nested element's ``[tag, attributes?, ...content]`` array."""
        declared = descriptor.getType()
        if declared is None or self._undetermined(declared):
            tag = self._lexical_name(descriptor.expandedInstanceName())
            return self._jsonml_general_array(tag, bool(descriptor.isNillable()))
        return self._jsonml_build(descriptor, root=False)

    def _jsonml_build(self, descriptor: Any, *, root: bool) -> dict[str, Any]:
        effective = self._effective_types(descriptor)
        types = [*effective.bindings, *effective.targets]
        tag = self._lexical_name(descriptor.expandedInstanceName())
        items: list[dict[str, Any]] = [self._jsonml_attrs_schema(types, effective, root=root)]
        for type_class in types:
            scalar = self._type_facts(type_class).scalar
            if scalar is not None:
                items.append(self._scalar_schema(scalar))
        if self._has_mixed(types):
            items.append({"type": "string"})
        for child in self._merged_children(types):
            items.append(self._element_value(child.declaration))
        if self._child_wildcard(types):
            items.append(deepcopy(_GENERIC_JSONML_ARRAY))
            self.unvalidated.add("xsd-wildcard-namespace")
        if bool(descriptor.isNillable()):
            items.append({"type": "null"})
        result: dict[str, Any] = {
            "type": "array",
            "prefixItems": [{"const": tag}],
            "items": {"anyOf": _dedupe(items)},
            "minItems": 1,
        }
        layouts = {self._type_facts(type_class).plan.layout for type_class in types}
        if layouts <= {Layout.EMPTY, Layout.EMPTY_PLUS}:
            result["maxItems"] = 2
        elif layouts <= {Layout.SIMPLE, Layout.SIMPLE_PLUS}:
            # The scalar content item is always present; only the optional
            # attribute object can shift its position.
            result["minItems"] = 2
            result["maxItems"] = 3
        return result

    def _jsonml_attrs_schema(
        self, types: list[Any], effective: EffectiveTypes, *, root: bool
    ) -> dict[str, Any]:
        """The optional attribute object with typed declared values."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        if effective.targets or effective.builtins:
            properties[_xsi_lexical("type")] = {"type": "string"}
        for expanded, descriptor, required_attr in self._merged_attributes(types):
            properties[self._lexical_name(expanded)] = self._scalar_schema(
                self._declared_type(descriptor)
            )
            if required_attr:
                required.append(self._lexical_name(expanded))
        for local in _XSI_LOCATION_LOCALS:
            properties[_xsi_lexical(local)] = {"type": "string"}
        # The xsi location attributes keep this object non-empty.
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        if root:
            schema["patternProperties"] = {"^xmlns": {"type": "string"}}
        if any(
            getattr(type_class, "effectiveAttributeWildcard_", None) is not None
            for type_class in types
        ):
            schema["additionalProperties"] = {"type": "string"}
            self.unvalidated.add("xsd-wildcard-namespace")
        else:
            schema["additionalProperties"] = False
        return schema

    def _jsonml_general_array(self, tag: str | None, nillable: bool) -> dict[str, Any]:
        """The sound general array for an undetermined or unknown element."""
        self.unvalidated.add("xsd-wildcard-namespace")
        items: list[dict[str, Any]] = [
            {"type": "object"},
            {"type": "string"},
            deepcopy(_GENERIC_JSONML_ARRAY),
        ]
        if nillable:
            items.append({"type": "null"})
        result: dict[str, Any] = {"type": "array", "items": {"anyOf": items}, "minItems": 1}
        if tag is not None:
            result["prefixItems"] = [{"const": tag}]
        return result

    # ------------------------------------------------------------------
    # Lexical names for BadgerFish and JsonML
    # ------------------------------------------------------------------

    def _prepare_lexical_names(self, descriptors: list[Any]) -> None:
        """Allocate the prefixes the exporter would allocate for these roots."""
        self._lexical = _SchemaLexicalNames(known_expanded_names(self.schema, descriptors))
        # A shared child can receive different prefixes under another root's
        # known-name closure. Keep definitions already emitted, but build fresh
        # bodies and references for the new root's lexical context.
        self._element_defs.clear()

    def _lexical_name(self, expanded_name: str) -> str:
        """The deterministic lexical QName the exporter emits."""
        assert self._lexical is not None, "lexical names require _prepare_lexical_names"
        return self._lexical.lexical(expanded_name)

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
            branches.append(deepcopy(_MARKER_OBJECT))
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

    def _marker_targets(self, declared: Any, cta_types: list[Any]) -> tuple[list[Any], list[Any]]:
        """The ``xsi:type`` targets for *declared*, split by origin."""
        return marker_target_types(self.schema, declared, cta_types, cache=self._marker_cache)

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

    @staticmethod
    def _undetermined(type_class: Any) -> bool:
        """Whether the type leaves the content model open (anyType-like)."""
        return is_undetermined(type_class)

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
            items.append(deepcopy(_NIL_MARKER))
        self.unvalidated.add("xsd-wildcard-namespace")
        return {
            "anyOf": [
                {"type": "array", "items": {"anyOf": items}},
                deepcopy(_MARKER_OBJECT),
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
        return self._allocate_key_name(self._type_expanded_name(type_class))

    def _allocate_key_name(self, name: str) -> str:
        """A deterministic, collision-safe definition key for one name."""
        base = re.sub(r"[^A-Za-z0-9_]", "_", name)
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
            items.append(deepcopy(_NIL_MARKER))
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

        Walks the MRO most derived first, keeping the first declaration
        for each name so redeclarations shadow base uses, as in import.
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
        """The JSON Schema of one datatype's values under this format."""
        cache_key = id(scalar_class)
        cached = self._scalar_cache.get(cache_key)
        if cached is not None:
            return cached
        name = getattr(scalar_class, "name", None) or scalar_class.__name__
        schema = self._typed_scalar_schema(scalar_class, name)
        self._scalar_cache[cache_key] = schema
        return schema

    def _typed_scalar_schema(self, scalar_class: Any, name: str) -> dict[str, Any]:
        """The typed value schema shared by every projection."""
        schema: dict[str, Any]
        members = getattr(scalar_class, "_unionMembers", None)
        if members:
            schema = {
                "anyOf": [self._scalar_schema(member) for member in members],
                "x-pyxsd-type": name,
            }
        elif issubclass(scalar_class, Boolean):
            schema = (
                {"type": "integer", "enum": [0, 1], "x-pyxsd-type": name}
                if self.format == "pyxsd"
                else {"type": "boolean", "x-pyxsd-type": name}
            )
            constraints = self._constraints(scalar_class)
            if constraints is not None:
                # Enumerations use the format's Boolean representation;
                # patterns over the lexical forms are
                # not expressible and are disclosed.
                values = constraints.enumeration_values
                if values and all(isinstance(value, (bool, Boolean)) for value in values):
                    schema["enum"] = [
                        int(value) if self.format == "pyxsd" else bool(int(value))
                        for value in values
                    ]
                elif values:
                    self.unvalidated.add("xsd-facet")
                if constraints.patterns:
                    self.unvalidated.add("xsd-facet")
        elif issubclass(scalar_class, (XsdList, TypeList, _ListString)):
            schema = self._list_schema(scalar_class, name)
        elif issubclass(scalar_class, int):
            schema = self._integer_schema(scalar_class, name)
        elif issubclass(scalar_class, decimal.Decimal):
            schema = self._decimal_schema(scalar_class, name)
        elif issubclass(scalar_class, float):
            schema = self._float_schema(scalar_class, name)
        elif issubclass(scalar_class, QName):
            schema = self._qname_schema(scalar_class, name)
        else:
            schema = self._string_schema(scalar_class, name)
        return schema

    def _qname_schema(self, scalar_class: Any, name: str) -> dict[str, Any]:
        """The EQName or lexical QName spelling one format exports."""
        if self.format == "pyxsd-json":
            # A QName enumeration's lexical values are not the EQName
            # strings the profile exports, so enums are disclosed rather
            # than emitted for the value space.
            return self._string_schema(
                scalar_class, name, pattern=_EQNAME_PATTERN, lexical_enum=False
            )
        schema = self._string_schema(
            scalar_class, name, pattern=_LEXICAL_QNAME_PATTERN, lexical_enum=False
        )
        schema["x-pyxsd-namespace-context"] = True
        if self.format == "parker":
            # Parker drops every namespace declaration, so a QName value's
            # namespace is permanently outside the JSON document.
            self.unvalidated.add("namespace-lost")
        return schema

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
        # A pattern over the lexical form cannot apply to JSON numbers,
        # so it is disclosed even when an enumeration is emitted.
        if constraints.patterns:
            self.unvalidated.add("xsd-facet")
        values = constraints.enumeration_values
        if values and all(
            isinstance(value, int) and not isinstance(value, bool) for value in values
        ):
            schema["enum"] = [int(value) for value in values]
        elif values:
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
        # Enumeration values are emitted with the exporter's exact
        # fixed-point spelling, so the enum matches the exported strings.
        values = constraints.enumeration_values
        if values and all(isinstance(value, decimal.Decimal) for value in values):
            schema["enum"] = [f"{value:f}" for value in values]
        elif values:
            self.unvalidated.add("xsd-facet")
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


def _dedupe(branches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate branches while preserving deterministic order."""
    unique: list[dict[str, Any]] = []
    for branch in branches:
        if branch not in unique:
            unique.append(branch)
    return unique


def _uri_local(expanded_name: str) -> tuple[str, str]:
    """Split a Clark expanded name into its namespace URI and local part."""
    eqname = expanded_to_eqname(expanded_name)
    if not eqname.startswith("Q{"):
        return "", eqname
    end = eqname.find("}", 2)
    return eqname[2:end], eqname[end + 1 :]


def _local_of(expanded_name: str) -> str:
    """The local part of a Clark expanded name."""
    return _uri_local(expanded_name)[1]


def _xsi_lexical(local: str) -> str:
    """The fixed ``xsi`` spelling the binder records XSI attributes under."""
    return f"xsi:{local}"


#: The generic wildcard child array JsonML content items admit.
_GENERIC_JSONML_ARRAY: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "prefixItems": [{"type": "string"}],
    "x-pyxsd-wildcard": True,
}


class _SchemaLexicalNames:
    """The exporter's deterministic lexical QNames, derived schema-side.

    Wraps the shared :class:`~pyxsd.data_conversion.names.PrefixAllocator`
    with the fixed ``xml`` spelling, matching the allocation order
    :func:`pyxsd.data_conversion.export._known_names` produces. The
    ``xsi`` prefix never reaches this mapping because the projection
    spells XSI-namespace markers directly.
    """

    def __init__(self, known_names: list[str]) -> None:
        self._allocator = PrefixAllocator(known_names)

    def lexical(self, expanded_name: str) -> str:
        """Return the lexical QName the exporter emits for this name."""
        uri, local = _uri_local(expanded_name)
        if not uri:
            return local
        if uri == _XML_NAMESPACE:
            return f"xml:{local}"
        return self._allocator.lexical(expanded_name)
