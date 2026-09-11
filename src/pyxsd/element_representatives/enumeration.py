from pyxsd.element_representatives.element_representative import ElementRepresentative


class Enumeration(ElementRepresentative):
    """The class for the enumeration tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        self.value = self.xsdElement.get("value")
        self.getContainingType().enumerations.append(self.value)

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|Enumeration|``an
        id number``.  The name on this class is used for almost nothing.
        """
        enumNum = len(self.getContainingType().enumerations) + 1
        name = self.getContainingTypeName()
        return f"{name}|enumeration|{enumNum}"
