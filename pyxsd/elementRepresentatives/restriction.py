
from elementRepresentative import ElementRepresentative

#============================================================
#
class Restriction(ElementRepresentative):
    """
    The class for the Restiction tag. Subclass of *ElementRepresentative*.
    """
    def __init__(self, xsdElement, parent):
        """
        See *ElementRepresentative* for documentation.
        """
        ElementRepresentative.__init__(self, xsdElement, parent)
        
        self.addSuperClassName(self.tagAttributes['base'])
        
    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- `ContainingTypeName`|restiction. 
        The name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()

        name = contName + '|restriction'

        return name

  
