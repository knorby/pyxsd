

from elementRepresentative import ElementRepresentative

#============================================================
#
class Extension(ElementRepresentative):
    """
    The class for the Extension tag. Subclass of *ElementRepresentative*.
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

        name = contName + '|extension'

        return name

    #============================================================
    #
    def addBaseToComplexType(self):
        """
        Used by complexContent. Adds its base to the complexType.
        """
        baseType = self.getFromName(self.tagAttributes['base'])
        if not baseType:
            print self.name, self.base

