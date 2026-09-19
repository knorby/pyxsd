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

    def checkDeclarationLegality(self):
        """Reports an attribute that is not the annotation's own ``id``.

        The schema-for-schemas gives ``annotation`` a single ``id``
        attribute; arbitrary content belongs in ``appinfo`` (annotF009:
        ``foo="bar"`` is illegal). A namespaced attribute is left to the
        parser's schema-attribute check.
        """
        for attr in self.xsdElement.attrib:
            if attr.startswith("{"):
                continue
            if attr != "id":
                self._reportSchemaError(
                    f"attribute '{attr}' is not allowed on <annotation>",
                    code="unexpected-attribute",
                )
