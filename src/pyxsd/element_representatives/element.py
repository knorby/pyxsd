import logging
from typing import Any

from pyxsd.element_representatives.element_representative import ElementRepresentative

logger = logging.getLogger(__name__)


class Element(ElementRepresentative):
    """The class for the element tag.

    The element tag and the attribute tag are the most important in the
    xml and in the program, so this class contains some machinery that
    many of the other classes do not have. The element and attribute
    classes contain descriptor methods. By specifying ``__get__``,
    ``__set__``, and ``__delete__`` (with ``__get__`` and
    ``__set__`` being the most important), these methods specify how a variable is
    set and how it is retrieved. Any modification of these methods
    should be made under extreme caution! If some variable is set to
    some value that does not match the specifications in the schema, an
    error will be raised. These methods add a powerful layer of
    functionality with a small amount of code; however, these functions
    are almost invisible unless they raise an error, so developers
    should bear in mind these methods when modifying the program.

    When a generated class is created, ``__set_name__`` binds this
    descriptor to the class that declares it, and
    ``SchemaBase.__init_subclass__`` records the binding in the class's
    ``_elementNames_`` bookkeeping.
    """

    # Set by ComplexType._resolveElementRef for ``ref`` sites; the
    # owning parser is attached during clsFor.  Annotations only: the
    # attributes are assigned dynamically.
    referredElement: Any
    pyXSD: Any

    def __init__(self, xsdElement, parent):
        """Adds itself to the element list in its parent.

        Reference sites (``<xs:element ref="..."/>``) are detected
        before the base run so name assignment can account for them;
        their content model comes from the referenced global element
        declaration (see
        ``ComplexType._resolveElementRef``).

        See ElementRepresentative for documentation.
        """
        self.isElementRef = xsdElement.get("ref") is not None
        if self.isElementRef:
            self.ref = xsdElement.get("ref")
        # Identity constraints (xs:key/xs:unique/xs:keyref) declared
        # inside this element record themselves here.
        self.identities = []
        super().__init__(xsdElement, parent)
        parent.elements.append(self)

    def getName(self):
        """Returns the element's schema name.

        Reference sites make a unique bookkeeping name like
        ``ContainingTypeName``|elementRef|``ref``; the resolved
        reference later adopts the referred declaration's name for
        matching (see ``_resolveElementRef``).
        """
        if getattr(self, "isElementRef", False):
            contName = self.getContainingTypeName()
            return f"{contName}|elementRef|{self.ref}"
        return self.xsdElement.get("name")

    def processChildren(self):
        """Processes children of a non-reference element.

        Reference elements carry no content model of their own (only
        annotations, which carry no parse-relevant information), so
        their children are not made into ERs.
        """
        if getattr(self, "isElementRef", False):
            return None
        children = list(self.xsdElement)

        if not children:
            return None

        for child in children:
            processedChild = ElementRepresentative.factory(child, self)
            self.processedChildren.append(processedChild)
            childClassName = processedChild.__class__.__name__ if processedChild is not None else ""
            if childClassName in ("SimpleType", "ComplexType"):
                self.type = processedChild.name
                self.tagAttributes["type"] = self.type
            # NOTE: the factory call above already processed the child's
            # children inside ElementRepresentative.__init__; do not
            # call processedChild.processChildren() again here (the old
            # code did, which constructed every grandchild ER twice and
            # double-registered sequences/elements).
        return None

    def getType(self):
        """Returns its type from the class dictionary in PyXSD.

        Reference sites use the referenced declaration's type. The
        instance of PyXSD is attached to every element and attribute
        while the classes for the schema types are being built.
        Clearly, this function is used after the main ER run.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.getType()

        if "type" not in self.__dict__:
            # An element with no type attribute and no inline type
            # declaration defaults to anyType, which accepts any
            # content; SchemaBase instances parse generically.
            logger.debug(
                "element '%s' declares no type; treating it as anyType",
                self.name,
            )
            from pyxsd.schema_base import SchemaBase

            return SchemaBase

        if self.type in self.pyXSD.classes:
            return self.pyXSD.classes[self.type]

        return self.typeFromName(self.type, self.pyXSD)

    def __set_name__(self, owner, name):
        """Called when this descriptor is bound as ``name`` on ``owner``.

        Stores the owning generated class so error messages can name
        it, and warns if the class attribute name does not match the
        schema element name (they are normally identical; a mismatch
        means a descriptor was rebound under a different name).
        """
        self.owner = owner
        if name != self.name:
            logger.warning(
                "element descriptor for %r was bound as %r on %s",
                self.name,
                name,
                owner.__name__,
            )

    def __str__(self):
        """Prints its name in a form that allows for quick identification
        of an element, without needing a bulky name that does not match
        the name used.
        """
        return f"{self.getContainingTypeName()}|{self.__class__.__name__}|{self.name}"

    def __get__(self, obj, objtype=None):
        """Gets an element value from the obj's dictionary.

        Returns its value if it has one; returns the default value if
        it does not. When accessed through the class itself, returns
        the descriptor, per the descriptor protocol.

        See the Python documentation for full documentation on
        descriptors.
        """
        if obj is None:
            return self
        if self.name in obj.__dict__:
            return obj.__dict__[self.name]

        default = getattr(self, "default", None)
        return default

    def __set__(self, obj, value):
        """Sets an element's value in the obj's dictionary.

        The value must be an instance of the element's type (or the
        assignment raises ``TypeError``). If the element may occur more
        than once (``maxOccurs`` greater than one), the value is
        appended to a list; otherwise it is stored directly.

        See the Python documentation for full documentation on
        descriptors.
        """
        if not isinstance(value, self.getType()):
            # Under the ``raw`` invalid-value policy a primitive child
            # whose lexical value failed validation is bound as a plain
            # string so no data is lost; the validation report still
            # records the problem.
            parser = getattr(self, "pyXSD", None)
            policy = getattr(parser, "mode", None)
            if getattr(policy, "invalid_value", "drop") != "raw":
                raise TypeError(
                    f"{value!r} is not an instance of the type of element "
                    f"{self.name!r} ({self.getType().__name__})"
                )

        if self.isList():
            obj.__dict__.setdefault(self.name, []).append(value)
            return None

        obj.__dict__[self.name] = value
        return None

    def __delete__(self, obj):
        """Deletes an entry from the dictionary.

        See the Python documentation for full documentation on
        descriptors.
        """
        del obj.__dict__[self.name]

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

    def getDefault(self):
        """Returns the element's schema ``default`` value, or ``None``.

        Per XSD 1.0 a ``default`` value only applies to simple (or
        mixed) content: when the element is empty in the instance, the
        default supplies its value. Reference sites use the
        referenced declaration's value.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.getDefault()
        return self.tagAttributes.get("default")

    def getFixed(self):
        """Returns the element's schema ``fixed`` value, or ``None``.

        A ``fixed`` element must either be absent or carry exactly
        that value (simple content only, per XSD 1.0). Reference
        sites use the referenced declaration's value.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.getFixed()
        return self.tagAttributes.get("fixed")

    def isNillable(self):
        """Returns True when the element declaration is ``nillable``.

        Reference sites use the referenced declaration's setting.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.isNillable()
        return self.tagAttributes.get("nillable") == "true"

    def isAbstract(self):
        """Returns True when the element declaration is ``abstract``.

        Abstract elements may not appear in instance documents
        directly; only their substitution group members can.
        Reference sites use the referenced declaration's setting.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.isAbstract()
        return self.tagAttributes.get("abstract") == "true"

    def getBlock(self):
        """Returns the element's ``block`` attribute value, or ``None``.

        Reference sites use the referenced declaration's value.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.getBlock()
        return self.tagAttributes.get("block")

    def getSubstitutionGroupHead(self):
        """Returns the local name of the ``substitutionGroup`` head.

        Namespace prefixes are stripped, matching the parser's
        schema-name lookups.
        """
        head = self.tagAttributes.get("substitutionGroup")
        if head is None:
            return None
        return head.split(":", 1)[-1]
