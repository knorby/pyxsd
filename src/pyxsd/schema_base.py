import logging
from typing import ClassVar

from pyxsd.validation import IssueSeverity

logger = logging.getLogger(__name__)


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

    def __init__(self):
        """Creates the instances that are in the tree.

        These objects are initialized from within SchemaBase.
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

        - ``elementTag`` - the xml element that corresponds to ``cls``
        """
        instance = cls()
        instance._name_ = elementTag.tag.split("}")[-1]
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
        for attr in elementTag.attrib:
            if "xmlns" in attr or "xsi:" in attr:
                setattr(self, attr, elementTag.attrib[attr])
                usedAttributes.append(attr)
                self._attribs_[attr] = elementTag.attrib[attr]
        for name in self.descAttributeNames():
            if name in elementTag.attrib:
                setattr(self, name, elementTag.attrib[name])
                usedAttributes.append(name)
                self._attribs_[name] = elementTag.attrib[name]
        return usedAttributes

    @classmethod
    def addElementsTo(cls, instance, elementTag):
        """Checks order on the child elements, with different functions
        for ``sequence`` and ``choice``.

        Iterates through all the elements specified in the class of the
        schema, and matches these elements with the elements from the
        xml. Redirects elements that are primitive types (integer,
        double, string, and so on) to another function. Calls
        ``makeInstanceFromTag()`` on all the children.

        - ``instance`` - the instance of ``cls`` that is having
          elements added to it.

        - ``elementTag`` - the xml element that corresponds to ``cls``
        """
        subElements = list(elementTag)

        def getSubElementName(x):
            return x.tag.split("}")[-1]

        elemDescriptors = instance._getElements()

        if not subElements:  # This element has no children
            return None

        if elemDescriptors[0].sOrC == "sequence":
            cls.checkElementOrderInSequence(elemDescriptors, subElements)

        if elemDescriptors[0].sOrC == "choice":
            cls.checkElementOrderInChoice(elemDescriptors[0], subElements)

        for descriptor in elemDescriptors:
            descriptorName = descriptor.name
            for subElement in subElements:
                subElementName = getSubElementName(subElement)
                if descriptorName == subElementName:
                    subElCls = descriptor.getType()

                    if subElCls is None:  # An Error Message
                        cls._report_error(
                            "no type in the schema corresponds to the type "
                            f"stated in the '{descriptorName}' element",
                            code="unknown-type",
                            element=cls.__name__,
                        )
                        continue

                    # for elements with primitive types
                    if not issubclass(subElCls, SchemaBase):
                        subInstance = cls.primitiveValueFor(subElCls, subElement)
                        if subInstance is None:
                            # invalid value; the error is already in
                            # the report, so skip the child
                            continue
                        subInstance._name_ = subElementName
                        instance._children_.append(subInstance)
                        setattr(instance, subElementName, subInstance)
                        continue

                    subInstance = subElCls.makeInstanceFromTag(subElement)
                    subInstance._name_ = subElementName
                    instance._children_.append(subInstance)
        return instance

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
    def checkElementOrderInChoice(cls, elemDescriptor, subElements):
        """Checks to see that elements in a choice field follow the rules
        of such a field.

        Gets minOccurs and maxOccurs from the choice element in the
        schema, and checks the number of elements from there.

        - ``subElements`` - all of the children of an element that is
          being processed in ``addElementsTo()``.
        """
        minOccurs = elemDescriptor.getMinOccurs()
        if minOccurs < 0:
            cls._report_warning(
                "the value of 'minOccurs' must be greater than or equal to "
                "zero; continuing with the default value of 1",
                code="schema",
                element=cls.__name__,
            )
            minOccurs = 1

        maxOccurs = elemDescriptor.getMaxOccurs()
        if maxOccurs < 0:
            cls._report_warning(
                "the value of 'maxOccurs' must be greater than or equal to "
                "zero; continuing with the default value of 1",
                code="schema",
                element=cls.__name__,
            )
            maxOccurs = 1

        if len(subElements) < minOccurs:
            cls._report_error(
                "the xml does not contain enough elements for the choice "
                f"(minOccurs is {minOccurs})",
                code="occurrence-min",
                element=cls.__name__,
            )

        elif len(subElements) > maxOccurs:
            cls._report_error(
                f"the xml contains too many elements for the choice (maxOccurs is {maxOccurs})",
                code="occurrence-max",
                element=cls.__name__,
            )

        return None

    @classmethod
    def checkElementOrderInSequence(cls, descriptors, subElements):
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
        """
        descriptorNames = [d.name for d in descriptors]
        subElementNames = [elem.tag.split("}")[-1] for elem in subElements]
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
        dataTypeAttrib = subElement.items()

        if dataTypeText is None and not dataTypeAttrib and not dataTypeChildren:
            # empty simple content: the empty lexical form is valid for
            # string types and invalid for everything else (XSD has no
            # 'empty element means true' convention, though 0.1 built
            # True here, making <x/> silently read as the string 'True')
            dataTypeVal = ""
        elif dataTypeText:
            dataTypeVal = dataTypeText
        elif len(dataTypeAttrib) == 1:
            dataTypeVal = dataTypeAttrib[0][1]
        elif len(dataTypeChildren) == 1:
            dataTypeVal = dataTypeChildren[0]
        else:
            cls._report_error(
                "an error occurred while reading the data in the "
                f"'{subElement.tag.split('}')[-1]}' element",
                code="value",
                element=cls.__name__,
            )
            return None

        try:
            dataTypeValInst = subElCls(dataTypeVal)
        except (TypeError, ValueError) as e:
            cls._report_error(
                f"the value of the '{subElement.tag.split('}')[-1]}' element "
                f"is not valid for its type: {e}",
                code="value",
                element=cls.__name__,
            )
            return None
        dataTypeValInst._attribs_ = dict(subElement.attrib)
        dataTypeValInst._value_ = (
            [dataTypeText.strip()] if dataTypeText and dataTypeText.strip() else None
        )
        dataTypeValInst._children_ = dataTypeChildren

        return dataTypeValInst

    # ------------------------------------------------------------------
    # Descriptor access
    # ------------------------------------------------------------------

    def _getElements(self):
        """Returns the element descriptors visible to this instance.

        Walks the MRO of the instance's class, most-derived first, and
        collects each class's own ``Element`` descriptors in
        declaration order. A subclass declaration shadows an inherited
        element with the same name. (For schemas that derive types with
        ``extension``, this makes base elements reachable from the
        derived class; the base's own instances are unaffected.)
        """
        elements = []
        seen = set()
        for klass in type(self).__mro__:
            for name in klass.__dict__.get("_elementNames_", ()):
                if name not in seen:
                    seen.add(name)
                    elements.append(klass.__dict__[name])
        return elements

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

        attrInElementTag = list(elementTag.attrib.keys())

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
