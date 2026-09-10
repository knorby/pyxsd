from pyxsd.element_representatives.element_representative import ElementRepresentative


class Element(ElementRepresentative):
    """The class for the element tag.

    The element tag and the attribute tag are the most important in the
    xml and in the program, so this class contains some machinery that
    many of the other classes do not have. The element and attribute
    classes contain descriptor methods. By specifying ``__get__``,
    ``__set__``, and ``__delete__`` (with ``__get__`` and ``__set__``
    being the most important), these methods specify how a variable is
    set and how it is retrieved. Any modification of these methods
    should be made under extreme caution! If some variable is set to
    some value that does not match the specifications in the schema, an
    error will be raised. These methods add a powerful layer of
    functionality with a small amount of code; however, these functions
    are almost invisible unless they raise an error, so developers
    should bear in mind these methods when modifying the program.
    """

    def __init__(self, xsdElement, parent):
        """Adds itself to the element list in its parent.

        See ElementRepresentative for documentation.
        """
        super().__init__(xsdElement, parent)
        parent.elements.append(self)

    def getType(self):
        """Returns its type from the class dictionary in PyXSD.

        The instance of PyXSD is attached to every element and attribute
        while the classes for the schema types are being built.
        Clearly, this function is used after the main ER run.
        """
        if "type" not in self.__dict__:
            raise TypeError(f"Element.getType() Error: type is not in {self.name}'s dictionary.")

        if self.type in self.pyXSD.classes:
            return self.pyXSD.classes[self.type]

        return self.typeFromName(self.type, self.pyXSD)

    def processChildren(self):
        """There is a special ``processChildren()`` here to handle special
        types, which can be declared as a child of an element. If an
        element child can exist that is not a type, then this function
        will screw it up; however, as far as the developers knew at the
        time of writing this program, they cannot.
        """
        children = list(self.xsdElement)

        if not children:
            return None

        for child in children:
            processedChild = ElementRepresentative.factory(child, self)
            self.processedChildren.append(processedChild)
            self.type = processedChild.name
            self.tagAttributes["type"] = self.type
            # NOTE: the factory call above already processed the child's
            # children inside ElementRepresentative.__init__; do not
            # call processedChild.processChildren() again here (the old
            # code did, which constructed every grandchild ER twice and
            # double-registered sequences/elements).
        return None

    def __str__(self):
        """Prints its name in a form that allows for quick identification
        of an element, without needing a bulky name that does not match
        the name used.
        """
        return f"{self.getContainingTypeName()}|{self.__class__.__name__}|{self.name}"

    def __get__(self, obj, objtype=None):
        """Gets an element value from the obj's dictionary.

        Returns its value if it has one; returns the default value if
        it does not.

        See the Python documentation for full documentation on
        descriptors.
        """
        if self.name in obj.__dict__:
            return obj.__dict__[self.name]

        default = getattr(self, "default", None)
        return default

    def __set__(self, obj, value):
        """Sets an element's name to the element in the obj's dictionary.

        If multiple elements exist, sets it to a list. If it is not an
        element, raises an error. Has code for the case when it is a
        dictionary, but there is no case in which a dictionary would be
        used.

        See the Python documentation for full documentation on
        descriptors.
        """
        if not isinstance(value, self.getType()):
            raise TypeError(f"{value!r} is not an instance of the element's type")

        value = obj.__dict__.get(self.name, None)

        if self.isList():
            if value is None:
                obj.__dict__[self.name] = []
            obj.__dict__[self.name].append(value)
            return None

        if self.isDict() and value is None:
            obj.__dict__[self.name] = {}
            obj.__dict__[self.name][obj.id] = value
            return None
        obj.__dict__[self.name] = value
        return None

    def __delete__(self, obj):
        """Deletes an entry from the dictionary.

        See the Python documentation for full documentation on
        descriptors.
        """
        del obj.__dict__[self.name]

    def isDict(self):
        """Returns false. Placeholder function for possible future
        addition of the case where an element could best be expressed
        as a dictionary.
        """
        return False

    def isList(self):
        """Returns true if maxOccurs is greater than one.

        If it is true, treats all of the elements that are from the
        schema definition as a list. Otherwise returns false.
        """
        maxOccurs = self.getMaxOccurs()
        return maxOccurs > 1

    def getMinOccurs(self):
        """Returns an integer value for ``minOccurs``.

        If no ``minOccurs`` has been set, uses the default of 1.
        """
        return int(getattr(self, "minOccurs", 1))

    def getMaxOccurs(self):
        """Returns an integer value for ``maxOccurs``.

        If no ``maxOccurs`` has been set, uses the default of 1. If
        ``maxOccurs`` is set to 'unbounded', returns 99999, since this
        should cover about every case in which someone would use
        'unbounded'.
        """
        maxOccurs = getattr(self, "maxOccurs", 1)
        if maxOccurs == "unbounded":
            return 99999
        return int(maxOccurs)
