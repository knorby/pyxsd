import logging
import types

from pyxsd.element_representatives.element_representative import ElementRepresentative

logger = logging.getLogger(__name__)


class XsdType(ElementRepresentative):
    """Base class for SimpleType and ComplexType.

    In this class, the Python classes for all of the schema types are
    generated.
    """

    def __init__(self, xsdElement, parent):
        """The ``__init__`` for this class' subclasses.  Creates a blank
        list for enumerations.  Creates a blank dictionary for
        attributes.

        See ElementRepresentative for documentation.
        """
        self.enumerations = []
        self.attributes = {}
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
        """
        baseList = []
        for superClassName in self.superClassNames:
            baseList.append(ElementRepresentative.typeFromName(superClassName, pyXSD))
        if not self.containsSchemaBase(baseList):
            baseList.append(SchemaBase)
        return tuple(baseList)

    def getElements(self):
        """Returns a blank list.

        Subclasses use this function to return elements, but this
        function is called elsewhere on all of the types.
        """
        return []

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

        Calls ``getBaseList()`` to generate the tuple of bases;
        SchemaBase is in every base list, which is what runs the
        ``__init_subclass__`` hook. Adds the name and the doc string to
        the namespace. Adds the instance of PyXSD to all attributes,
        elements, and the namespace, so it can be accessed later on.
        """
        bases = self.getBaseList(pyXSD)
        namespace = {
            "pyXSD": pyXSD,
            "name": self.name,
            "__doc__": self.__doc__,
        }
        for element in self.getElements():
            element.pyXSD = pyXSD
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

        return cls


from pyxsd.schema_base import SchemaBase  # noqa: E402
