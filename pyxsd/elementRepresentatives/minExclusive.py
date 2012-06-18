from elementRepresentative import ElementRepresentative

#============================================================
#
class MinExclusive(ElementRepresentative):
    """
    The class for the MaxInclusive tag. Subclass of *ElementRepresentative*.
    """
    def __init__(self, xsdElement, parent):
        """
        See *ElementRepresentative* for documentation.
        """
        ElementRepresentative.__init__(self, xsdElement, parent)

        self.value                            = self.xsdElement.get('value')
        self.getContainingType().minExclusive = self.value

        
    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- `ContainingTypeName`|minExclusive.
        The name on this class is used for almost nothing.
        """
        return "%s|minExclusive" % self.getContainingTypeName()
