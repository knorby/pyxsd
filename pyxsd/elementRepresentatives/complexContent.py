from elementRepresentative import ElementRepresentative

#============================================================
#
class ComplexContent(ElementRepresentative):
    """
    The class for the complexContent tag. Subclass of *ElementRepresentative*.
    """
    def __init__(self, xsdElement, parent):
        """
        See *ElementRepresentative* for documentation.
        """
        
        ElementRepresentative.__init__(self, xsdElement, parent)


    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- `ContainingTypeName`|complexContent. 
        The name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()

        name = contName + '|complexContent'

        return name


    
