from pyxsd.element_representatives.element_representative import ElementRepresentative


class TotalDigits(ElementRepresentative):
    """The class for the totalDigits tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        self.value = self.xsdElement.get("value")
        self.getContainingType().totalDigits = self.value

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|totalDigits.  The
        name on this class is used for almost nothing.
        """
        return f"{self.getContainingTypeName()}|totalDigits"
