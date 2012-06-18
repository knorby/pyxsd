
from elementRepresentative import ElementRepresentative

#============================================================
#
class Length(ElementRepresentative):
    """
    The class for the Length tag. Subclass of ElementRepresentative.
    """
    def __init__(self, xsdElement, parent):
        """
        See *ElementRepresentative* for documentation.
        """
        ElementRepresentative.__init__(self, xsdElement, parent)

        self.value                      = self.xsdElement.get('value')

        self.getContainingType().length = self.value

        
    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- `ContainingTypeName`|length. 
        The name on this class is used for almost nothing.
        """
        return "%s|length" % self.getContainingTypeName()
