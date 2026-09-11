from pyxsd.element_representatives.element_representative import ElementRepresentative


class Union(ElementRepresentative):
    """The class for the ``union`` tag.

    A union simple type validates values against several member types
    in order (from the ``memberTypes`` attribute and inline
    ``simpleType`` children) and accepts the first one that matches.
    The member list is recorded on the containing ``SimpleType`` ER;
    the validating class itself is built later, during ``clsFor``
    (see ``XsdType.makeUnionClass``).
    """

    def __init__(self, xsdElement, parent):
        """Records the union member specification on the containing
        SimpleType: named members from ``memberTypes`` (whitespace
        separated) and inline simpleType children.  Uses the ER
        ``__init__``.  See ElementRepresentative for more
        documentation.
        """
        super().__init__(xsdElement, parent)
        self.memberTypes = (self.tagAttributes.get("memberTypes") or "").split()
        containing = self.getContainingType()
        containing.unionSpec = list(self.memberTypes)
        containing.unionInline = [
            child.name
            for child in self.processedChildren
            if child.__class__.__name__ == "SimpleType"
        ]

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|union.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return contName + "|union"
