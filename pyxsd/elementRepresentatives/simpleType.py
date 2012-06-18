
from xsdType import XsdType

#============================================================
#
class SimpleType(XsdType):
    """
    The class for the simpleType tag. Subclass of *XsdType*.
    """
    #============================================================
    #
    def __init__(self, xsdElement, parent):

        """
        Creates blank variables for a series of facets, which are variables that specify restrictions on attributes.
        Adds itself to the simpleType dictionary in the *schema* class instance.
        Uses the *XsdType* `__init__`.
        See *ElementRepresentative* for documentation.        
        """
        
        self.patterns     = []

        self.length       = None

        self.listItemType = None

        self.minInclusive = None

        self.maxInclusive = None

        self.minExclusive = None

        self.maxExclusive = None

        XsdType.__init__(self, xsdElement, parent)

        self.getSchema().simpleTypes[self.name] = self

