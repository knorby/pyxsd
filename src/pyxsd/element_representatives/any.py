from pyxsd.element_representatives.element_representative import ElementRepresentative


class Any(ElementRepresentative):
    """The class for the ``any`` tag (element wildcard).

    Marks the containing type as having a wildcard content-model
    slot: instance children that the schema does not declare are
    accepted and parsed generically (raw attribute values, untyped
    text, recursed children) instead of being ignored. The pass-through
    is deliberately permissive; namespace and ``processContents``
    fine-tuning is not enforced.
    """

    def __init__(self, xsdElement, parent):
        """Flags the containing type with a wildcard element slot.
        Uses the ER ``__init__``.  See ElementRepresentative for more
        documentation.
        """
        super().__init__(xsdElement, parent)
        self.getContainingType().hasWildcardElements = True

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|any.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return f"{contName}|any"
