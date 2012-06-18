from complexType import ComplexType

#============================================================
#
class Schema(ComplexType):
    """
    The class for the schema tag. Subclass of *ComplexType*, becuase it is so similar to it.
    """
    #============================================================
    #
    def __init__(self, xsdElement, parent):
        """
        Stores all the attributeGroups, complexTypes,  and simpleTypes in the document in dictionaries. Also has a list of top-level elements (should be only one). 
        See *ElementRepresentative* for more documentation.
        """
        self.attributeGroups = {}
        self.complexTypes    = {}
        self.simpleTypes     = {}
        self.elements        = []

        ComplexType.__init__(self, xsdElement, parent)

    #============================================================
    #
    def getName(self):
        """
        returns 'schema'
        """
        return 'schema'

    #============================================================
    #
    def getElements(self):
        """
        Returns a list of elements.

        No parameters
        """
        oldElements = getattr(self, 'elements_', None)
        
        if not oldElements == None:
            return oldElements
        
        self.elements_ = []

        
        for element in self.elements:
            self.elements_.append(element)
        return self.elements_

    #============================================================
    #
    def getSchema(self):
        """
        returns the schema ER obj. In ER, a method with the same name points down to the method by the same name in its parent.
        """
        return self
