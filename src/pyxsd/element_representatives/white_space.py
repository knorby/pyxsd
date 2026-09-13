from pyxsd.element_representatives.element_representative import ElementRepresentative


class WhiteSpace(ElementRepresentative):
    """The class for the whiteSpace tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        self.value = self.xsdElement.get("value")
        self.getContainingType().whiteSpace = self.value

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|whiteSpace.  The
        name on this class is used for almost nothing.
        """
        return f"{self.getContainingTypeName()}|whiteSpace"
