from pyxsd.element_representatives.element_representative import ElementRepresentative


class AttributeGroup(ElementRepresentative):
    """The class for the attributeGroup tag.

    At the current time, attributeGroups may not be fully used in the
    program. If your schema uses attributeGroup, you might check to
    make sure that they are being used correctly.
    """

    def __init__(self, xsdElement, parent):
        """Creates a dictionary for attributes.  Adds itself to the
        attribute group dictionary in schema.  See ElementRepresentative
        for more documentation.
        """
        self.attributes = {}
        ElementRepresentative.__init__(self, xsdElement, parent)
        attrGroupContainer = self.parent.getContainingType()
        attrGroupContainer.attributeGroups[self.name] = self
        self.getSchema().attributeGroups[self.name] = self

    def getContainingType(self):
        """Returns self, because attribute groups are containing types."""
        return self
