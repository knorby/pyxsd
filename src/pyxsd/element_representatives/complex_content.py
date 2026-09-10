from pyxsd.element_representatives.element_representative import ElementRepresentative


class ComplexContent(ElementRepresentative):
    """The class for the complexContent tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|complexContent.
        The name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return contName + "|complexContent"
