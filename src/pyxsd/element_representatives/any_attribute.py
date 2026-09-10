from pyxsd.element_representatives.element_representative import ElementRepresentative


class AnyAttribute(ElementRepresentative):
    """The class for the ``anyAttribute`` tag (attribute wildcard).

    Marks the containing type as accepting attributes that the schema
    does not declare: they are stored raw on the instance instead of
    being reported as unexpected. The pass-through is deliberately
    permissive; namespace fine-tuning is not enforced.
    """

    def __init__(self, xsdElement, parent):
        """Flags the containing type with a wildcard attribute slot.
        Uses the ER ``__init__``.  See ElementRepresentative for more
        documentation.
        """
        super().__init__(xsdElement, parent)
        self.getContainingType().hasWildcardAttributes = True

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|anyAttribute.
        The name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return f"{contName}|anyAttribute"
