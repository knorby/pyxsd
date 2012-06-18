from elementRepresentative import ElementRepresentative

#============================================================
#
class AttributeGroup(ElementRepresentative):
    """
    The class for the Extension tag. Subclass of *ElementRepresentative*.
    At the current time, attributeGroups may not be fully used in the
    program. If your schema uses AttributeGroup, you might check to make
    sure that they are being used correctly.
    """

    #============================================================
    #
    def __init__(self, xsdElement, parent):
        """
        Creates a dictionary for attributes.
        Adds itself to the attribute group dictionary in schema.        
        See *ElementRepresentative* for more documentation.
        """
        self.attributes = {}

        ElementRepresentative.__init__(self, xsdElement, parent)

        attrGroupContainer = self.parent.getContainingType()
        attrGroupContainer.attributeGroups[self.name] = self
        self.getSchema().attributeGroups[self.name] = self
    #============================================================
    #
    def getContainingType(self):
        """
        Returns self, because Attribute groups are containg types.

        No parameters
        """
        return self
    
