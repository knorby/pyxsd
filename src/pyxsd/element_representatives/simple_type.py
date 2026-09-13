from pyxsd.element_representatives.xsd_type import XsdType


class SimpleType(XsdType):
    """The class for the simpleType tag."""

    def __init__(self, xsdElement, parent):
        """Creates blank variables for a series of facets, which are
        variables that specify restrictions on values.  Adds itself to
        the simpleType dictionary in the *schema* class instance.  Uses
        the XsdType ``__init__``.

        See ElementRepresentative for documentation.
        """
        self.patterns = []
        self.length = None
        self.minLength = None
        self.maxLength = None
        self.totalDigits = None
        self.fractionDigits = None
        self.whiteSpace = None
        self.listItemType = None
        self.minInclusive = None
        self.maxInclusive = None
        self.minExclusive = None
        self.maxExclusive = None
        super().__init__(xsdElement, parent)
        self.getSchema().simpleTypes[self.name] = self
