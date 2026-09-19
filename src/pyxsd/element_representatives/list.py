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
        containing = self.getContainingType()
        containing.listItemType = self.itemType
        # An inline item ``simpleType`` is the other way a list names its
        # item type; record it so class building resolves and enforces it.
        inline = [
            child
            for child in self.processedChildren
            if child is not None and child.__class__.__name__ == "SimpleType"
        ]
        containing.listInlineItem = inline[0] if inline else None
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
        or a union with no list type anywhere in its transitive
        membership (stJ002; nested unions are followed). A list item type
        that is itself a list, a union with a transitive list member, a
        complex type or a built-in list is reported as
        ``atomic-required``.
        """
        containingName = self.getContainingTypeName()
        inline = [
            child
            for child in self.processedChildren or ()
            if child is not None and child.__class__.__name__ == "SimpleType"
        ]
        if self.itemType is not None and inline:
            # XSD 1.1 §3.16.2.1: a list takes its item type from either
            # the ``itemType`` attribute or an inline ``simpleType``
            # child, never both (stD018).
            self._reportSchemaError(
                f"list '{containingName}' has both an itemType attribute and an inline simpleType",
                code="declaration-duplicate",
            )
        if self.itemType is not None:
            self._checkItemType(self.itemType, f"list '{containingName}'")
        for child in inline:
            self._checkInlineItemType(child, containingName)

    def _checkItemType(self, itemType, owner):
        variety, er = self.varietyOfReference(itemType)
        if er is not None and self._finalBlocks(er, "list"):
            self._reportSchemaError(
                f"item type '{itemType}' of {owner} is final for list derivation",
                code="final",
            )
        if self._itemVarietyIsAtomic(variety, er):
            return
        self._reportSchemaError(
            f"item type '{itemType}' of {owner} is not an atomic simple type",
            code="atomic-required",
        )

    def _checkInlineItemType(self, child, containingName):
        if self._finalBlocks(child, "list"):
            self._reportSchemaError(
                f"inline item type '{child.name}' of list '{containingName}' is "
                "final for list derivation",
                code="final",
            )
        variety = child.simpleVariety()
        if self._itemVarietyIsAtomic(variety, child):
            return
        self._reportSchemaError(
            f"inline item type '{child.name}' of list '{containingName}' is not "
            "an atomic simple type",
            code="atomic-required",
        )

    @staticmethod
    def _finalBlocks(er, method):
        """Whether a type's effective ``final`` excludes *method*."""
        getter = getattr(er, "effectiveFinal", None)
        if getter is None:
            return False
        final = getter()
        if not final:
            return False
        tokens = str(final).split()
        return "#all" in tokens or method in tokens

    def _itemVarietyIsAtomic(self, variety, er):
        """Whether a resolved item type satisfies the atomicity rule.

        An unresolved type (``None``) is left to the ``unknown-type``
        check; a union is acceptable when no list type appears anywhere in
        its transitive membership (nested unions are followed).
        """
        if variety is None:
            return True
        if variety == "atomic":
            return True
        if variety == "union":
            return er is not None and self.unionTransitiveMembershipHasNoList(er)
        return False
