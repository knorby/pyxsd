import logging
from typing import Any

from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.xsd_data_types import AnySimpleType, Boolean, XsdDataType

logger = logging.getLogger(__name__)


class Attribute(ElementRepresentative):
    """The class for the attribute tag.

    The element tag and the attribute tag are the most important in the
    xml and in the program, so this class contains some machinery that
    many of the other classes do not have. The element and attribute
    classes contain descriptor methods. By specifying ``__get__``,
    ``__set__``, and ``__delete__`` (with ``__get__`` and ``__set__``
    being the most important), these methods specify how a variable is
    set and how it is retrieved. Any modification of these methods
    should be made under extreme caution! Both Element and Attribute,
    primarily Attribute, use these descriptors to add a level of
    checking to the program. If some variable is set to some value that
    does not match the specifications in the schema, an error will be
    raised. These methods add a powerful layer of functionality with a
    small amount of code; however, these functions are almost invisible
    unless they raise an error, so developers should bear in mind these
    methods when modifying the program.
    """

    # The owning parser is attached during clsFor.  Annotation only:
    # the attribute is assigned dynamically.
    pyXSD: Any

    def __init__(self, xsdElement, parent):
        """Adds itself to the attribute dictionary in its containing
        type. See ElementRepresentative for documentation.

        Reference sites (``<xs:attribute ref="..."/>``) carry no
        ``name`` of their own; a bookkeeping name is assigned in
        ``getName`` and the site is resolved to the referenced global
        declaration when the generated class is built (see
        ``XsdType.resolveAttributeRefs``).
        """
        self.isAttributeRef = xsdElement.get("ref") is not None
        if self.isAttributeRef:
            self.ref = xsdElement.get("ref")
        super().__init__(xsdElement, parent)
        self.getContainingType().attributes[self.name] = self

    def getName(self):
        """Returns the attribute's schema name.

        Reference sites make a unique bookkeeping name like
        ``ContainingTypeName``|attributeRef|``ref``; the resolved
        reference later adopts the referred declaration's name so
        instance matching and Python access work.
        """
        if getattr(self, "isAttributeRef", False):
            contName = self.getContainingTypeName()
            return f"{contName}|attributeRef|{self.ref}"
        return self.xsdElement.get("name")

    def __set_name__(self, owner, name):
        """Called when this descriptor is bound as ``name`` on ``owner``.

        Stores the owning generated class so error messages can name
        it, and warns if the class attribute name does not match the
        schema attribute name (they are normally identical; a mismatch
        means a descriptor was rebound under a different name).
        """
        self.owner = owner
        if name != self.name:
            logger.warning(
                "attribute descriptor for %r was bound as %r on %s",
                self.name,
                name,
                owner.__name__,
            )

    def __str__(self):
        """Prints its name in a form that allows for quick identification
        of an attribute, without needing a bulky name that does not
        match the name used.
        """
        return f"{self.getContainingTypeName()}|{self.__class__.__name__}|{self.getName()}"

    def processChildren(self):
        """There is a special ``processChildren()`` here to handle special
        types, which can be declared as a child of an attribute. If an
        attribute child can exist that is not a type, then this
        function will screw it up; however, as far as the developers
        knew at the time of writing this program, they cannot.
        """
        children = list(self.xsdElement)

        if not children:
            return None

        for child in children:
            processedChild = ElementRepresentative.factory(child, self)
            self.processedChildren.append(processedChild)
            # An ``xsd:annotation`` is documentation, not the attribute
            # declaration. Schemas commonly attach one to an attribute
            # that already carries a ``type`` attribute (GPX does this
            # for every attribute), and taking its bookkeeping name as
            # the declared type overwrites the real one.
            if child.tag.split("}")[-1] == "annotation":
                continue
            # An explicit ``type`` attribute wins over any child; a child
            # is the inline ``xsd:simpleType`` used when there is none.
            if "type" in self.tagAttributes:
                continue
            self.type = processedChild.name
            self.tagAttributes["type"] = self.type
            # NOTE: the factory call above already processed the child's
            # children inside ElementRepresentative.__init__; the old
            # code's extra processedChild.processChildren() call here
            # constructed every grandchild ER twice.
        return None

    def getType(self):
        """Returns its type from the class dictionary in PyXSD.

        Reference sites use the referenced global declaration's type.
        The instance of PyXSD is attached to every element and attribute
        while the classes for the schema types are being built.
        Clearly, this function is used after the main ER run.
        """
        if getattr(self, "isAttributeRef", False):
            referred = getattr(self, "referredAttribute", None)
            if referred is None:
                raise TypeError(f"attribute reference {self.ref!r} was not resolved")
            return referred.getType()

        if "type" not in self.__dict__:
            # XSD: an attribute declaration with no ``type`` and no
            # inline ``simpleType`` child takes anySimpleType, which
            # accepts any value.
            return AnySimpleType

        # Resolve the QName first so strict mode disambiguates types
        # that share a local name across namespaces; in legacy mode this
        # is the same raw-type lookup as before.
        resolved = self.resolvedTypeName()
        if resolved is not None and resolved in self.pyXSD.classes:
            return self.pyXSD.classes[resolved]

        return self.typeFromName(resolved, self.pyXSD)

    def __get__(self, obj, objtype=None):
        """Gets an attribute value from the obj's dictionary.

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
        """Sets values to attributes.

        Converts text Boolean values to binary values (integers 0 and
        1), and validates the value against the attribute's type.

        See the Python documentation for full documentation on
        descriptors.
        """
        if issubclass(self.getType(), XsdDataType):
            if self.getType() is Boolean and isinstance(value, str):
                if value in ("true", "True"):
                    value = 1
                elif value in ("False", "false"):
                    value = 0
            try:
                value = self.getType()(value)
            except Exception as e:
                message = f"attribute '{self.name}' has an invalid value: {e}"
                parser = getattr(self, "pyXSD", None)
                if parser is not None:
                    parser.report.add_error(
                        message,
                        code="invalid-attribute",
                        element=getattr(obj, "_name_", None),
                    )
                else:
                    logger.error(message)
        elif not isinstance(value, self.getType()):
            # The declared type did not resolve to a validating datatype
            # (a malformed or unresolved declaration). Record a
            # structured error; assignment must never raise out of
            # instance binding.
            message = (
                f"attribute '{self.name}' has a value that cannot be "
                f"validated against its declared type"
            )
            parser = getattr(self, "pyXSD", None)
            if parser is not None:
                parser.report.add_error(
                    message,
                    code="invalid-attribute",
                    element=getattr(obj, "_name_", None),
                )
            else:
                logger.error(message)

        obj.__dict__[self.name] = value

    def __delete__(self, obj):
        """Deletes an entry from the dictionary.

        See the Python documentation for full documentation on
        descriptors.
        """
        del obj.__dict__[self.name]

    def getUse(self):
        """Returns the 'use' value, which says if the attribute is
        required or optional (the default).
        """
        if "use" not in self.__dict__:
            self.use = "optional"
        return self.use

    def getDefault(self):
        """Returns the attribute's schema ``default`` value, or ``None``.

        When the attribute is absent from an instance document, the
        default supplies its value. A reference site may override the
        referenced declaration's default.
        """
        if self.tagAttributes.get("default") is not None:
            return self.tagAttributes["default"]
        if getattr(self, "isAttributeRef", False):
            referred = getattr(self, "referredAttribute", None)
            if referred is not None:
                return referred.getDefault()
        return None

    def getFixed(self):
        """Returns the attribute's schema ``fixed`` value, or ``None``.

        A ``fixed`` attribute must either be absent (in which case it
        takes the fixed value) or carry exactly that value. A reference
        site may override the referenced declaration's fixed value.
        """
        if self.tagAttributes.get("fixed") is not None:
            return self.tagAttributes["fixed"]
        if getattr(self, "isAttributeRef", False):
            referred = getattr(self, "referredAttribute", None)
            if referred is not None:
                return referred.getFixed()
        return None
