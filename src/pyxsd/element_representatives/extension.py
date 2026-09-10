from pyxsd.element_representatives.element_representative import ElementRepresentative


class Extension(ElementRepresentative):
    """The class for the extension tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        ElementRepresentative.__init__(self, xsdElement, parent)
        self.addSuperClassName(self.tagAttributes["base"])

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|extension.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return contName + "|extension"

    def addBaseToComplexType(self):
        """Used by complexContent. Adds its base to the complexType."""
        baseType = self.getFromName(self.tagAttributes["base"])
        if not baseType:
            print(self.name, self.tagAttributes.get("base"))
