from pyxsd.element_representatives.element_representative import ElementRepresentative


class ExplicitTimezone(ElementRepresentative):
    """The class for the XSD 1.1 ``explicitTimezone`` facet tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        self.value = self.xsdElement.get("value")
        self.getContainingType().explicitTimezone = self.value

    def getName(self):
        """Makes a name like ``ContainingTypeName|explicitTimezone``.

        The name on this class is used for almost nothing.
        """
        return f"{self.getContainingTypeName()}|explicitTimezone"
