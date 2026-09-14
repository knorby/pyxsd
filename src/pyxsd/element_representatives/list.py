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

    def checkDeclarationLegality(self):
        """Reports a list item type that is not atomic.

        XSD 1.1 requires a list's item type to be an atomic simple type,
        or a union all of whose members are atomic (stJ002). A list item
        type that is itself a list, a union with a non-atomic member, a
        complex type or a built-in list is reported as
        ``atomic-required``.
        """
        containingName = self.getContainingTypeName()
        if self.itemType is not None:
            self._checkItemType(self.itemType, f"list '{containingName}'")
        for child in self.processedChildren or ():
            if child is not None and child.__class__.__name__ == "SimpleType":
                self._checkInlineItemType(child, containingName)

    def _checkItemType(self, itemType, owner):
        variety, er = self.varietyOfReference(itemType)
        if self._itemVarietyIsAtomic(variety, er):
            return
        self._reportSchemaError(
            f"item type '{itemType}' of {owner} is not an atomic simple type",
            code="atomic-required",
        )

    def _checkInlineItemType(self, child, containingName):
        variety = child.simpleVariety()
        if self._itemVarietyIsAtomic(variety, child):
            return
        self._reportSchemaError(
            f"inline item type '{child.name}' of list '{containingName}' is not "
            "an atomic simple type",
            code="atomic-required",
        )

    def _itemVarietyIsAtomic(self, variety, er):
        """Whether a resolved item type satisfies the atomicity rule.

        An unresolved type (``None``) is left to the ``unknown-type``
        check; a union is acceptable only when every member is atomic.
        """
        if variety is None:
            return True
        if variety == "atomic":
            return True
        if variety == "union":
            return er is not None and self.unionMembersAllAtomic(er)
        return False
