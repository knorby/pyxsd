import logging
from typing import Any, ClassVar

from pyxsd import xsi
from pyxsd.binding import BindingPolicy, ParseModes
from pyxsd.content_model import first_required_name, match_content, particle_names
from pyxsd.derivation import combinedBlock, derivationMessage, is_validly_derived
from pyxsd.validation import IssueSeverity
from pyxsd.xsd_data_types import AnySimpleType, XsdDataType, xsd_value_key

logger = logging.getLogger(__name__)


def _mode_for(cls) -> BindingPolicy:
    """The binding policy stamped on a generated class (or STRICT)."""
    return getattr(cls, "_parseMode_", ParseModes.STRICT)


class SchemaBase:
    """Serves as the base class for all schema type classes created.

    The pythonic instance tree is built from this class.  This class
    also contains the means to do non-fatal parser error checking.  A
    little bit of the work this class does is also done in the PyXSD
    parser. The schema and xml file do not line up perfectly.  The top
    level element in the schema and the schema tag both contain
    information relevant to the top-level tag in the XML. For this
    reason, the tree building/checking must be started in the same
    location the method ``makeInstanceFromTag`` is called in this
    class.

    Class creation is wired through ``__init_subclass__``: when a class
    is generated from the schema (see
    :meth:`pyxsd.element_representatives.xsd_type.XsdType.clsFor`),
    every :class:`~pyxsd.element_representatives.element.Element` and
    :class:`~pyxsd.element_representatives.attribute.Attribute`
    descriptor placed in the class body is registered under its element
    name in the class's ``_elementNames_``/``_attributeNames_`` lists.
    The descriptors themselves learn their owning class through the
    standard ``__set_name__`` protocol.

    Recoverable validation problems are recorded on the
    :class:`~pyxsd.validation.ValidationReport` owned by the running
    :class:`~pyxsd.parser.PyXSD` instance (which every generated class
    carries as its ``pyXSD`` attribute) instead of being printed. When
    no parser is attached, issues fall back to the ``pyxsd`` logging
    hierarchy.
    """

    #: Default element bookkeeping for subclasses that declare no
    #: element descriptors of their own (overridden per subclass by
    #: ``__init_subclass__``).
    _elementNames_: ClassVar[list[str]] = []

    #: Default attribute bookkeeping, like ``_elementNames_``.
    _attributeNames_: ClassVar[list[str]] = []

    # Per-instance bookkeeping consumed by the writers, the transform
    # framework, and identity checking. Assigned by the parser and
    # ``makeInstanceFromTag``; annotations only.
    _name_: str
    _attribs_: dict[str, str]
    _value_: list[str] | None

    def __init_subclass__(cls, **kwargs):
        """Collects the descriptor bookkeeping for a new subclass.

        Every ``Element``/``Attribute`` descriptor in the new class
        body is recorded, in declaration order, so the instance-tree
        machinery can find them without per-class closures. Rebinding
        a descriptor inherited from a base class has no effect on the
        base's own bookkeeping.
        """
        super().__init_subclass__(**kwargs)
        elementNames = []
        attributeNames = []
        for attrName, value in cls.__dict__.items():
            if isinstance(value, Element):
                elementNames.append(attrName)
            elif isinstance(value, Attribute):
                attributeNames.append(attrName)
        cls._elementNames_ = elementNames
        cls._attributeNames_ = attributeNames

    def __init__(self, *args):
        """Creates the instances that are in the tree.

        These objects are initialized from within SchemaBase.

        The signature accepts (and ignores) a positional value so the
        generated classes for schema-defined simple types - which
        carry SchemaBase in their method resolution order for the
        ``__init_subclass__`` bookkeeping - can be instantiated with
        the value being validated. Lexical validation happens in the
        data-type ``__new__`` before ``__init__`` is reached.
        """
        self._children_ = []
        self._value_ = None

    def __getattr__(self, name):
        """Provides a helpful error for attributes normal lookup misses.

        ``__getattr__`` is only consulted when regular lookup (the
        class descriptors, the instance dictionary, and the class
        hierarchy) has already failed, so declaring this hook cannot
        change any successful lookup. Internal names keep a plain
        ``AttributeError`` so that copy/pickle/introspection probing
        behaves as usual.
        """
        # Dunder probes from copy, pickle, and introspection: fail plainly.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            elements = [descriptor.name for descriptor in self._getElements()]
            attributes = list(self.descAttributes())
        except Exception:  # never mask the original lookup failure
            raise AttributeError(name) from None
        raise AttributeError(
            f"{type(self).__name__!r} instance has no attribute {name!r}; "
            f"the schema declares elements {elements} and attributes {attributes}. "
            "Only schema-declared names are available as attributes; use "
            "_children_/_attribs_ for the raw containers."
        )

    # ------------------------------------------------------------------
    # Validation issue plumbing
    # ------------------------------------------------------------------

    @classmethod
    def _report_issue(cls, severity, message, *, code, element=None):
        """Record a validation issue on the owning parser's report.

        Falls back to logging when the class has no attached parser
        (for example hand-written overlay classes that subclass
        SchemaBase directly).
        """
        parser = getattr(cls, "pyXSD", None)
        if parser is not None:
            if severity is IssueSeverity.ERROR:
                parser.report.add_error(message, code=code, element=element)
            else:
                parser.report.add_warning(message, code=code, element=element)
        else:
            logger.log(
                logging.ERROR if severity is IssueSeverity.ERROR else logging.WARNING,
                "%s[%s] %s",
                element or cls.__name__,
                code,
                message,
            )

    @classmethod
    def _report_error(cls, message, *, code, element=None):
        """Record an error-severity validation issue."""
        cls._report_issue(IssueSeverity.ERROR, message, code=code, element=element)

    @classmethod
    def _report_warning(cls, message, *, code, element=None):
        """Record a warning-severity validation issue."""
        cls._report_issue(IssueSeverity.WARNING, message, code=code, element=element)

    # ------------------------------------------------------------------
    # Instance tree construction
    # ------------------------------------------------------------------

    @classmethod
    def makeInstanceFromTag(cls, elementTag):
        """Takes in a schema type class and its corresponding xml element.

        It then instantiates the class, adds a name from the name in
        the xml element, and then hands the instance and the element to
        other methods to add attributes, elements, and values to this
        instance. It adds these according to the schema classes, and
        not the element. A non-fatal (when possible) error is raised
        when the xml element does not correspond to the schema class.

        Types declared ``abstract`` may not be instantiated directly
        (derived classes may); the violation is recorded on the
        validation report and parsing continues.

        - ``elementTag`` - the xml element that corresponds to ``cls``
        """
        instance = cls()
        instance._name_ = elementTag.tag.split("}")[-1]
        if cls.__dict__.get("abstract_"):
            cls._report_error(
                f"type '{cls.__name__}' is declared abstract and may not be instantiated directly",
                code="abstract-type",
                element=instance._name_,
            )
        cls.addAttributesTo(instance, elementTag)
        cls.addElementsTo(instance, elementTag)
        cls.addValueTo(instance, elementTag)

        return instance

    @classmethod
    def addAttributesTo(cls, instance, elementTag):
        """Called by ``makeInstanceFromTag()``.

        Adds attributes according to the schema by calling
        ``getAttributesFromTag()``. The attributes are then checked.
        """
        tagsUsed = instance.getAttributesFromTag(elementTag)
        instance.checkAttributes(tagsUsed, elementTag)

    def getAttributesFromTag(self, elementTag):
        """Adds attributes to the ``_attribs_`` dictionary in the
        instance.

        Only attributes in the type classes are added. All of the
        attribute values are validated against descriptors in the
        Attribute class in element_representatives. The only exception
        to this procedure is for namespace and schemaLocation tags, as
        the program currently does not have any mechanism to actually
        check these.

        - ``elementTag``: the xml element that the instance represents
        """
        self._attribs_ = {}
        usedAttributes = []
        xsiPrefix = f"{{{xsi.XSI_NAMESPACE}}}"
        # XSI-namespace attributes (xsi:nil, xsi:type, ...) are stored
        # under their conventional display spelling so the writers emit
        # valid xml (the document's own xmlns:xsi declaration, a plain
        # attribute here, keeps the output reparseable).
        for attr in elementTag.attrib:
            if "xmlns" in attr or "xsi:" in attr or attr.startswith(xsiPrefix):
                displayKey = xsi.xsi_attr_key(attr)
                setattr(self, displayKey, elementTag.attrib[attr])
                usedAttributes.append(displayKey)
                self._attribs_[displayKey] = elementTag.attrib[attr]
        for name in self.descAttributeNames():
            if name in elementTag.attrib:
                setattr(self, name, elementTag.attrib[name])
                usedAttributes.append(name)
                self._attribs_[name] = elementTag.attrib[name]
        # Attribute wildcard (xs:anyAttribute) pass-through: attributes
        # the schema does not declare are stored raw instead of being
        # left out and reported as unexpected.
        if getattr(self, "hasWildcardAttributes_", False):
            for attr, value in elementTag.attrib.items():
                if "xmlns" in attr or "xsi:" in attr or attr.startswith(xsiPrefix):
                    continue
                if attr in usedAttributes:
                    continue
                self._attribs_[attr] = value
                usedAttributes.append(attr)
        return usedAttributes

    @classmethod
    def addElementsTo(cls, instance, elementTag):
        """Checks order on the child elements, with different functions
        for ``sequence``, ``choice``, and ``all``.

        Iterates through all the elements specified in the class of the
        schema, and matches these elements with the elements from the
        xml. Redirects elements that are primitive types (integer,
        double, string, and so on) to another function. Calls
        ``makeInstanceFromTag()`` on all the children.

        Children may also match as ``substitutionGroup`` members of a
        declared element: a member element is parsed with its own type
        wherever its head element is declared, unless the head blocks
        substitution.

        - ``instance`` - the instance of ``cls`` that is having
          elements added to it.

        - ``elementTag`` - the xml element that corresponds to ``cls``
        """
        subElements = list(elementTag)

        def getSubElementName(x):
            return x.tag.split("}")[-1]

        elemDescriptors = instance._getElements()

        # No early return on childless elements: the order checkers
        # still run with an empty child list so that required content
        # (e.g. a choice with minOccurs=1) is reported when absent.

        # Substitution-group dispatch: member xml children may appear
        # wherever their head element is declared.
        substitutionGroups = cls._schemaSubstitutionGroups(elemDescriptors)
        declaredByName = {descriptor.name: descriptor for descriptor in elemDescriptors}
        declaredByExpanded = {
            getattr(descriptor, "expandedName", None): descriptor for descriptor in elemDescriptors
        }
        memberHeadMap: dict[str, str] = {}
        for headName, members in substitutionGroups.items():
            headDescriptor = declaredByName.get(headName) or declaredByExpanded.get(headName)
            localHead = headDescriptor.name if headDescriptor is not None else headName
            for member in members:
                memberHeadMap[member.name] = localHead

        # Wildcard (xs:any) pass-through: children the schema does not
        # declare are accepted and parsed generically when the type
        # declares a wildcard. Order checking only sees declared
        # children in that case.
        hasWildcard = getattr(instance, "hasWildcardElements_", False)
        if hasWildcard:
            declaredNames = {descriptor.name for descriptor in elemDescriptors}
            declaredNames.update(memberHeadMap)
            declaredChildren = [
                subElement
                for subElement in subElements
                if getSubElementName(subElement) in declaredNames
            ]
        else:
            declaredChildren = subElements

        model = getattr(instance, "_contentModel_", None)
        if model is not None:
            complete, leftover = match_content(model, declaredChildren, memberHeadMap)
        else:
            complete, leftover = False, None

        if model is None or not complete:
            sOrC = getattr(elemDescriptors[0], "sOrC", None) if elemDescriptors else None

            if sOrC == "sequence":
                cls.checkElementOrderInSequence(elemDescriptors, declaredChildren, memberHeadMap)

            elif sOrC == "choice":
                cls.checkElementOrderInChoice(elemDescriptors, declaredChildren, memberHeadMap)

            elif sOrC == "all":
                cls.checkElementOrderInAll(elemDescriptors, declaredChildren, memberHeadMap)

            if model is not None and leftover:
                # The compiled model rejected some children the legacy
                # checkers do not cover (a closed model must consume
                # every child).
                declared = particle_names(model)
                for subElement in leftover:
                    subElementName = getSubElementName(subElement)
                    head = memberHeadMap.get(subElementName, subElementName)
                    if head in declared:
                        cls._report_error(
                            f"element '{subElementName}' is not allowed in this "
                            "position in the content model",
                            code="order",
                            element=cls.__name__,
                        )
                    elif not hasWildcard:
                        cls._report_error(
                            f"element '{subElementName}' is not declared in the "
                            "content model and no wildcard allows it",
                            code="unexpected-element",
                            element=cls.__name__,
                        )

            if model is not None and not leftover:
                missing = first_required_name(model)
                if missing is not None:
                    cls._report_error(
                        f"the content model requires element '{missing}', "
                        "which is missing from the xml",
                        code="occurrence-min",
                        element=cls.__name__,
                    )

        # Children are matched (and recorded) in document order so the
        # instance tree preserves the xml's layout.
        for subElement in subElements:
            subElementName = getSubElementName(subElement)
            matched = False
            for descriptor in elemDescriptors:
                if descriptor.name != subElementName:
                    continue
                matched = True
                if descriptor.isAbstract():
                    cls._report_error(
                        f"element '{subElementName}' is declared abstract; "
                        "only its substitution group members may appear in the xml",
                        code="abstract-element",
                        element=cls.__name__,
                    )
                subElCls = cls._classForChild(descriptor, subElement)
                if subElCls is None:
                    cls._report_error(
                        "no type in the schema corresponds to the type "
                        f"stated in the '{subElementName}' element",
                        code="unknown-type",
                        element=cls.__name__,
                    )
                    if _mode_for(cls).unresolved_type == "generic":
                        instance._children_.append(cls.makeGenericInstance(subElement))
                    break
                cls._addChildInstance(instance, subElement, subElCls, descriptor)
                break

            if not matched and substitutionGroups:
                matched = cls._addSubstitutionMember(instance, subElement, elemDescriptors)

            if not matched and (hasWildcard or _mode_for(cls).undeclared_content == "generic"):
                wildcardInstance = cls.makeGenericInstance(subElement)
                instance._children_.append(wildcardInstance)
        return instance

    @classmethod
    def _classForChild(cls, descriptor, subElement):
        """Resolves the class used to build one matched child element.

        Uses the declared type by default; ``xsi:type`` on the xml
        element dispatches to another schema type. Unresolvable
        ``xsi:type`` values are recorded on the validation report and
        the declared type is kept. Returns ``None`` when no type can
        be resolved.
        """
        subElCls = descriptor.getType() if descriptor is not None else None
        xsiTypeName = xsi.xsi_type_name(subElement)
        if xsiTypeName is None:
            return subElCls
        pyXSD = getattr(cls, "pyXSD", None)
        resolved = ElementRepresentative.typeFromName(xsiTypeName, pyXSD)
        if resolved is not None:
            blocked = combinedBlock(
                descriptor.getBlock() if descriptor is not None else None,
                subElCls,
            )
            reason = is_validly_derived(resolved, subElCls, blocked)
            if reason is not None:
                cls._report_error(
                    derivationMessage(resolved, subElCls, reason),
                    code="xsi-type",
                    element=cls.__name__,
                )
                return subElCls
            logger.debug(
                "Element %r dispatched via xsi:type to %s", subElement.tag, resolved.__name__
            )
            return resolved
        cls._report_error(
            f"xsi:type '{xsiTypeName}' on element "
            f"'{subElement.tag.split('}')[-1]}' does not correspond to a "
            "type in the schema",
            code="xsi-type",
            element=cls.__name__,
        )
        return subElCls

    @classmethod
    def _addChildInstance(cls, instance, subElement, subElCls, descriptor):
        """Builds and stores the instance for one matched child element.

        Handles ``xsi:nil`` (nillable elements carry no content to
        validate), ``fixed`` value checking on simple content, and the
        primitive/complex split. Appends the built instance to the
        parent's ``_children_`` and, for primitive content, exposes it
        as an instance attribute.
        """
        subElementName = subElement.tag.split("}")[-1]
        nilled = xsi.xsi_nil_is_true(subElement)
        if nilled and not descriptor.isNillable():
            cls._report_error(
                f"element '{subElementName}' carries xsi:nil but its declaration is not nillable",
                code="nil",
                element=cls.__name__,
            )
            nilled = False

        # for elements with primitive types
        contentKind = getattr(subElCls, "_contentKind_", None)
        isComplex = (
            contentKind == "complex"
            if contentKind is not None
            else issubclass(subElCls, SchemaBase)
        )
        if not isComplex:
            if nilled:
                subInstance = cls._nilPrimitive(subElCls, subElement)
            else:
                subInstance = cls._primitiveForElement(subElCls, subElement, descriptor)
            if subInstance is not None:
                # invalid values are skipped; the error is
                # already in the report
                subInstance._name_ = subElementName
                # Identity constraints are checked against this
                # descriptor after the tree is fully bound.
                subInstance._descriptor_ = descriptor
                subInstance._nil_ = nilled
                instance._children_.append(subInstance)
                setattr(instance, subElementName, subInstance)
                if not nilled:
                    cls._checkFixedElement(descriptor, subElCls, subInstance, subElementName)
            return None

        subInstance = subElCls.makeInstanceFromTag(subElement)
        subInstance._name_ = subElementName
        subInstance._descriptor_ = descriptor
        subInstance._nil_ = nilled
        instance._children_.append(subInstance)
        return None

    @classmethod
    def _primitiveForElement(cls, subElCls, subElement, descriptor):
        """Builds a typed instance for a primitive-typed child element.

        An empty element (no text, no children) takes its declared
        ``default`` value, or its ``fixed`` value when there is no
        default; otherwise the lexical content is validated normally
        (see ``primitiveValueFor``). Invalid values yield ``None``
        with the error already on the validation report.
        """
        emptyContent = subElement.text is None and not list(subElement)
        if emptyContent:
            forced = descriptor.getDefault()
            if forced is None:
                forced = descriptor.getFixed()
            if forced is not None:
                return cls._valueForcedPrimitive(subElCls, subElement, forced, "default")
        return cls.primitiveValueFor(subElCls, subElement)

    @classmethod
    def _valueForcedPrimitive(cls, subElCls, subElement, forcedValue, code):
        """Builds a typed instance for a forced (default/fixed) value.

        The forced value must be valid for the element's type; a
        violation is a schema problem and is recorded with the given
        report code (``default`` or ``fixed-element``).
        """
        try:
            instance = subElCls(forcedValue)
        except (TypeError, ValueError) as e:
            cls._report_error(
                f"the forced value {forcedValue!r} of the "
                f"'{subElement.tag.split('}')[-1]}' element is not valid "
                f"for its type: {e}",
                code=code,
                element=cls.__name__,
            )
            return None
        instance._attribs_ = {
            xsi.xsi_attr_key(key): value for key, value in subElement.attrib.items()
        }
        instance._value_ = None
        instance._children_ = list(subElement)
        return instance

    @classmethod
    def _nilPrimitive(cls, subElCls, subElement):
        """Builds an unvalidated instance for a nillable primitive element.

        A nillable element may carry no content, so no lexical form is
        available; the bare instance keeps the raw attributes (which
        include ``xsi:nil``) for the writers.
        """
        try:
            subInstance = subElCls._unvalidated()
        except Exception:
            cls._report_error(
                f"could not build a nil instance for the '{subElement.tag.split('}')[-1]}' element",
                code="value",
                element=cls.__name__,
            )
            return None
        subInstance._attribs_ = {
            xsi.xsi_attr_key(key): value for key, value in subElement.attrib.items()
        }
        subInstance._value_ = None
        subInstance._children_ = list(subElement)
        return subInstance

    @classmethod
    def _checkFixedElement(cls, descriptor, subElCls, subInstance, subElementName):
        """Validates a primitive element's value against ``fixed``."""
        fixed = descriptor.getFixed()
        if fixed is None:
            return None
        try:
            fixedInstance = subElCls(fixed)
        except Exception:
            cls._report_error(
                f"fixed value {fixed!r} of element '{subElementName}' is not valid for its type",
                code="fixed-element",
                element=cls.__name__,
            )
            return None
        if xsd_value_key(subInstance) != xsd_value_key(fixedInstance):
            cls._report_error(
                f"element '{subElementName}' has a value that conflicts "
                f"with its fixed value {fixed!r}",
                code="fixed-element",
                element=cls.__name__,
            )
        return None

    @classmethod
    def _addSubstitutionMember(cls, instance, subElement, elemDescriptors):
        """Parses a child as a substitution-group member, if it is one.

        Matches the xml child name against the members registered
        under each declared element (the head). Returns True when the
        child was handled. Members blocked by the head's ``block``
        attribute are reported and rejected.
        """
        subElementName = subElement.tag.split("}")[-1]
        declared = {descriptor.name: descriptor for descriptor in elemDescriptors}
        declaredExpanded = {
            getattr(descriptor, "expandedName", None): descriptor for descriptor in elemDescriptors
        }
        for headName, members in cls._schemaSubstitutionGroups(elemDescriptors).items():
            headDescriptor = declared.get(headName) or declaredExpanded.get(headName)
            if headDescriptor is None:
                continue
            for memberER in members:
                if memberER.name != subElementName:
                    continue
                block = headDescriptor.getBlock()
                if block and ("substitution" in block.split() or block == "#all"):
                    cls._report_error(
                        f"substitution-group member '{subElementName}' is "
                        f"blocked by head element '{headName}' (block={block!r})",
                        code="blocked",
                        element=cls.__name__,
                    )
                    return False
                if memberER.tagAttributes.get("type"):
                    subElCls = memberER.getType()
                else:
                    subElCls = headDescriptor.getType()
                if subElCls is None:
                    return False
                # An xsi:type on the member overrides the member's
                # declared type, provided it is validly derived.
                xsiTypeName = xsi.xsi_type_name(subElement)
                if xsiTypeName is not None:
                    override = ElementRepresentative.typeFromName(
                        xsiTypeName, getattr(cls, "pyXSD", None)
                    )
                    if override is not None:
                        blocked = combinedBlock(memberER.getBlock(), subElCls)
                        reason = is_validly_derived(override, subElCls, blocked)
                        if reason is None:
                            subElCls = override
                        else:
                            cls._report_error(
                                derivationMessage(override, subElCls, reason),
                                code="xsi-type",
                                element=cls.__name__,
                            )
                # The member declaration supplies the value constraints
                # (nillable, default, fixed, identity); the head's
                # particle supplied the occurrence match.
                cls._addChildInstance(instance, subElement, subElCls, memberER)
                return True
        return False

    @staticmethod
    def _schemaSubstitutionGroups(elemDescriptors):
        """Returns the substitution-group map of the owning schema.

        Used by both the dispatch path and the declared-name
        computation. Overlay classes without ER ancestry get an empty
        map.
        """
        if not elemDescriptors:
            return {}
        try:
            schemaER = elemDescriptors[0].getSchema()
        except Exception:
            return {}
        return getattr(schemaER, "substitutionGroups", None) or {}

    @classmethod
    def addValueTo(cls, instance, elementTag):
        """Checks to see if the tag has a value, and assigns it to the
        element instance if it does.

        Uses the ElementTree function ``.text`` to retrieve this
        information from the tag.
        """
        if elementTag.text:
            instance._value_ = []
            if "\n" in elementTag.text.rstrip("\n"):
                dataEntry = elementTag.text.splitlines()
                for line in dataEntry:
                    line = line.strip()
                    if line:
                        instance._value_.append(line)
            else:
                stripped = elementTag.text.strip()
                if stripped:
                    instance._value_.append(stripped)
            instance._value_ = instance._value_ if instance._value_ else None

    @classmethod
    def checkElementOrderInChoice(cls, descriptors, subElements, memberHeadMap):
        """Checks to see that elements in a choice field follow the rules
        of such a field.

        Finds the ``choice`` compositor that owns the descriptors and
        checks the total child count against the choice's own
        ``minOccurs``/``maxOccurs``. Each element in the choice is then
        checked against its own ``maxOccurs`` (``minOccurs`` is not
        applicable to individual branches: any branch may be the one
        that does not appear).

        - ``descriptors`` - the schema-specified elements that make up
          the choice.

        - ``subElements`` - the declared children of an element being
          processed in ``addElementsTo()``.

        - ``memberHeadMap`` - substitution-group member name to head
          name, so member children count toward the head's limits.
        """
        subElementNames = [elem.tag.split("}")[-1] for elem in subElements]
        subElementNames = [memberHeadMap.get(name, name) for name in subElementNames]

        choiceER = None
        for descriptor in descriptors:
            parent = getattr(descriptor, "parent", None)
            if parent is not None and parent.__class__.__name__ == "Choice":
                choiceER = parent
                break

        if choiceER is not None:
            minOccurs = choiceER.getMinOccurs()
            maxOccurs = choiceER.getMaxOccurs()
        else:
            # Defensive fallback: derive the limits from the first
            # descriptor (the pre-phase-8 behavior).
            minOccurs = descriptors[0].getMinOccurs()
            maxOccurs = descriptors[0].getMaxOccurs()

        if minOccurs < 0:
            cls._report_warning(
                "the value of 'minOccurs' must be greater than or equal to "
                "zero; continuing with the default value of 1",
                code="schema",
                element=cls.__name__,
            )
            minOccurs = 1

        if maxOccurs < 0:
            cls._report_warning(
                "the value of 'maxOccurs' must be greater than or equal to "
                "zero; continuing with the default value of 1",
                code="schema",
                element=cls.__name__,
            )
            maxOccurs = 1

        if len(subElementNames) < minOccurs:
            cls._report_error(
                "the xml does not contain enough elements for the choice "
                f"(minOccurs is {minOccurs})",
                code="occurrence-min",
                element=cls.__name__,
            )

        elif len(subElementNames) > maxOccurs:
            cls._report_error(
                f"the xml contains too many elements for the choice (maxOccurs is {maxOccurs})",
                code="occurrence-max",
                element=cls.__name__,
            )

        if choiceER is not None:
            for descriptor in descriptors:
                count = subElementNames.count(descriptor.name)
                if count > descriptor.getMaxOccurs():
                    cls._report_error(
                        f"element '{descriptor.name}' occurs more times than "
                        f"maxOccurs ({descriptor.getMaxOccurs()}) allows "
                        "within the choice",
                        code="occurrence-max",
                        element=cls.__name__,
                    )

        return None

    @classmethod
    def checkElementOrderInAll(cls, descriptors, subElements, memberHeadMap):
        """Checks the occurrence counts in an ``all`` content model.

        Unlike ``sequence``, ``all`` does not constrain the order of
        children, so each element's occurrence count is checked
        against its ``minOccurs``/``maxOccurs`` limits regardless of
        position. Children that match no declared element are ignored
        here (wildcard pass-through handles them upstream).

        - ``descriptors`` - the schema-specified elements of the
          ``all`` compositor.

        - ``subElements`` - the declared children of an element being
          processed in ``addElementsTo()``.

        - ``memberHeadMap`` - substitution-group member name to head
          name, so member children count toward the head's limits.
        """
        subElementNames = [elem.tag.split("}")[-1] for elem in subElements]
        subElementNames = [memberHeadMap.get(name, name) for name in subElementNames]
        for descriptor in descriptors:
            count = subElementNames.count(descriptor.name)
            if count < descriptor.getMinOccurs():
                cls._report_error(
                    f"element '{descriptor.name}' occurs fewer times than "
                    f"minOccurs ({descriptor.getMinOccurs()}) requires",
                    code="occurrence-min",
                    element=cls.__name__,
                )
                continue
            if count > descriptor.getMaxOccurs():
                cls._report_error(
                    f"element '{descriptor.name}' occurs more times than "
                    f"maxOccurs ({descriptor.getMaxOccurs()}) allows",
                    code="occurrence-max",
                    element=cls.__name__,
                )

    @classmethod
    def checkElementOrderInSequence(cls, descriptors, subElements, memberHeadMap):
        """Checks the element order in sequence fields to make sure that
        the order specified in the schema is preserved in the xml.

        Records non-fatal issues when a problem is found. Checks
        minOccurs and maxOccurs on each element as well.

        - ``descriptors`` - a list of schema-specified elements that
          define parameters for an element. Called ``descriptors``
          because the program takes advantage of descriptors in Python
          to help check the data. These descriptors are in the Element
          class in element_representatives.

        - ``subElements`` - all of the children of an element that is
          being processed in ``addElementsTo()``. Correspond to
          elements in ``descriptors``.

        - ``memberHeadMap`` - substitution-group member name to head
          name, so member children are validated in place of the head.
        """
        descriptorNames = [d.name for d in descriptors]
        subElementNames = [elem.tag.split("}")[-1] for elem in subElements]
        subElementNames = [memberHeadMap.get(name, name) for name in subElementNames]
        for index in range(0, len(descriptors)):
            descriptor = descriptors[index]
            dname = descriptorNames[index]
            count, subElementNames = cls.consume(dname, subElementNames)

            if count == 0:
                if descriptor.getMinOccurs() == 0 and dname not in subElementNames:
                    continue
                cls._report_error(
                    f"order error - expected element '{dname}' in a different position",
                    code="order",
                    element=cls.__name__,
                )
                continue

            if count < descriptor.getMinOccurs():
                cls._report_error(
                    f"element '{dname}' occurs fewer times than minOccurs "
                    f"({descriptor.getMinOccurs()}) requires; this may also "
                    "indicate an ordering problem",
                    code="occurrence-min",
                    element=cls.__name__,
                )
                continue

            if count > descriptor.getMaxOccurs():
                cls._report_error(
                    f"element '{dname}' occurs more times than maxOccurs "
                    f"({descriptor.getMaxOccurs()}) allows",
                    code="occurrence-max",
                    element=cls.__name__,
                )
                continue

    @classmethod
    def consume(cls, dname, subElements):
        """Used to check the number of times an element type in the schema
        is used with the xml elements. Used by
        ``checkElementOrderInSequence()``.

        - ``dname`` - the name of the descriptor that is currently
          being checked.

        - ``subElements`` - the list of subElement names being checked.
        """
        count = 0
        while len(subElements) > 0 and subElements[0] == dname:
            subElements = subElements[1:]
            count += 1

        return count, subElements

    @classmethod
    def primitiveValueFor(cls, subElCls, subElement):
        """Used to check and assign primitive values to an instance.

        Called by ``addElementsTo()``.  NOTE: this method may not work
        correctly for all elements with primitive data types.

        - ``subElCls`` - the schema type class that corresponds to the
          subElement that is being processed.

        - ``subElement`` - the subElement that has a primitive data
          type.
        """
        dataTypeChildren = list(subElement)
        dataTypeText = subElement.text

        if dataTypeChildren:
            # A simple-typed element cannot contain child elements, and
            # an undeclared attribute must never stand in for its text
            # value.
            cls._report_error(
                f"the '{subElement.tag.split('}')[-1]}' element has a "
                "simple type but contains child elements",
                code="unexpected-element",
                element=cls.__name__,
            )
            return None

        # Empty simple content is the empty lexical form: valid for
        # string types and invalid for everything else (XSD has no
        # 'empty element means true' convention, though 0.1 built
        # True here, making <x/> silently read as the string 'True').
        dataTypeVal = dataTypeText if dataTypeText is not None else ""

        try:
            dataTypeValInst = subElCls(dataTypeVal)
        except (TypeError, ValueError) as e:
            cls._report_error(
                f"the value of the '{subElement.tag.split('}')[-1]}' element "
                f"is not valid for its type: {e}",
                code="value",
                element=cls.__name__,
            )
            if _mode_for(cls).invalid_value == "raw":
                return cls._rawPrimitiveValue(subElement, dataTypeText)
            return None
        dataTypeValInst._attribs_ = dict(subElement.attrib)
        dataTypeValInst._value_ = (
            [dataTypeText.strip()] if dataTypeText and dataTypeText.strip() else None
        )
        dataTypeValInst._children_ = dataTypeChildren

        return dataTypeValInst

    @classmethod
    def _rawPrimitiveValue(cls, subElement, dataTypeText):
        """Binds an invalid lexical value as an unvalidated string.

        Used by the ``raw`` invalid-value policy so a data-mapping user
        keeps the original text (and the report still records the
        problem). The stored value keeps the document's exact spelling
        rather than the stripped lexical form.
        """
        instance: Any = AnySimpleType(dataTypeText if dataTypeText is not None else "")
        instance._attribs_ = dict(subElement.attrib)
        instance._value_ = [dataTypeText] if dataTypeText and dataTypeText.strip() else None
        instance._children_ = []
        return instance

    @classmethod
    def makeGenericInstance(cls, elementTag):
        """Builds a pass-through instance for wildcard (``xs:any``)
        content.

        Undeclared children permitted by a wildcard are stored raw:
        attributes keep their lexical values, text is split like
        untyped data (see ``addValueTo``), and children are recursed
        generically. Called by ``addElementsTo()`` when the
        instance's type declares a wildcard.

        - ``elementTag`` - the undeclared xml element to store raw.
        """
        instance = SchemaBase()
        instance._name_ = elementTag.tag.split("}")[-1]
        instance._attribs_ = dict(elementTag.attrib)
        cls.addValueTo(instance, elementTag)
        instance._children_ = [cls.makeGenericInstance(child) for child in elementTag]
        return instance

    # ------------------------------------------------------------------
    # Descriptor access
    # ------------------------------------------------------------------

    def _getElements(self):
        """Returns the element descriptors visible to this instance.

        Walks the MRO of the instance's class collecting each class's
        own ``Element`` descriptors, least-derived first, so an
        extension's content model comes out in XSD order: base
        elements before extension elements. A derived declaration
        shadows an inherited element with the same name (later,
        more-derived assignments overwrite earlier ones while keeping
        the original position).
        """
        ordered = {}
        for klass in reversed(type(self).__mro__):
            for name in klass.__dict__.get("_elementNames_", ()):
                ordered[name] = klass.__dict__[name]
        return list(ordered.values())

    def descAttributes(self):
        """Returns a dictionary of the attribute descriptors.

        These attributes are from the schema and use descriptors, which
        are specified in the Attribute class in
        element_representatives, that help check element attribute
        values. Walks the MRO collecting each class's own attribute
        descriptors (derived classes shadow bases by name). Uses lazy
        evaluation by storing the descriptor attributes in a variable
        called ``_descAttrs_``, which it returns if this variable is
        set.
        """
        if "_descAttrs_" in self.__dict__:
            return self._descAttrs_
        attrs = {}
        for klass in type(self).__mro__:
            for name in klass.__dict__.get("_attributeNames_", ()):
                if name not in attrs:
                    attrs[name] = klass.__dict__[name]

        self.__dict__["_descAttrs_"] = attrs

        return attrs

    def descAttributeNames(self):
        """Returns a list that has all of the names of attribute
        descriptors. Calls ``descAttributes()``, and returns a list of
        the keys from that dictionary.
        """
        return list(self.descAttributes().keys())

    def checkAttributes(self, usedAttrs, elementTag):
        """Checks to see that required attributes are used in the xml,
        and does other such checks on the attributes.

        Note: the attribute descriptors check the values in element
        attributes.

        - ``usedAttrs`` - a list containing the names of attributes
          that were put into the instance.

        - ``elementTag`` - the ElementTree tag for the instance that is
          being checked.
        """
        descriptorAttributes = self.descAttributes()

        descriptorAttributeNames = self.descAttributeNames()

        attrInElementTag = [xsi.xsi_attr_key(attr) for attr in elementTag.attrib]

        elementName = getattr(self, "_name_", None) or self.__class__.__name__

        if len(usedAttrs) > len(attrInElementTag):
            self._report_warning(
                "the parser recorded more attributes than the xml file contains",
                code="internal",
                element=elementName,
            )
        elif len(usedAttrs) < len(attrInElementTag):
            for attrET in attrInElementTag:
                if attrET not in usedAttrs:
                    self._report_warning(
                        f"attribute '{attrET}' is not declared in the schema and was not parsed",
                        code="unexpected-attribute",
                        element=elementName,
                    )
        for descriptorAttrName in descriptorAttributeNames:
            found = False
            attrUse = descriptorAttributes[descriptorAttrName].getUse()
            for usedAttr in usedAttrs:
                if usedAttr == descriptorAttrName:
                    found = True
            if attrUse == "required" and not found:
                self._report_error(
                    f"attribute '{descriptorAttrName}' is required but was not found",
                    code="missing-attribute",
                    element=elementName,
                )
            if found and attrUse == "prohibited":
                self._report_error(
                    f"attribute '{descriptorAttrName}' is prohibited and "
                    "must not appear in the xml",
                    code="prohibited-attribute",
                    element=elementName,
                )
            attributeDescriptor = descriptorAttributes[descriptorAttrName]
            if found:
                self._checkFixedAttribute(attributeDescriptor, self, elementName)
            else:
                self._applyAttributeDefault(attributeDescriptor, self, elementName)

    def _checkFixedAttribute(self, attributeDescriptor, instance, elementName):
        """Validates a present attribute's value against ``fixed``.

        Both values are compared as typed values (through the
        attribute's type), so boolean spellings like 'true'/'1' agree.
        """
        fixed = attributeDescriptor.getFixed()
        if fixed is None:
            return None
        attributeType = attributeDescriptor.getType()
        if not issubclass(attributeType, XsdDataType):
            return None  # complex-typed attributes have no lexical fixed value
        stored = instance.__dict__.get(attributeDescriptor.name)
        try:
            fixedInstance = attributeType(fixed)
        except Exception:
            self._report_error(
                f"fixed value {fixed!r} of attribute '{attributeDescriptor.name}' "
                "is not valid for its type",
                code="fixed-attribute",
                element=elementName,
            )
            return None
        if xsd_value_key(stored) != xsd_value_key(fixedInstance):
            self._report_error(
                f"attribute '{attributeDescriptor.name}' has a value that "
                f"conflicts with its fixed value {fixed!r}",
                code="fixed-attribute",
                element=elementName,
            )
        return None

    def _applyAttributeDefault(self, attributeDescriptor, instance, elementName):
        """Applies ``default``/``fixed`` values for an absent attribute.

        Per XSD 1.0, an absent attribute whose declaration carries a
        default (or a fixed value) takes that value. The value is
        applied through the attribute descriptor, so it is validated
        and stored typed; it is deliberately not added to the raw
        ``_attribs_`` container, keeping xml output faithful to the
        input document.
        """
        default = attributeDescriptor.getDefault()
        fixed = attributeDescriptor.getFixed()
        value = default if default is not None else fixed
        if value is None:
            return None
        setattr(instance, attributeDescriptor.name, value)
        return None

    @staticmethod
    def dumpCls(cls):
        """For debugging purposes only. Logs the contents of a class at
        debug level.

        - ``cls`` - the class to dump the contents of.
        """
        logger.debug("In dumpCls[%s] bases = %s", cls.__name__, cls.__bases__)

        for key, value in cls.__dict__.items():
            logger.debug("  %s - %r", key, value)


from pyxsd.element_representatives.attribute import Attribute  # noqa: E402
from pyxsd.element_representatives.element import Element  # noqa: E402
from pyxsd.element_representatives.element_representative import (  # noqa: E402
    ElementRepresentative,
)
