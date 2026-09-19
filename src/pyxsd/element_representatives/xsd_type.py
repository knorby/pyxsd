import copy
import logging
import types
from typing import Any

from pyxsd import facets
from pyxsd.binding import ParseModes
from pyxsd.content_model import compile_content_model
from pyxsd.element_representatives.element_representative import (
    ElementRepresentative,
    componentKind,
)
from pyxsd.wildcards import (
    effective_attribute_wildcard,
    intersect_wildcard_specs,
    register_wildcard,
    union_wildcard_specs,
)
from pyxsd.xsd_data_types import NOTATION, AnySimpleType, XsdDataType, XsdList, qname_context

logger = logging.getLogger(__name__)

#: Marker stored in ``_generatedClass`` while a type's class is being
#: built. Re-entry into ``clsFor`` while the marker is present means the
#: type is part of a derivation cycle.
_CLASS_IN_PROGRESS = object()

#: Marks a simple-content restriction that applies its facets directly to
#: the complex type being built; ``clsFor`` replaces it with the new class
#: once that class exists.
_SELF_CONTENT = object()


class XsdType(ElementRepresentative):
    """Base class for SimpleType and ComplexType.

    In this class, the Python classes for all of the schema types are
    generated.
    """

    # Set by the Union ER for ``xs:union`` members; annotation only
    # (assigned dynamically, guarded with ``getattr`` at use sites).
    unionSpec: list[str]

    def __init__(self, xsdElement, parent):
        """The ``__init__`` for this class' subclasses.  Creates a blank
        list for enumerations.  Creates a blank dictionary for
        attributes.  Creates a blank list for attributeGroup reference
        sites.

        See ElementRepresentative for documentation.
        """
        self.enumerations = []
        self.attributes = {}
        self.attributeGroupRefs = []
        super().__init__(xsdElement, parent)

    def getContainingTypeName(self):
        """Since all types are containing types, this method returns its
        own name.
        """
        if self.name is None:
            self.name = self.getName()
        return self.name

    def getContainingType(self):
        """All types are containing types, so this works for all of the
        XSD types.
        """
        return self

    def getName(self):
        """Mostly normal ``getName()``, except it includes a means to make
        a type name if the type is the child of an element or some
        other tag. That name should look like this:

        ``elementName``|``type tag type``
        """
        name = ElementRepresentative.getName(self)
        if name is not None:
            return name
        # We are implicitly defined in an element
        element = self.parent
        name = f"{element.name}|{self.tagType}"
        # Sibling anonymous types under the same parent (for example
        # the inline simpleType members of a union) would otherwise
        # collide; only colliding candidates get a numeric suffix.
        from pyxsd.element_representatives.element_representative import (
            ComponentTable,
            registry,
        )

        table = getattr(self.getSchema(), "components", None)
        if not isinstance(table, ComponentTable):
            table = registry
        candidate = name
        counter = 0
        while candidate in table:
            counter += 1
            candidate = f"{name}|{counter}"
        name = candidate
        element.typeName = name
        return name

    def containsSchemaBase(self, bases):
        """Returns true if SchemaBase is in the bases list, false if it is
        not. Used by ``getBaseList()``.
        """
        return any(issubclass(base, SchemaBase) for base in bases)

    def getBaseList(self, pyXSD):
        """Creates a list for the base classes.

        Goes through the list of super classes to be added (all type
        classes), and adds them. Adds SchemaBase, if it is not already
        added.  Returns the list as a tuple, since the type factory
        must have the bases stored in a tuple, not a list.

        Honors ``final`` on the base type: if the base declares
        ``final="#all"`` (or the specific derivation method used
        here), the derivation is recorded as a validation error and
        the base is still used so parsing can continue.
        """
        baseList = []
        rawNames = list(self.superClassNames)
        if getattr(self, "listItemType", None) is not None and rawNames:
            # A simple type is either a list or derived from a base;
            # combining both would create classes with conflicting
            # instance layouts (str from the base, list from XsdList).
            self._report_ref_error(
                f"simpleType '{self.name}' cannot combine a list with a restriction or extension",
                code="conflicting-derivation",
            )
            rawNames = []
        for rawName in rawNames:
            superClassName = self.resolveSchemaQName(rawName, parser=pyXSD)
            if superClassName in (self.name, self.expandedName):
                # A type deriving from itself would re-enter clsFor
                # forever; report the cycle and skip the base.
                self._report_ref_error(
                    f"type '{self.name}' derives from itself",
                    code="circular-derivation",
                )
                continue
            base = ElementRepresentative.typeFromName(superClassName, pyXSD)
            if base is None:
                # An unresolved base must not reach issubclass() or
                # the ``types.new_class()`` factory: report it and keep
                # building so the rest of a large schema still loads.
                self._report_ref_error(
                    f"base type '{superClassName}' of '{self.name}' could not be resolved",
                    code="unknown-type",
                )
                continue
            self._checkFinal(base, superClassName)
            baseList.append(base)
        listItem = getattr(self, "listItemType", None)
        if listItem is not None and not any(
            isinstance(base, type) and issubclass(base, XsdList) for base in baseList
        ):
            # A schema-declared xs:list simple type is a value list, not
            # a scalar derived from a base; the item declaration
            # converts each token (see XsdList and the class's itemType).
            baseList.append(XsdList)
        if not self.containsSchemaBase(baseList):
            baseList.append(SchemaBase)
        return tuple(baseList)

    def _listItemClass(self, pyXSD):
        """Resolves the ``itemType`` of a schema-declared list simple type.

        Returns ``None`` when this type is not a list type. An
        unresolvable item type is reported and anySimpleType is used so
        the rest of the schema can still load.
        """
        rawName = getattr(self, "listItemType", None)
        if rawName is None:
            return None
        itemName = self.resolveSchemaQName(rawName, parser=pyXSD)
        itemCls = ElementRepresentative.typeFromName(itemName, pyXSD)
        if itemCls is None:
            self._report_ref_error(
                f"item type '{itemName}' of list type '{self.name}' could not be resolved",
                code="unknown-type",
            )
            return AnySimpleType
        return itemCls

    def getDerivation(self) -> str | None:
        """Returns ``"extension"``, ``"restriction"`` or ``None``.

        The method is read from this type's own content: an
        ``Extension`` child (possibly below a ``ComplexContent``
        wrapper) means extension, a ``Restriction`` means restriction.
        """
        derivation = None
        for child in getattr(self, "processedChildren", []):
            childName = child.__class__.__name__ if child is not None else ""
            if childName == "Extension":
                derivation = "extension"
            elif childName == "Restriction":
                derivation = derivation or "restriction"
            elif childName in ("ComplexContent", "SimpleContent"):
                for grandchild in getattr(child, "processedChildren", []):
                    grandName = grandchild.__class__.__name__ if grandchild is not None else ""
                    if grandName == "Extension":
                        derivation = "extension"
                    elif grandName == "Restriction":
                        derivation = derivation or "restriction"
        return derivation

    def _reportMissingDerivationBase(self):
        """Reports restrictions and extensions that declare no base.

        A restriction may derive from an inline ``simpleType`` instead,
        so only extensions and restrictions without either are errors.
        The derivation ER itself cannot report during construction (the
        parser may not be attached yet), so the check runs while the
        containing type builds its class.
        """
        derivations = []
        for child in getattr(self, "processedChildren", None) or ():
            if child is None:
                continue
            childName = child.__class__.__name__
            if childName in ("Restriction", "Extension"):
                derivations.append(child)
            elif childName in ("ComplexContent", "SimpleContent"):
                for grandchild in getattr(child, "processedChildren", None) or ():
                    if grandchild is not None and grandchild.__class__.__name__ in (
                        "Restriction",
                        "Extension",
                    ):
                        derivations.append(grandchild)
        for derivation in derivations:
            if not getattr(derivation, "hasNoBase", False):
                continue
            if derivation.__class__.__name__ == "Restriction":
                message = (
                    f"restriction of type '{self.name}' has neither a base "
                    "attribute nor an inline simple type"
                )
            else:
                message = f"extension of type '{self.name}' has no base attribute"
            self._report_ref_error(message, code=f"{derivation.__class__.__name__.lower()}-base")

    def _checkFinal(self, base, superClassName):
        """Reports a ``final`` violation against a derivation base.

        ``final="#all"`` forbids any further derivation; the specific
        spellings (``extension``/``restriction``) forbid that method.
        The derivation method is detected from this type's own
        content: an ``Extension`` child means extension, a
        ``Restriction`` child means restriction.
        """
        if base is None:
            return
        if not issubclass(base, SchemaBase):
            # Built-in types cannot declare ``final``; only
            # user-defined types (which carry SchemaBase in their mro)
            # can.
            return
        # The base class's ``name`` attribute is unreliable (an element
        # named 'name' can replace the metadata string), so resolve the
        # base ER by the base reference's local name instead.
        if superClassName.startswith("{"):
            lookupName = superClassName.split("}", 1)[1]
        else:
            lookupName = superClassName.split(":")[-1]
        baseER = ElementRepresentative.getFromName(lookupName, kind="type")
        final = getattr(baseER, "final", None) if baseER is not None else None
        if final is None:
            return
        derivation = self.getDerivation()
        finalTokens = str(final).split()
        violates = "#all" in finalTokens or (derivation is not None and derivation in finalTokens)
        if violates:
            message = (
                f"type '{self.name}' derives by {derivation or 'extension/restriction'} "
                f"from '{superClassName}', whose final value is '{final}'"
            )
            parser = getattr(self.getSchema(), "pyXSD", None)
            if parser is not None:
                parser.report.add_error(message, code="final", element=self.name)
            else:
                logger.error("%s[%s] %s", self.name, "final", message)

    def getElements(self):
        """Returns a blank list.

        Subclasses use this function to return elements, but this
        function is called elsewhere on all of the types.
        """
        return []

    def resolveAttributeGroupRefs(self, pyXSD):
        """Merges referenced attributeGroups into this type's
        attributes.

        Each attributeGroup reference site recorded on this type is
        resolved against the schema's ``attributeGroups`` dictionary;
        the named group's attribute descriptors are added to this
        type's attribute dictionary (local declarations win over
        referenced ones on a name conflict). Unresolvable references
        are recorded on the validation report.
        """
        for refSite in self.attributeGroupRefs:
            groupName = refSite.ref.split(":")[-1]
            group = refSite.resolveReference(
                refSite.ref, self._globalAttributeGroupCandidates(parser=pyXSD), parser=pyXSD
            )
            if group is None:
                message = (
                    f"attributeGroup reference '{refSite.ref}' in type "
                    f"'{self.name}' could not be resolved"
                )
                self._report_ref_error(message, code="unknown-attributeGroup")
                continue
            groupKey = getattr(group, "expandedName", None) or groupName
            for spec in getattr(group, "wildcardElementSpecs", ()):
                register_wildcard(self, spec)
            for spec in getattr(group, "wildcardAttributeSpecs", ()):
                register_wildcard(self, spec)
            for attrName, attr in self._collectAttributeGroup(
                group, frozenset({groupKey}), pyXSD
            ).items():
                if attr.getUse() == "prohibited":
                    # A prohibited use contributed by an attributeGroup
                    # is not an attribute use of the referring type
                    # (attZ015); the base type's own use, if any, stays.
                    continue
                if self._attributeCollides(self.attributes.values(), attr, pyXSD):
                    # A complex type's {attribute uses} must not contain
                    # two uses with the same expanded name; a group
                    # contributing a name the type already has (from its
                    # own declaration or an earlier group) is a schema
                    # error, not a silent override (attQ009, attQ013).
                    self._report_ref_error(
                        f"attribute '{attrName}' is contributed more than "
                        f"once to type '{self.name}' (from attributeGroup "
                        f"'{groupName}')",
                        code="duplicate-attribute",
                    )
                    continue
                attr.pyXSD = pyXSD
                self.attributes[attrName] = attr
        self._applyDefaultAttributeGroup(pyXSD)

    def _applyDefaultAttributeGroup(self, pyXSD) -> None:
        """Merges the schema document's default attribute group into this type.

        XSD 1.1 §3.1.2: when a schema document carries ``defaultAttributes``
        and the type does not set ``defaultAttributesApply="false"`` (the
        parser records the resolved group on ``defaultAttributeGroup``), the
        group's attribute uses and attribute wildcard join the type's. An
        attribute name already contributed by the type's own declaration or
        by an explicitly referenced group is a duplicate attribute use
        (si02), reported rather than silently shadowed.
        """
        group = getattr(self, "defaultAttributeGroup", None)
        if group is None:
            return
        groupName = getattr(group, "name", None) or "?"
        groupKey = getattr(group, "expandedName", None) or groupName
        for spec in getattr(group, "wildcardElementSpecs", ()):
            register_wildcard(self, spec)
        for spec in getattr(group, "wildcardAttributeSpecs", ()):
            register_wildcard(self, spec)
        for attrName, attr in self._collectAttributeGroup(
            group, frozenset({groupKey}), pyXSD
        ).items():
            if attrName in self.attributes:
                self._report_ref_error(
                    f"attribute '{attrName}' is contributed both by the "
                    f"schema's default attribute group '{groupName}' and by "
                    f"type '{self.name}'",
                    code="duplicate-attribute",
                )
                continue
            attr.pyXSD = pyXSD
            self.attributes[attrName] = attr

    def _attributeMatchName(self, attr, pyXSD) -> str | None:
        """The expanded instance name an attribute use matches under.

        Two attribute uses collide only when their qualified names match;
        two local declarations with one local name in different
        namespaces (or with different forms) are distinct (attQ019).
        A reference site that is not resolved yet contributes its
        bookkeeping name, so an unresolved ref never collides with a
        local declaration here.
        """
        name = attr.instanceName(is_attribute=True, parser=pyXSD)
        return name if name is not None else getattr(attr, "name", None)

    def _attributeCollides(self, existing, candidate, pyXSD) -> bool:
        """Whether *candidate* reuses an expanded name already present."""
        key = self._attributeMatchName(candidate, pyXSD)
        if key is None:
            return False
        return any(self._attributeMatchName(attr, pyXSD) == key for attr in existing)

    def resolveAttributeRefs(self, pyXSD):
        """Resolves attribute reference sites to global declarations.

        A ``<xs:attribute ref="..."/>`` has no name or type of its own;
        the referenced global declaration supplies both. Resolved sites
        adopt the declaration's name so instance matching and Python
        access use the real attribute name. Unresolvable references are
        reported and dropped rather than crashing class construction.
        A resolved use whose expanded name is already present is a
        duplicate attribute use (attQ011, attQ012).
        """
        resolved = {}
        match_names: dict[str, object] = {}
        for attr in self.attributes.values():
            effective = self._resolveAttributeRef(attr, pyXSD)
            if effective is None:
                continue
            key = self._attributeMatchName(effective, pyXSD)
            if key is not None and key in match_names:
                self._report_ref_error(
                    f"attribute '{effective.name}' is contributed more than "
                    f"once to type '{self.name}'",
                    code="duplicate-attribute",
                )
                continue
            if key is not None:
                match_names[key] = effective
            localKey = effective.name
            if localKey in resolved:
                # Two uses share a local name but have distinct expanded
                # names (different namespaces): keep both, the later one
                # under its expanded name, so instance matching can
                # address each (attQ019).
                localKey = key if key is not None else localKey
                while localKey in resolved:
                    localKey = f"{localKey}|2"
            resolved[localKey] = effective
        self.attributes = resolved

    def _globalAttributeCandidates(self, pyXSD):
        """Returns the global attribute declarations a ref may resolve to.

        In ``strict`` namespace mode the per-type ``attributes`` mapping
        is keyed by local name, so two global attributes that share a
        local name but live in different namespaces (for example
        ``r:id`` and the injected ``xml:id``) collapse onto one key. The
        parser-owned component table preserves both by expanded name, so
        gather the global attribute declarations from it instead. In
        ``legacy`` mode the historical mapping is used unchanged.
        """
        schema = self.getSchema()
        mode = getattr(pyXSD, "mode", ParseModes.STRICT)
        if getattr(mode, "namespaces", "legacy") != "strict":
            return schema.attributes.values()
        table = getattr(schema, "components", None)
        if not table:
            return schema.attributes.values()
        candidates = []
        for entries in table.values():
            for entry in entries:
                if (
                    componentKind(entry) == "attribute"
                    and not getattr(entry, "isAttributeRef", False)
                    # Only global declarations are valid ref targets; a
                    # local declaration that happens to share the name
                    # must not satisfy the reference.
                    and entry.checkTopLevelType()
                ):
                    candidates.append(entry)
        return candidates

    def _resolveAttributeRef(self, attr, pyXSD):
        """Returns the effective attribute for a reference site.

        Non-reference declarations are returned unchanged. A reference
        is resolved against the schema's global attributes; on success
        the site adopts the declaration's name and type, and on failure
        the problem is reported and ``None`` is returned.
        """
        if not getattr(attr, "isAttributeRef", False):
            return attr
        candidate = attr.resolveReference(
            attr.ref, self._globalAttributeCandidates(pyXSD), parser=pyXSD
        )
        if candidate is None:
            message = (
                f"attribute reference '{attr.ref}' in type '{self.name}' could not be resolved"
            )
            self._report_ref_error(message, code="unknown-attributeRef")
            return None
        attr.referredAttribute = candidate
        attr.name = candidate.name
        if "type" not in attr.__dict__:
            # An untyped global declaration (anySimpleType) has no
            # ``type`` entry; the reference site adopts it through
            # ``getType`` instead of copying an absent attribute.
            candidateType = getattr(candidate, "type", None)
            if candidateType is not None:
                attr.type = candidateType
        attr.pyXSD = pyXSD
        # The referred global declaration may not have been reached while
        # building its containing type's class, so it can lack the
        # parser binding that ``getType`` needs. Give it one.
        candidate.pyXSD = pyXSD
        return attr

    def _collectAttributeGroup(self, group, visited, pyXSD):
        """Returns a group's attributes including nested group refs.

        Direct declarations win over those pulled in from a nested
        ``attributeGroup`` reference. Circular references are skipped
        rather than recursed into: XSD 1.1 allows a circular attribute
        group definition (bug 15795), so the cycle is not an error.
        Every visited group's wildcards are registered on the referring
        type, so an ``xs:anyAttribute`` inside a group definition reaches
        the type's effective attribute wildcard.
        """
        for spec in getattr(group, "wildcardElementSpecs", ()):
            register_wildcard(self, spec)
        for spec in getattr(group, "wildcardAttributeSpecs", ()):
            register_wildcard(self, spec)
        collected = dict(group.attributes)
        for refSite in getattr(group, "attributeGroupRefs", []):
            nestedName = refSite.ref.split(":")[-1]
            nested = refSite.resolveReference(
                refSite.ref, self._globalAttributeGroupCandidates(parser=pyXSD), parser=pyXSD
            )
            if nested is None:
                message = (
                    f"attributeGroup reference '{refSite.ref}' in group "
                    f"'{group.name}' could not be resolved"
                )
                self._report_ref_error(message, code="unknown-attributeGroup")
                continue
            nestedKey = getattr(nested, "expandedName", None) or nestedName
            if nestedKey in visited:
                # A reference back into the current chain closes a cycle;
                # XSD 1.1 accepts it (bug 15795, attgC010/C020/C031/D015).
                continue
            for spec in getattr(nested, "wildcardElementSpecs", ()):
                register_wildcard(self, spec)
            for spec in getattr(nested, "wildcardAttributeSpecs", ()):
                register_wildcard(self, spec)
            for attrName, attr in self._collectAttributeGroup(
                nested, visited | {nestedKey}, pyXSD
            ).items():
                collected.setdefault(attrName, attr)
        return collected

    def _effectiveAttributeWildcard(self, bases):
        """Computes the type's effective attribute wildcard.

        The type's own wildcards — a local ``xs:anyAttribute`` plus the
        ones its attribute groups contribute — combine by intersection
        (errata E1-10). The result then combines with the first base
        class's effective wildcard: an extension unions the two, a
        restriction (or any other derivation) intersects them, and a
        single contribution stands alone. Returns ``None`` when no
        wildcard applies. A base class without the stamped effective
        wildcard falls back to its own stamped specs, so classes built
        before the stamp existed behave like their declarations.
        """
        ownSpecs = getattr(self, "wildcardAttributeSpecs", None)
        target = self.getNamespace()
        own = effective_attribute_wildcard(ownSpecs, target) if ownSpecs else None
        baseSpec = None
        for base in bases:
            if not isinstance(base, type):
                continue
            baseSpec = base.__dict__.get("effectiveAttributeWildcard_")
            if baseSpec is None:
                baseOwn = base.__dict__.get("wildcardAttributeSpecs_")
                if baseOwn:
                    baseSpec = effective_attribute_wildcard(
                        baseOwn, base.__dict__.get("_targetNamespace_")
                    )
            if baseSpec is not None:
                break
        if own is None:
            if self.getDerivation() == "restriction":
                # A restriction that states no wildcard drops the base's
                # (the intersection is empty): an attribute wildcard is
                # never inherited across a restriction (SUN test008,
                # XSD 1.1 §3.4.6.3).
                return None
            return baseSpec
        if baseSpec is None:
            return own
        if self.getDerivation() == "extension":
            return union_wildcard_specs(baseSpec, own, target)
        return intersect_wildcard_specs(baseSpec, own, target)

    def _report_ref_error(self, message, *, code):
        """Records a schema-reference problem on the parser's report.

        Falls back to logging when no parser is attached (for example
        when classes are built in isolation).
        """
        self._reportSchemaError(message, code=code)

    def makeUnionClass(self, pyXSD):
        """Produces the class for a union simple type.

        Member classes are resolved in order: named members first
        (``memberTypes``), then inline ``simpleType`` children; members
        that are themselves unions are flattened. The resulting class
        validates a value by trying each member type in order; a value
        that matches no member raises ``TypeError``.

        Validated instances are union-class instances wrapping the
        first successfully validated member value in ``memberValue``,
        so ``isinstance`` checks against the union class succeed and
        ``str``/``repr``/``==``/``hash`` delegate to the wrapped value.

        Note: members are validated through ``__new__`` (skipping
        ``__init__``), because schema-derived member classes also
        inherit ``SchemaBase.__init__``, which takes no value.
        """
        namedMembers = [
            self.resolveSchemaQName(memberName, parser=pyXSD) for memberName in self.unionSpec
        ]
        members = []
        for memberName in namedMembers:
            if memberName in pyXSD.classes:
                resolved = pyXSD.classes[memberName]
            else:
                resolved = ElementRepresentative.typeFromName(memberName, pyXSD)
            if resolved is None:
                # A ``memberTypes`` name that resolves to no type is a
                # schema error; report it rather than silently
                # accepting a union over an undefined type.
                self._report_ref_error(
                    f"member type '{memberName}' of union '{self.name}' could not be resolved",
                    code="unknown-type",
                )
                continue
            if hasattr(resolved, "_unionMembers"):
                # A union member that is itself a union: flatten.
                members.extend(resolved._unionMembers)
            else:
                members.append(resolved)

        # Inline ``simpleType`` members are built from their own ER
        # directly.  Name lookup would miss them because an anonymous
        # member is not in the component table under the pipe name the
        # union records (D3_4_28v04, D3_4_26v03, D3_4_27v03).
        for inlineER in getattr(self, "unionInline", ()):
            resolved = inlineER.clsFor(pyXSD)
            if resolved is None:
                logger.warning(
                    "inline union member %r of %r could not be built and was skipped",
                    getattr(inlineER, "name", inlineER),
                    self.name,
                )
                continue
            if hasattr(resolved, "_unionMembers"):
                members.extend(resolved._unionMembers)
            else:
                members.append(resolved)

        def __new__(cls, value):
            for member in members:
                try:
                    validated = member.__new__(member, value)
                except (TypeError, ValueError):
                    continue
                instance = object.__new__(cls)
                instance.memberValue = validated
                return instance
            raise TypeError(f"invalid {self.name} value: {value!r} matches no member type")

        def __repr__(self):
            return repr(self.memberValue)

        def __str__(self):
            return str(self.memberValue)

        def __eq__(self, other):
            if other.__class__ is type(self):
                return self.memberValue == other.memberValue
            return self.memberValue == other

        def __hash__(self):
            return hash(self.memberValue)

        union = types.new_class(
            self.name,
            (XsdDataType,),
            {},
            lambda ns: ns.update(
                {
                    "__new__": __new__,
                    "__repr__": __repr__,
                    "__str__": __str__,
                    "__eq__": __eq__,
                    "__hash__": __hash__,
                    "_unionMembers": members,
                    "name": self.name,
                    "pyXSD": pyXSD,
                    "__doc__": self.__doc__,
                }
            ),
        )
        return union

    def _facetNamespace(self, pyXSD, bases):
        """Builds the facet-enforcement entries for a simple type's class.

        Only ``SimpleType`` classes carry facets here; a complex type
        with ``simpleContent`` applies its facets through
        :meth:`_simpleContentNamespace`.
        """
        if self.__class__.__name__ != "SimpleType":
            return {}
        base = bases[0] if bases else None
        parent = getattr(base, "_facetConstraints_", None) if isinstance(base, type) else None
        return self._constraintNamespace(pyXSD, self, base, parent)

    def _checkNotationRestriction(self, base):
        """Reports XSD 1.1 NOTATION restriction violations.

        A *direct* restriction of ``xs:NOTATION`` must carry an
        enumeration facet (Schema Component Constraint), and every
        enumeration value must name a notation declared in the schema
        (simple094, simple095). A type derived from such a restriction
        may add other facets without repeating the enumeration, so only
        the primitive itself is checked.
        """
        if base is not NOTATION:
            return
        enumerations = list(getattr(self, "enumerations", None) or ())
        if not enumerations:
            self._report_ref_error(
                "a restriction of NOTATION must include an enumeration facet",
                code="notation-enumeration-required",
            )
            return
        declared = self._declaredNotations()
        for value in enumerations:
            local = str(value).split(":")[-1] if value else ""
            if local not in declared:
                self._report_ref_error(
                    f"enumeration value '{value}' of a NOTATION restriction "
                    "is not a declared notation",
                    code="unknown-notation",
                )

    def _declaredNotations(self):
        """Returns the local names of the schema's notation declarations."""
        table = getattr(self.getSchema(), "components", None)
        names: set[str] = set()
        if table is None:
            return names
        for entries in table.values():
            for entry in entries:
                if type(entry).__name__ == "Notation" and entry.name:
                    names.add(str(entry.name))
        return names

    def _constraintNamespace(self, pyXSD, source, base, parent):
        """Builds the ``_facetConstraints_``/``_assertionFacets_`` and
        ``__new__`` entries for a definition that restricts *base*.

        The constraint set is merged with the base class's own constraints
        (a restriction can only tighten), and the ``__new__`` wrapper
        applies the whiteSpace facet to the lexical form, constructs the
        value through the base class's validating ``__new__``, then checks
        every other facet and finally the XSD 1.1 ``xs:assertion`` facets
        (assertions accumulate across restriction steps).  A facet
        violation raises ``TypeError``, which the binding paths already
        record as a ``value`` issue; a failed assertion raises a coded
        ``SimpleAssertionError`` the binding paths record as
        ``assert-failed``.
        """
        from pyxsd.assertions import check_simple_assertions, compile_simple_assertions

        if getattr(getattr(pyXSD, "mode", None), "facets", "strict") == "off":
            return {}
        if not isinstance(base, type) or not issubclass(base, XsdDataType):
            return {}
        # Facet literals that are QNames (an enumeration value, a bound
        # on a QName-derived type) resolve against the schema document's
        # own prefix bindings, not the instance's.
        bindings = None
        namespace_context = getattr(pyXSD, "namespaceContext", None)
        if namespace_context is not None:
            try:
                bindings = namespace_context.bindings_for(self.xsdElement)
            except Exception:  # pragma: no cover - defensive
                bindings = None
        with qname_context(bindings):
            result = facets.build_constraints(source, base, parent, base_factory=base)
        for message in result.errors:
            self._report_ref_error(message, code="facet")
        for message in result.conflicts:
            self._report_ref_error(message, code="facet-conflict")
        constraints = result.constraints
        # This type's own assertions; the base class's effective assertions
        # are injected by its own ``__new__`` (which this wrapper chains
        # through ``baseNew``), so the restriction step's set accumulates
        # without evaluating an inherited assertion twice (XSD 1.1 §4.3.15).
        assertion_facets = tuple(compile_simple_assertions(source))
        if constraints.is_empty and not assertion_facets:
            return {}
        baseNew: Any = base.__new__
        # Element classes whose type is (or extends) this simple type are
        # built bare by ``SchemaBase.makeInstanceFromTag`` and receive
        # their value later, so mirror the built-in behaviour for a
        # missing lexical value instead of requiring one.
        missing = object()

        def __new__(cls, value=missing, *args, **kwargs):
            if value is missing:
                return baseNew(cls, *args, **kwargs)
            lexical = value
            if constraints.white_space is not None and isinstance(lexical, str):
                lexical = facets.whitespace_transform(constraints.white_space, lexical)
            instance = baseNew(cls, lexical, *args, **kwargs)
            checked_lexical = lexical if isinstance(lexical, str) else None
            if not constraints.is_empty:
                constraints.check(instance, checked_lexical)
            check_simple_assertions(instance, checked_lexical, assertion_facets)
            return instance

        namespace: dict[str, Any] = {"__new__": __new__}
        if not constraints.is_empty:
            namespace["_facetConstraints_"] = constraints
        if assertion_facets:
            namespace["_assertionFacets_"] = list(assertion_facets)
        return namespace

    def _simpleContentNamespace(self, pyXSD, bases):
        """Builds the ``_simpleContentType_`` entry for a complex type.

        A complex type with ``simpleContent`` ultimately constrains a
        simple type.  An extension inherits its base's content type; a
        restriction either declares an inline simple type (which carries
        the facet machinery itself) or applies facets directly to the
        complex type.  The recorded class validates the element's lexical
        text when the instance binder constructs the value.
        """
        if self.__class__.__name__ != "ComplexType":
            return {}
        simple_content = self._firstProcessedChild(self, "SimpleContent")
        if simple_content is None:
            return {}
        derivation = None
        for name in ("Extension", "Restriction"):
            derivation = self._firstProcessedChild(simple_content, name)
            if derivation is not None:
                break
        if derivation is None:
            return {}
        base = bases[0] if bases else None
        if derivation.__class__.__name__ == "Restriction":
            inline = self._firstProcessedChild(derivation, "SimpleType")
            if inline is not None:
                content_cls = inline.clsFor(pyXSD)
                if content_cls is None:
                    return {}
                return {"_simpleContentType_": content_cls}
            # A restriction of a complex simple-content type restricts
            # that type's content type, not the generated complex class
            # itself (whose name says nothing about the value space).
            facet_base = getattr(base, "_simpleContentType_", None)
            if not (isinstance(facet_base, type) and issubclass(facet_base, XsdDataType)):
                facet_base = base
            parent = (
                getattr(facet_base, "_facetConstraints_", None)
                if isinstance(facet_base, type)
                else None
            )
            namespace = self._constraintNamespace(pyXSD, self, facet_base, parent)
            namespace["_simpleContentType_"] = _SELF_CONTENT
            return namespace
        # Extension: the content type is the base type itself when the
        # base is a simple type, or the content type already carried by
        # the base complex type.
        content_cls = getattr(base, "_simpleContentType_", None)
        if content_cls is None and isinstance(base, type) and issubclass(base, XsdDataType):
            content_cls = base
        if content_cls is None:
            return {}
        return {"_simpleContentType_": content_cls}

    @staticmethod
    def _firstProcessedChild(parent, name):
        """The first processed child of *parent* whose ER class is *name*."""
        for child in getattr(parent, "processedChildren", None) or ():
            if child is not None and child.__class__.__name__ == name:
                return child
        return None

    def clsFor(self, pyXSD):
        """Produces a class for a schema type.

        This function only makes classes for tag types that are
        subclasses of XsdType. The class is created with
        ``types.new_class``, which resolves the correct metaclass and
        prepares the namespace properly. The lifecycle wiring happens
        automatically as the class is built: ``__set_name__`` binds
        each element and attribute descriptor to the new class, and
        ``SchemaBase.__init_subclass__`` records the descriptor
        bookkeeping (``_elementNames_``/``_attributeNames_``) without
        any manual registration here.

        Union simple types take a different route: ``makeUnionClass``
        builds a validating class that tries each member type in
        order.

        AttributeGroup reference sites are resolved (and their
        descriptors merged) before the namespace is assembled.
        Wildcards on the type are recorded as class flags so the
        instance machinery can open pass-through slots.

        Calls ``getBaseList()`` to generate the tuple of bases;
        SchemaBase is in every base list, which is what runs the
        ``__init_subclass__`` hook. Adds the name and the doc string to
        the namespace. Adds the instance of PyXSD to all attributes,
        elements, and the namespace, so it can be accessed later on.
        """
        # One generated class per type ER: a base resolved through
        # ``typeFromName`` during another type's build is the same
        # object as the one stored in ``pyXSD.classes``, so
        # ``issubclass`` and MRO checks for derivation are reliable even
        # when a derived type is declared before its base.
        cached = getattr(self, "_generatedClass", None)
        if cached is not None:
            if cached is _CLASS_IN_PROGRESS:
                # Re-entry means an indirect derivation cycle (A derives
                # from B which derives from A): report it and let the
                # caller treat this base as unresolvable.
                self._report_ref_error(
                    f"type '{self.name}' is part of a circular derivation",
                    code="circular-derivation",
                )
                return None
            return cached
        self._generatedClass = _CLASS_IN_PROGRESS

        if getattr(self, "unionSpec", None) is not None:
            union = self.makeUnionClass(pyXSD)
            self._generatedClass = union
            return union

        self.resolveAttributeGroupRefs(pyXSD)
        self.resolveAttributeRefs(pyXSD)
        self._reportMissingDerivationBase()

        bases = self.getBaseList(pyXSD)
        if self.__class__.__name__ == "SimpleType":
            self._checkNotationRestriction(bases[0] if bases else None)
        namespace = {
            "pyXSD": pyXSD,
            "name": self.name,
            "__doc__": self.__doc__,
            # Binding policy stamped at class-build time; the binding
            # sites in SchemaBase read it to decide what to do with
            # invalid or unresolved content.
            "_parseMode_": getattr(pyXSD, "mode", ParseModes.STRICT),
        }
        # XSD 1.1 assertion set owned by this declaration; the bind-time hook
        # unions it with the base classes' sets by walking the MRO. The
        # class builder may run before the declaration sweep reaches this
        # declaration (a derived one resolves its base), so compile on demand.
        if self.__class__.__name__ == "ComplexType":
            from pyxsd.assertions import compile_assertions

            own_assertions: Any = compile_assertions(self)
        else:
            own_assertions = getattr(self, "compiledAssertions", None) or ()
        namespace["_assertions_"] = list(own_assertions)
        itemCls = self._listItemClass(pyXSD)
        if itemCls is not None:
            namespace["itemType"] = itemCls
        namespace.update(self._facetNamespace(pyXSD, bases))
        namespace.update(self._simpleContentNamespace(pyXSD, bases))
        # Expand group references before reading the wildcard metadata:
        # a wildcard contributed by a named group registers on this type
        # during expansion, and the class must stamp it so binding and
        # occurrence checks see it.
        elements = list(self.getElements())
        if getattr(self, "hasWildcardElements", False):
            namespace["hasWildcardElements_"] = True
        # The effective attribute wildcard folds in the wildcards the
        # attribute groups contribute and combines with the base class's
        # (extension unions, restriction intersects). A type without a
        # wildcard of its own still inherits its base's.
        effectiveWildcard = self._effectiveAttributeWildcard(bases)
        # Stamp the effective wildcard even when it is absent: a
        # restriction that drops the base's wildcard must not inherit it
        # through the Python MRO (SUN test008, XSD 1.1 §3.4.6.3).
        namespace["effectiveAttributeWildcard_"] = effectiveWildcard
        namespace["hasWildcardAttributes_"] = bool(
            getattr(self, "hasWildcardAttributes", False) or effectiveWildcard is not None
        )
        elementSpecs = getattr(self, "wildcardElementSpecs", None)
        if elementSpecs:
            namespace["wildcardElementSpecs_"] = list(elementSpecs)
        attributeSpecs = getattr(self, "wildcardAttributeSpecs", None)
        if attributeSpecs:
            namespace["wildcardAttributeSpecs_"] = list(attributeSpecs)
        namespace["_targetNamespace_"] = self.getNamespace()
        if self.tagAttributes.get("abstract") == "true":
            namespace["abstract_"] = True
        # Record the XSD content category explicitly. Generated simple
        # declarations inherit both a primitive Python class and
        # SchemaBase (for bookkeeping), so Python inheritance cannot
        # tell a simple declaration from a complex one at
        # instance-dispatch time.
        namespace["_contentKind_"] = (
            "simple" if self.__class__.__name__ == "SimpleType" else "complex"
        )
        # Element-only content model: instance validation rejects
        # character data unless the type is mixed or has simple content
        # (whose text is the value).
        mixed_method = getattr(self, "effectiveMixed", None)
        is_mixed = bool(mixed_method()) if mixed_method is not None else False
        has_simple_content = (
            self.__class__.__name__ == "ComplexType"
            and self._firstProcessedChild(self, "SimpleContent") is not None
        )
        namespace["_elementOnly_"] = not is_mixed and not has_simple_content
        # Derivation method and block are needed to validate xsi:type
        # overrides at instance time.
        namespace["_derivation_"] = self.getDerivation()
        blockValue = self.tagAttributes.get("block")
        if blockValue is None:
            blockValue = self.getSchemaBlockDefault()
        if blockValue:
            namespace["_block_"] = blockValue
        # Compile the particle tree before getElements() flattens and
        # folds group-reference occurrences onto the shared descriptors.
        contentModel = compile_content_model(self, pyXSD)
        if contentModel is not None:
            namespace["_contentModel_"] = contentModel
        # The instance matcher additionally admits the type's effective
        # open content (XSD 1.1 §3.4.4.3): a suffix wildcard after the
        # declared particles or an interleaved one around them. The
        # declared particle tree above stays open-content-free for the
        # schema-phase particle/UPA checks and for base composition.
        if self.__class__.__name__ == "ComplexType":
            effective = getattr(self, "effectiveOpenContent", lambda: None)()
            if effective is not None:
                from pyxsd.content_model import merge_open_content

                instanceModel = merge_open_content(contentModel, effective, pyXSD)
                if instanceModel is not None:
                    namespace["_instanceContentModel_"] = instanceModel

        # Accessor allocation must consider every inherited and
        # same-class declaration, not just the names assembled so far:
        # an alias that reuses an inherited key would shadow that
        # declaration out of the effective content model, and an alias
        # that collides with a later attribute would be overwritten by
        # it. Collect the occupied keys and declaration names first.
        inheritedKeys: set[str] = set()
        inheritedAttributeNames: set[str] = set()
        for base in bases:
            for klass in getattr(base, "__mro__", ()):
                for key in klass.__dict__.get("_elementNames_", ()):
                    inheritedKeys.add(key)
                for key in klass.__dict__.get("_attributeNames_", ()):
                    inheritedKeys.add(key)
                    inheritedAttributeNames.add(klass.__dict__[key].name)

        attributes = list(self.attributes.values())
        ownElementNames = {element.name for element in elements}
        ownAttributeNames = {attr.name for attr in attributes}

        def allocateAlias(name: str) -> str:
            """Returns a free ``<name>_element`` class-attribute key."""
            alias = f"{name}_element"
            suffix = 2
            while (
                alias in namespace
                or alias in ownElementNames
                or alias in ownAttributeNames
                or alias in inheritedKeys
            ):
                alias = f"{name}_element_{suffix}"
                suffix += 1
            return alias

        for element in elements:
            element.pyXSD = pyXSD
            existing = namespace.get(element.name)
            if existing is not None and not isinstance(existing, str):
                # A repeated declaration or reference to the same
                # element name: keep both descriptors, disambiguating
                # only the class-attribute key (instance access and
                # xml matching keep using the element's real name).
                counter = 2
                key = f"{element.name}|{counter}"
                while key in namespace:
                    counter += 1
                    key = f"{element.name}|{counter}"
                namespace[key] = element
            elif element.name in ownAttributeNames or element.name in inheritedAttributeNames:
                # An element and an attribute share a name (legal in
                # XSD). The attribute keeps the natural accessor; the
                # element is re-keyed under ``<name>_element`` (with a
                # numeric suffix when that is taken). Marking the
                # descriptor aliased before class creation makes both
                # its storage and its binding use the alias, so the
                # two declarations never share an instance slot.
                alias = allocateAlias(element.name)
                namespace[alias] = element
                element._aliased_ = True
            else:
                # The plain-string case is the historical quirk where
                # an element named 'name' replaces the name metadata.
                namespace[element.name] = element

        # An inherited element shadowed by a derived attribute needs a
        # per-derived-class alias: the base descriptor cannot be
        # re-keyed without changing base instances, so the derived
        # class binds an aliased copy and the merge keeps it in place
        # of the inherited declaration.
        for attr in attributes:
            if attr.name in namespace:
                continue
            inherited = XsdType._findInheritedElement(bases, attr.name)
            if inherited is None:
                continue
            alias = allocateAlias(attr.name)
            copied = copy.copy(inherited)
            copied._aliased_ = True
            namespace[alias] = copied

        for attr in attributes:
            attr.pyXSD = pyXSD
            key = attr.name
            if key in namespace:
                # Two attribute uses share a local name but differ in
                # namespace (attQ019): bind the later one under a unique
                # key so both descriptors reach ``_attributeNames_`` and
                # instance matching sees both expanded names.
                counter = 2
                key = f"{attr.name}|{counter}"
                while key in namespace:
                    counter += 1
                    key = f"{attr.name}|{counter}"
                attr._aliased_ = True
            namespace[key] = attr

        try:
            cls = types.new_class(self.name, bases, {}, lambda ns: ns.update(namespace))
        except Exception:
            logger.exception(
                "class creation failed for %s (superClassNames=%r, bases=%r)",
                self.name,
                self.superClassNames,
                bases,
            )
            raise

        self._generatedClass = cls
        if cls.__dict__.get("_simpleContentType_") is _SELF_CONTENT:
            # A direct-facet restriction is its own content type.
            cls._simpleContentType_ = cls  # type: ignore[attr-defined]
        return cls

    @staticmethod
    def _findInheritedElement(bases, name):
        """Returns the most-derived inherited element declaration.

        Walks each base's MRO (most-derived first) and returns the first
        element descriptor whose declaration name is ``name``, or
        ``None`` when no base declares it.
        """
        for base in bases:
            for klass in getattr(base, "__mro__", ()):
                for key in klass.__dict__.get("_elementNames_", ()):
                    descriptor = klass.__dict__[key]
                    if descriptor.name == name:
                        return descriptor
        return None


from pyxsd.schema_base import SchemaBase  # noqa: E402
