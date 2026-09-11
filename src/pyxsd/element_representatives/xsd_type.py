import logging
import types

from pyxsd.binding import ParseModes
from pyxsd.content_model import compile_content_model
from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.xsd_data_types import XsdDataType

logger = logging.getLogger(__name__)


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
        for rawName in self.superClassNames:
            superClassName = self.resolveSchemaQName(rawName, is_attribute=True, parser=pyXSD)
            base = ElementRepresentative.typeFromName(superClassName, pyXSD)
            self._checkFinal(base, superClassName)
            baseList.append(base)
        if not self.containsSchemaBase(baseList):
            baseList.append(SchemaBase)
        return tuple(baseList)

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
            elif childName == "ComplexContent":
                for grandchild in getattr(child, "processedChildren", []):
                    grandName = grandchild.__class__.__name__ if grandchild is not None else ""
                    if grandName == "Extension":
                        derivation = "extension"
                    elif grandName == "Restriction":
                        derivation = derivation or "restriction"
        return derivation

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
            group = self.getSchema().attributeGroups.get(groupName)
            if group is None:
                message = (
                    f"attributeGroup reference '{groupName}' in type "
                    f"'{self.name}' could not be resolved"
                )
                self._report_ref_error(message, code="unknown-attributeGroup")
                continue
            for attrName, attr in self._collectAttributeGroup(
                group, frozenset({groupName})
            ).items():
                if attrName in self.attributes:
                    logger.debug(
                        "attribute %r from attributeGroup %r is already "
                        "declared on %r; keeping the local declaration",
                        attrName,
                        groupName,
                        self.name,
                    )
                    continue
                attr.pyXSD = pyXSD
                self.attributes[attrName] = attr

    def _collectAttributeGroup(self, group, visited):
        """Returns a group's attributes including nested group refs.

        Direct declarations win over those pulled in from a nested
        ``attributeGroup`` reference. Circular references are skipped
        rather than recursed into.
        """
        collected = dict(group.attributes)
        for refSite in getattr(group, "attributeGroupRefs", []):
            nestedName = refSite.ref.split(":")[-1]
            if nestedName in visited:
                message = (
                    f"circular attributeGroup reference chain involving "
                    f"'{nestedName}' (reached from '{self.name}')"
                )
                self._report_ref_error(message, code="circular-attributeGroup")
                continue
            nested = self.getSchema().attributeGroups.get(nestedName)
            if nested is None:
                message = (
                    f"attributeGroup reference '{nestedName}' in group "
                    f"'{group.name}' could not be resolved"
                )
                self._report_ref_error(message, code="unknown-attributeGroup")
                continue
            for attrName, attr in self._collectAttributeGroup(
                nested, visited | {nestedName}
            ).items():
                collected.setdefault(attrName, attr)
        return collected

    def _report_ref_error(self, message, *, code):
        """Records a schema-reference problem on the parser's report.

        Falls back to logging when no parser is attached (for example
        when classes are built in isolation).
        """
        parser = getattr(self.getSchema(), "pyXSD", None)
        if parser is not None:
            parser.report.add_error(message, code=code, element=self.name)
        else:
            logger.error("%s[%s] %s", self.name, code, message)

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
        memberNames = list(self.unionSpec) + list(getattr(self, "unionInline", ()))
        members = []
        for memberName in memberNames:
            if memberName in pyXSD.classes:
                resolved = pyXSD.classes[memberName]
            else:
                resolved = ElementRepresentative.typeFromName(memberName, pyXSD)
            if resolved is None:
                logger.warning(
                    "union member type %r of %r could not be resolved and was skipped",
                    memberName,
                    self.name,
                )
                continue
            if hasattr(resolved, "_unionMembers"):
                # A union member that is itself a union: flatten.
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
            return cached

        if getattr(self, "unionSpec", None) is not None:
            union = self.makeUnionClass(pyXSD)
            self._generatedClass = union
            return union

        self.resolveAttributeGroupRefs(pyXSD)

        bases = self.getBaseList(pyXSD)
        namespace = {
            "pyXSD": pyXSD,
            "name": self.name,
            "__doc__": self.__doc__,
            # Binding policy stamped at class-build time; the binding
            # sites in SchemaBase read it to decide what to do with
            # invalid or unresolved content.
            "_parseMode_": getattr(pyXSD, "mode", ParseModes.STRICT),
        }
        if getattr(self, "hasWildcardElements", False):
            namespace["hasWildcardElements_"] = True
        if getattr(self, "hasWildcardAttributes", False):
            namespace["hasWildcardAttributes_"] = True
        if self.tagAttributes.get("abstract") == "true":
            namespace["abstract_"] = True
        # Record the XSD content category explicitly. Generated simple
        # types inherit both a primitive Python type and SchemaBase
        # (for bookkeeping), so Python inheritance cannot tell a simple
        # type from a complex one at instance-dispatch time.
        namespace["_contentKind_"] = (
            "simple" if self.__class__.__name__ == "SimpleType" else "complex"
        )
        # Derivation method and block are needed to validate xsi:type
        # overrides at instance time.
        namespace["_derivation_"] = self.getDerivation()
        blockValue = self.tagAttributes.get("block")
        if blockValue:
            namespace["_block_"] = blockValue
        # Compile the particle tree before getElements() flattens and
        # folds group-reference occurrences onto the shared descriptors.
        contentModel = compile_content_model(self, pyXSD)
        if contentModel is not None:
            namespace["_contentModel_"] = contentModel
        for element in self.getElements():
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
            else:
                # The plain-string case is the historical quirk where
                # an element named 'name' replaces the name metadata.
                namespace[element.name] = element
        for attr in self.attributes.values():
            attr.pyXSD = pyXSD
            namespace[attr.name] = attr

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
        return cls


from pyxsd.schema_base import SchemaBase  # noqa: E402
