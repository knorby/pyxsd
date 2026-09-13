from pyxsd.element_representatives.element_representative import ElementRepresentative


class MinLength(ElementRepresentative):
    """The class for the minLength tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        self.value = self.xsdElement.get("value")
        self.getContainingType().minLength = self.value

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|minLength.  The
        name on this class is used for almost nothing.
        """
        return f"{self.getContainingTypeName()}|minLength"
