from pyxsd.element_representatives.element_representative import ElementRepresentative


class Length(ElementRepresentative):
    """The class for the length tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        self.value = self.xsdElement.get("value")
        self.getContainingType().length = self.value

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|length.  The name
        on this class is used for almost nothing.
        """
        return f"{self.getContainingTypeName()}|length"
