from elementRepresentative import ElementRepresentative

#============================================================
#
class MinInclusive(ElementRepresentative):
    """
    The class for the MinInclusive tag. Subclass of *ElementRepresentative*.
    """
    def __init__(self, xsdElement, parent):
        """
        See *ElementRepresentative* for documentation.
        """
        ElementRepresentative.__init__(self, xsdElement, parent)

        self.value                            = self.xsdElement.get('value')
        
        self.getContainingType().minInclusive = self.value

        
    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- `ContainingTypeName`|minInclusive.
        The name on this class is used for almost nothing.
        """

        return "%s|minInclusive" % self.getContainingTypeName()
