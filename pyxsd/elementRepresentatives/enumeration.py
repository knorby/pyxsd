from elementRepresentative import ElementRepresentative

#============================================================
#
class Enumeration(ElementRepresentative):
    """
    The class for the Enumeration tag. Subclass of *ElementRepresentative*.
    """
    def __init__(self, xsdElement, parent):
        """
        See *ElementRepresentative* for documentation.
        """
        ElementRepresentative.__init__(self, xsdElement, parent)

        self.value = self.xsdElement.get('value')

        self.getContainingType().enumerations.append(self.value)
        
    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- `ContainingTypeName`|enuneration|`an id number`. 
        The name on this class is used for almost nothing.
        """
        enumNum = len(self.getContainingType().enumerations) + 1
        return "%s|enumeration|%i" % (self.getContainingTypeName(),
                                     enumNum)
