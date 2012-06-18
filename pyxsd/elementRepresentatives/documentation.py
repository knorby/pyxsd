

from elementRepresentative import ElementRepresentative

#============================================================
#
class Documentation(ElementRepresentative):
    """
    The class for the Documentation tag. Subclass of *ElementRepresentative*.
    """
    def __init__(self, xsdElement, parent):
        """
        See *ElementRepresentative* for documentation. Adds its documentation to the __doc__ field for the ER.
        """
        ElementRepresentative.__init__(self, xsdElement, parent)

        self.contType         = self.getContainingType()

        self.contType.__doc__ = xsdElement.text.strip()

    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- `ContainingTypeName`|documentation. 
        The name on this class is used for almost nothing.
        """
        return "%s|%s" % (self.getContainingTypeName(),
                          self.__class__.__name__)




