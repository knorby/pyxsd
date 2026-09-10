"""The ElementRepresentative system.

The ElementRepresentative (ER) system converts the schema file into a
collection of classes that represent the types in the schema.  The
system takes in the ElementTree representation of the schema file.  The
PyXSD class calls ElementTree to parse the schema file in order to keep
all calls to ElementTree in one place.  The classmethod ``factory`` in
the ElementRepresentative class starts the system up, and it is the
function PyXSD calls.  ``factory`` reads an ElementTree element and
finds the class that has the same name as the element's tag type, then
makes an instance of that class.

In these tag type classes, the ``__init__`` function is the first to be
called.  Each of these classes is a subclass of ElementRepresentative,
and their ``__init__`` calls the ``__init__`` in the
ElementRepresentative class, which collects general information about
the name and makes calls to specific functions to help with this
operation.  Some of these functions are located in ElementRepresentative
exclusively, but many can also be found in the specific classes.  For
example, ``getName()`` normally calls a version of the function in the
ER class, but on some tags the ER version of ``getName()`` would not
generate a unique name in every case, or it would lack all the needed
information.  In the classes for such tags there is another version of
``getName()`` that overrides the ElementRepresentative version.  In
general, the most common methods are found in ElementRepresentative,
while methods that are very specific to a tag are found in the tag's
class.  All the methods needed to parse the tree are found in the ER
class.  Overriding classes must all have the same arguments in order
for the system to work.  The system was designed so that it is each
ElementRepresentative's job to:

- Collect all information from the element and put it in the
  appropriate place

- Construct ElementRepresentatives for all of the element's children

The ``__init__`` function
-------------------------

In the ER ``__init__`` function, a call is made to the
``processChildren`` method, which calls the ``factory`` on all of the
children of an element.  Since the ER ``__init__`` calls this method,
any variable assignment that is needed by the children must be made
before the call to the ER ``__init__`` function.  The children are
completely processed before the parent is fully finished.  Any
developer should be mindful of this fact when creating or changing an
``__init__`` method in a tag type class.

Each ``__init__`` function has two variables as arguments:

- ``xsdElement`` - the ElementTree element that is being converted to
  an ER

- ``parent`` - the parent ER for the tag being processed

Remember: the tag type classes are called from ``factory`` so each
``__init__`` must follow this same pattern in order to work.

Class construction
------------------

The classes are constructed in the XsdType class (the base class for
SimpleType and ComplexType) via ``clsFor()``, which builds new classes
with ``types.new_class()`` by supplying a namespace, tuple of bases,
and a name. As each class is created, the standard Python protocols do
the wiring: ``__set_name__`` binds every element and attribute
descriptor to the new class, and ``SchemaBase.__init_subclass__``
collects the descriptor bookkeeping (``_elementNames_`` and
``_attributeNames_``) automatically. The classes are stored in a
dictionary in the PyXSD instance.
"""

import logging

from pyxsd.xsd_data_types import (
    ID,
    IDREF,
    Base64Binary,
    Boolean,
    Double,
    Integer,
    PositiveInteger,
    String,
)

logger = logging.getLogger(__name__)


class ElementRepresentative:
    """Base class for all of the tag type classes in the
    ElementRepresentative system.

    It contains the methods that control movement around the tree and
    the most general ways to gather information from the schema.
    """

    def __init__(self, xsdElement, parent):
        """See the documentation for the ElementRepresentative system at
        the top of this module.
        """
        self.xsdElement = xsdElement
        self.parent = parent
        self.tagParts = self.xsdElement.tag.split("}")
        self.tagType = self.tagParts[1]
        self.name = self.getName()
        self.register(self.name, self)
        self.superClassNames = []
        self.subClassNames = []
        self.references = []
        self.referToMe = []
        self.tagAttributes = {}
        self.processedChildren = []
        self.layerNum = self.findLayerNum()
        self.clsName = self.name
        self.clsName = self.clsName[0].upper() + self.clsName[1:]

        for name, value in xsdElement.items():
            if name == "name":
                continue
            setattr(self, name, value)
            self.tagAttributes[name] = value

        self.processChildren()

    def __str__(self):
        """Prints the ER information for a tag in the form:
        ``ClassName[TagName]``.
        """
        return "{}[{}]".format(
            self.__class__.__name__,
            self.__dict__.get("name", "???"),
        )

    def processChildren(self):
        """Calls the ``factory`` on all of the children of an element."""
        children = list(self.xsdElement)
        if not children:
            return None
        for child in children:
            processedChild = ElementRepresentative.factory(child, self)
            self.processedChildren.append(processedChild)
        return None

    @classmethod
    def factory(cls, xsdElement, parent):
        """Initializes the tag-specific class for a particular ElementTree
        schema element.  See the ER system documentation.

        usage: ``ElementRepresentative.factory(xsdElement, parent)``
        """
        clsName = cls.classNameFor(xsdElement, parent)
        if clsName in TAG_CLASSES:
            tagCls = TAG_CLASSES[clsName]
            return tagCls(xsdElement, parent)
        # Complain
        logger.warning("ElementRepresentative %s is not defined!", clsName)
        return None

    def describe(self):
        """Returns a multi-line description of this ER's attributes for
        debugging.
        """
        return "\n".join(f" {attrName} -> {value} " for attrName, value in vars(self).items())

    def findLayerNum(self):
        """Returns an integer that specifies how deep in the tree a
        particular element is.  The ``schema`` element, which is the
        root element, is 1.
        """
        if self.parent is None:
            return 1
        return self.parent.findLayerNum() + 1

    def checkTopLevelType(self):
        """Checks to see if an element is at the top level. Returns True
        if it is, False if it is not. The top level elements are all
        the children of the root ``schema`` tag.
        """
        return bool(isinstance(self.parent, Schema))

    @classmethod
    def typeFromName(cls, xsdTypeName, pyXSD):
        """Returns a schema type given the type's name.

        Returns data type classes from ``xsd_data_types`` for primitive
        data types.  Calls ``clsFor`` on ERs.
        """
        if not xsdTypeName.startswith("xs:"):
            getFromNameReturned = cls.getFromName(xsdTypeName)
            if getFromNameReturned:
                return getFromNameReturned.clsFor(pyXSD)
            logger.warning(
                "typeFromName() error: getFromName() is returning None for %s", xsdTypeName
            )
            return None
        primitive = _PRIMITIVE_TYPES.get(xsdTypeName.split(":", 1)[1])
        if primitive is not None:
            return primitive
        logger.warning(
            "XsdTypeName Error: %s does not correspond to a class",
            xsdTypeName.split(":", 1)[1],
        )
        return None

    def addSuperClassName(self, name):
        """Adds a base class name to the containing type for a particular
        element.  Calls come from Restriction and Extension.
        """
        if name is None:
            return None
        # Prevent Duplicates
        for superClassName in self.getContainingType().superClassNames:
            if superClassName == name:
                return None
        self.getContainingType().superClassNames.append(name)
        return None

    def getContainingType(self):
        """Returns the parent's ``getContainingType()``.

        If this function is being called from schema, an error is
        returned.  When an ER *is* a containing type, its own
        ``getContainingType()`` should return that ER.
        """
        if self.parent is not None:
            return self.parent.getContainingType()
        logger.error(
            "ElementRepresentative Error: the program encountered an unknown "
            "error in getContainingType()"
        )
        logger.error("The class dictionary is as follows:\n%s", self.describe())
        return None

    def getSchema(self):
        """Returns the parent's ``getSchema()``.

        getSchema() returns the containing instance when it is a schema
        tag (see the ``getSchema()`` in the ``schema`` class).
        """
        return self.parent.getSchema()

    def getContainingTypeName(self):
        """Returns the name of the containing type."""
        theType = self.getContainingType()
        if theType is None:
            return None
        return theType.name

    def getName(self):
        """Returns the name field in the ElementTree element."""
        return self.xsdElement.get("name")

    @classmethod
    def register(cls, name, obj):
        """Stores ER objects in a registry keyed by name.

        This is why all names must be unique; it helps find objects.
        Only the first ER registered under a given name is kept
        (duplicates are dropped).
        """
        if name not in registry:
            registry[name] = [obj]
        else:
            logger.debug(
                "an element representative named %r is already registered; keeping the first one",
                name,
            )

    @classmethod
    def getFromName(cls, name):
        """Retrieve an entry in the registry by its name."""
        entries = registry.get(name)
        if not entries:
            logger.warning("getFromName Error: %s is not a key in the registry", name)
            return None
        if len(entries) == 1:
            return entries[0]
        # Complain
        logger.warning("ElementRepresentative Error: %r", entries)
        return None

    @staticmethod
    def tryConvert(variable):
        """Tries to convert a variable from a string in the xsd to a
        Python value.  Returns the entry unchanged if it cannot be
        converted.
        """
        try:
            return int(variable)
        except (TypeError, ValueError):
            pass
        try:
            return float(variable)
        except (TypeError, ValueError):
            pass
        if variable == "false":
            return False
        if variable == "true":
            return True
        return variable

    @classmethod
    def classNameFor(cls, xsdElement, parent):
        """Returns the name of the class that the factory should find.

        usage: ``ElementRepresentative.classNameFor(xsdElement, parent)``
        """
        clsName = xsdElement.tag
        tagParts = xsdElement.tag.split("}")
        if len(tagParts) == 2:
            clsName = tagParts[1]
            clsName = clsName[0].upper() + clsName[1:]
        return clsName


# Primitive type names (after the 'xs:' prefix) to classes.
_PRIMITIVE_TYPES = {
    "string": String,
    "double": Double,
    "int": Integer,
    "integer": Integer,
    "boolean": Boolean,
    "positiveInteger": PositiveInteger,
    "ID": ID,
    "IDREF": IDREF,
    "base64Binary": Base64Binary,
}

# Registry of all ER objects, keyed by name.
registry = {}

# Import all of the tag-specific classes after the ER class definition
# (the tag modules import this module's ElementRepresentative).  This
# replaces the old exec-based import loop.
from pyxsd.element_representatives.annotation import Annotation  # noqa: E402
from pyxsd.element_representatives.attribute import Attribute  # noqa: E402
from pyxsd.element_representatives.attribute_group import AttributeGroup  # noqa: E402
from pyxsd.element_representatives.choice import Choice  # noqa: E402
from pyxsd.element_representatives.complex_content import ComplexContent  # noqa: E402
from pyxsd.element_representatives.complex_type import ComplexType  # noqa: E402
from pyxsd.element_representatives.documentation import Documentation  # noqa: E402
from pyxsd.element_representatives.element import Element  # noqa: E402
from pyxsd.element_representatives.enumeration import Enumeration  # noqa: E402
from pyxsd.element_representatives.extension import Extension  # noqa: E402
from pyxsd.element_representatives.length import Length  # noqa: E402
from pyxsd.element_representatives.list import List  # noqa: E402
from pyxsd.element_representatives.max_exclusive import MaxExclusive  # noqa: E402
from pyxsd.element_representatives.max_inclusive import MaxInclusive  # noqa: E402
from pyxsd.element_representatives.min_exclusive import MinExclusive  # noqa: E402
from pyxsd.element_representatives.min_inclusive import MinInclusive  # noqa: E402
from pyxsd.element_representatives.pattern import Pattern  # noqa: E402
from pyxsd.element_representatives.restriction import Restriction  # noqa: E402
from pyxsd.element_representatives.schema import Schema  # noqa: E402
from pyxsd.element_representatives.sequence import Sequence  # noqa: E402
from pyxsd.element_representatives.simple_content import SimpleContent  # noqa: E402
from pyxsd.element_representatives.simple_type import SimpleType  # noqa: E402
from pyxsd.element_representatives.xsd_type import XsdType  # noqa: E402

TAG_CLASSES = {
    "Element": Element,
    "Attribute": Attribute,
    "Schema": Schema,
    "XsdType": XsdType,
    "Extension": Extension,
    "SimpleType": SimpleType,
    "ComplexType": ComplexType,
    "Annotation": Annotation,
    "AttributeGroup": AttributeGroup,
    "Documentation": Documentation,
    "Restriction": Restriction,
    "Sequence": Sequence,
    "Choice": Choice,
    "List": List,
    "SimpleContent": SimpleContent,
    "ComplexContent": ComplexContent,
    "Enumeration": Enumeration,
    "Pattern": Pattern,
    "Length": Length,
    "MinInclusive": MinInclusive,
    "MaxInclusive": MaxInclusive,
    "MinExclusive": MinExclusive,
    "MaxExclusive": MaxExclusive,
}
