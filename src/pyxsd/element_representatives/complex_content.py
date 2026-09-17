from pyxsd.element_representatives.element_representative import ElementRepresentative


class ComplexContent(ElementRepresentative):
    """The class for the complexContent tag."""

    #: ``complexContent`` wraps exactly one derivation.
    _ALLOWED_CHILDREN = ("annotation", "restriction", "extension")
    _MAX_ONE_CHILDREN = ("annotation", "restriction", "extension")
    _CHILD_ORDER = (("annotation",), ("restriction", "extension"))
    _ONE_OF_SLOTS = frozenset({1})

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|complexContent.
        The name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return contName + "|complexContent"
