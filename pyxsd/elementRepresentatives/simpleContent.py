from elementRepresentative import ElementRepresentative

#============================================================
#
class SimpleContent(ElementRepresentative):
    """
    The class for the simpleContent tag. Subclass of *ElementRepresentative*.
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
        Makes a name like this- `ContainingTypeName`|simpleContent.
        The name on this class is used for almost nothing.
        """        
        contName = self.getContainingTypeName()

        name = contName + '|simpleContent'

        return name
