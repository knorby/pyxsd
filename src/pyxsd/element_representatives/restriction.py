from pyxsd.element_representatives.element_representative import ElementRepresentative


class Restriction(ElementRepresentative):
    """The class for the restriction tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        self.addSuperClassName(self.tagAttributes["base"])

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|restriction.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return contName + "|restriction"
