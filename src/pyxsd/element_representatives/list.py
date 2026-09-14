from pyxsd.element_representatives.element_representative import ElementRepresentative


class List(ElementRepresentative):
    """The class for the list tag."""

    #: A ``list`` is an annotation plus either an ``itemType`` attribute
    #: or a single inline ``simpleType``.
    _ALLOWED_CHILDREN = ("annotation", "simpleType")
    _MAX_ONE_CHILDREN = ("annotation", "simpleType")
    _CHILD_ORDER = (("annotation",), ("simpleType",))

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        self.itemType = self.xsdElement.get("itemType")
        self.getContainingType().listItemType = self.itemType
        # the 'xs' is used so that it can be properly identified as a
        # primitive data type later on
        self.type = "xs:list"

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|list.  The name
        on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return contName + "|list"
