from pyxsd.element_representatives.element_representative import ElementRepresentative


class Annotation(ElementRepresentative):
    """The class for the annotation tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        ElementRepresentative.__init__(self, xsdElement, parent)

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|Annotation.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return f"{contName}|{self.__class__.__name__}"
