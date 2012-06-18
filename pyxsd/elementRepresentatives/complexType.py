
from xsdType import XsdType
#============================================================
#

class ComplexType(XsdType):
    """
    The class for the complexType tag. Subclass of *XsdType*.
    """
    #============================================================
    #
    def __init__(self, xsdElement, parent):
        """
        Keeps a list of sequences and choices that are children of it.
        Stores itself in the schema dictionary of complexTypes.
        Uses the XsdType `__init__`.
         See *ElementRepresentative* for more documentation.
        """
        self.sequencesOrChoices = []
        
        XsdType.__init__(self, xsdElement, parent)
        
        self.getSchema().complexTypes[self.name] = self

    #============================================================
    #
    def getElements(self):
        """
        Returns a list of elements. Uses lazy evaluation. Goes through the `sequencesOrChoices` list,
        goes through each ones elements, adds its container information, then adds the element to a list
        which it returns.

        No parameters.
        """
        elements = getattr(self, 'elements_', None)
        
        if not elements == None:
            return elements
        
        self.elements_ = []
        itemInfo = None
        for item in self.sequencesOrChoices:
            if item.tagType == "sequence":
                itemInfo = "sequence"
            if item.tagType == "choice":
                itemInfo = "choice"
            for element in item.elements:
                element.sOrC = itemInfo
                self.elements_.append(element)
        return self.elements_

    #============================================================
    #
    def gatherFacets(self):
        """
        returns a blank dictionary. Needed for SimpleType, so the function can be called for any type without error.
        """
        return {}
