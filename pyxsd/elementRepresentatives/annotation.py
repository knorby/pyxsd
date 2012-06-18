

from elementRepresentative import ElementRepresentative

#============================================================
#
class Annotation(ElementRepresentative):
    """
    The class for the Annotation tag. Subclass of ElementRepresentative.
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
        Makes a name like this- `ContainingTypeName`|annotation. 
        The name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()

        return "%s|%s" % (contName, self.__class__.__name__)




