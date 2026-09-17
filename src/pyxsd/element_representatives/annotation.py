from pyxsd.element_representatives.element_representative import ElementRepresentative


class Annotation(ElementRepresentative):
    """The class for the annotation tag."""

    #: An ``annotation`` contains only ``appinfo`` and ``documentation``
    #: children (XSD 1.0/1.1); a nested ``annotation`` is illegal. The
    #: bodies of ``appinfo``/``documentation`` stay permissive (they are
    #: not schema components), so those classes declare no table.
    _ALLOWED_CHILDREN = ("appinfo", "documentation")

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|Annotation.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return f"{contName}|{self.__class__.__name__}"
