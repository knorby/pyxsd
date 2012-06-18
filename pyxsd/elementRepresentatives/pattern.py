from elementRepresentative import ElementRepresentative

#============================================================
#
class Pattern(ElementRepresentative):
    """
    The class for the pattern tag. Subclass of *ElementRepresentative*.
    """
    def __init__(self, xsdElement, parent):
        """
        See *ElementRepresentative* for documentation.
        """
        ElementRepresentative.__init__(self, xsdElement, parent)

        self.value = self.xsdElement.get('value')

        self.getContainingType().patterns.append(self.value)

        
    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- `ContainingTypeName`|pattern|`an id number`. 
        The name on this class is used for almost nothing.
        """
        patNum = len(self.getContainingType().patterns) + 1
        return "%s|pattern|%i" % (self.getContainingTypeName(),
                                     patNum)
