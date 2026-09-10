import os


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
    """

    def __init__(self):
        """Creates the instances that are in the tree.

        These objects are initialized from within SchemaBase.
        """
        self._children_ = []
        self._value_ = None

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
                        print(
                            "Parser Error: There is no type in the schema that "
                            f"corresponds to the type stated in the {descriptorName} element"
                        )
                        continue

                    # for elements with primitive types
                    if not issubclass(subElCls, SchemaBase):
                        subInstance = cls.primitiveValueFor(subElCls, subElement)
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
            if os.linesep in elementTag.text.rstrip(os.linesep):
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
            print(
                f"Parser Error: the value of 'minOccurs' in {cls.name} must be greater "
                "than or equal to zero."
            )
            print(
                "The program will assign minOccurs the default value of 1 and attempt to proceed."
            )
            print()
            minOccurs = 1

        maxOccurs = elemDescriptor.getMaxOccurs()
        if maxOccurs < 0:
            print(
                f"Parser Error: the value of 'maxOccurs' in {cls.name} must be greater "
                "than or equal to zero."
            )
            print(
                "The program will assign maxOccurs the default value of 1 and attempt to proceed."
            )
            print()
            maxOccurs = 1

        if len(subElements) < minOccurs:
            print(
                "Parser Error: the program cannot find enough elements in the "
                "xml that are specified in the choice field for",
                cls.name,
            )
            print()

        elif len(subElements) > maxOccurs:
            print(
                f"Parser Error: the parser found too many elements for a choice element in {cls.name}."
            )
            print("This choice element can only have one element in it.")
            print()

        return None

    @classmethod
    def checkElementOrderInSequence(cls, descriptors, subElements):
        """Checks the element order in sequence fields to make sure that
        the order specified in the schema is preserved in the xml.

        Raises non-fatal errors when a problem is found. Checks
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
                print(
                    f"Parser Error: Order Error - Expected element name '{dname}' in different position."
                )
                print()
                continue

            if count < descriptor.getMinOccurs():
                # complain
                print(f"Parser Error: The Element '{dname}' in '{cls.name}' occurs less")
                print(
                    f"than the specified number of minOccurs ({descriptor.getMinOccurs()}) in the schema."
                )
                print("Note: it is possible that there is a problem with the order of")
                print("elements and not the minOccurs value.")
                print()
                continue

            if count > descriptor.getMaxOccurs():
                # complain
                print(f"Parser Error: The element '{dname}' in '{cls.name}' occurs more")
                print(" than the specified number of maxOccurs in the schema.")
                if descriptor.getMaxOccurs() == 1:
                    print("Your maxOccurs value is 1, which is the default value.")
                    print("Perhaps you meant to assign this variable a different value?")
                print()
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
            dataTypeVal = True
        elif dataTypeText:
            dataTypeVal = dataTypeText
        elif len(dataTypeAttrib) == 1:
            dataTypeVal = dataTypeAttrib[0][1]
        elif len(dataTypeChildren) == 1:
            dataTypeVal = dataTypeChildren[0]
        else:
            print(f"An error occurred while reading the data in the {subElement.tag} element.")
            return None

        dataTypeValInst = subElCls(dataTypeVal)
        dataTypeValInst._attribs_ = dict(subElement.attrib)
        dataTypeValInst._value_ = (
            [dataTypeText.strip()] if dataTypeText and dataTypeText.strip() else None
        )
        dataTypeValInst._children_ = dataTypeChildren

        return dataTypeValInst

    @classmethod
    def addBaseDescriptors(cls):
        """Adds attribute descriptors from classes that are bases to the
        current class.  Does this recursively down the list of bases.
        Everything returned as a dictionary.
        """
        descriptors = {}
        for key, value in vars(cls).items():
            if isinstance(value, Attribute):
                descriptors[key] = value

        for bcls in cls.__bases__:
            if hasattr(bcls, "addBaseDescriptors"):
                bclsDescriptors = bcls.addBaseDescriptors()
                for key, value in bclsDescriptors.items():
                    if key in descriptors:
                        continue
                    descriptors[key] = value
        return descriptors

    def descAttributes(self):
        """Returns a dictionary of the descriptor attributes.

        These attributes are from the schema and use descriptors, which
        are specified in the Attribute class in
        element_representatives, that help check element attribute
        values. Uses lazy evaluation by storing the descriptor
        attributes in a variable called ``_descAttrs_``, which it
        returns if this variable is set.
        """
        if "_descAttrs_" in self.__dict__:
            return self._descAttrs_
        attrs = {}
        for key, value in vars(self.__class__).items():
            if isinstance(value, Attribute):
                attrs[key] = value

        for base in self.__class__.__bases__:
            if hasattr(base, "addBaseDescriptors"):
                bclsDescriptors = base.addBaseDescriptors()
                for key, value in bclsDescriptors.items():
                    if key in attrs:
                        continue
                    attrs[key] = value

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

        if len(usedAttrs) > len(attrInElementTag):
            print(
                f"Parser Error: For an unknown reason, in {self.__class__.__name__}, the program "
                "parsed more attributes than there are in the XML file."
            )
        elif len(usedAttrs) < len(attrInElementTag):
            print(
                f"Parser Error: Not all attributes in the XML file in {self.__class__.__name__} were parsed."
            )
            print("Attributes not processed:")
            for attrET in attrInElementTag:
                if attrET not in usedAttrs:
                    print("   ", attrET)
        for descriptorAttrName in descriptorAttributeNames:
            found = False
            attrUse = descriptorAttributes[descriptorAttrName].getUse()
            for usedAttr in usedAttrs:
                if usedAttr == descriptorAttrName:
                    found = True
            if attrUse == "required" and not found:
                print(
                    f"Parser Error: the {descriptorAttrName} in the {self.__class__.__name__} element is required but was not found."
                )

    @staticmethod
    def dumpCls(cls):
        """For debugging purposes only. Prints out the contents of a
        class.

        - ``cls`` - the class to dump the contents of.
        """
        print(f" In dumpCls[{cls.__name__}] bases = {cls.__bases__} ")

        for key, value in cls.__dict__.items():
            print(f"   {key} - {value!r}")


from pyxsd.element_representatives.attribute import Attribute  # noqa: E402
