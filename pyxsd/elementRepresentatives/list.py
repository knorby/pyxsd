from elementRepresentative import ElementRepresentative
from pyxsd.xsdDataTypes import *

#============================================================
#
class List(ElementRepresentative):
    """
    The class for the list tag. Subclass of *ElementRepresentative*.
    """
    def __init__(self, xsdElement, parent):
        """
        See *ElementRepresentative* for documentation.
        """
        ElementRepresentative.__init__(self, xsdElement, parent)

        self.itemType                         = self.xsdElement.get('itemType')
                
        self.getContainingType().listItemType = self.itemType
        
        self.type = 'xs:list' #the 'xs' is used so that it can be properly identified as a primitive data type later on

    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- `ContainingTypeName`|minExclusive.
        The name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()

        name = contName + '|list'

        return name
