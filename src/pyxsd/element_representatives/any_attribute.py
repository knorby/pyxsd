from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.wildcards import register_wildcard, wildcard_spec


class AnyAttribute(ElementRepresentative):
    """The class for the ``anyAttribute`` tag (attribute wildcard).

    Marks the containing type as accepting attributes that the schema
    does not declare, subject to the wildcard's namespace constraint.
    ``processContents`` decides whether a matching global attribute
    declaration is required (``strict``) or only used when present
    (``lax``); ``skip`` accepts without checking.
    """

    def __init__(self, xsdElement, parent):
        """Flags the containing type with a wildcard attribute slot and
        records the namespace/processContents constraint. Uses the ER
        ``__init__``.  See ElementRepresentative for more documentation.
        """
        super().__init__(xsdElement, parent)
        self.wildcardSpec = wildcard_spec(self.tagAttributes, is_attribute=True)
        register_wildcard(self.getContainingType(), self.wildcardSpec)

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|anyAttribute.
        The name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return f"{contName}|anyAttribute"
