from pyxsd.element_representatives.element_representative import ElementRepresentative


class Restriction(ElementRepresentative):
    """The class for the restriction tag."""

    # Set when the restriction declares neither a ``base`` attribute nor
    # an inline ``simpleType``; the containing type reports it while
    # building its class (see ``XsdType.clsFor``).
    hasNoBase: bool = False

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        base = self.tagAttributes.get("base")
        if base is None:
            # A restriction without ``base`` may derive from an inline
            # simple type declared as its child; that child registers
            # itself and can be referenced by its generated name.
            for child in self.processedChildren:
                if child is not None and child.__class__.__name__ == "SimpleType":
                    base = child.name
                    break
        if base is None:
            self.hasNoBase = True
            return
        self.addSuperClassName(base)

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|restriction.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return contName + "|restriction"
