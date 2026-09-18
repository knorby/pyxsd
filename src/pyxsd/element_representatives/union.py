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

    #: A ``union`` is an annotation plus any number of inline member
    #: ``simpleType`` children, so only the annotation is max-one.
    _ALLOWED_CHILDREN = ("annotation", "simpleType")
    _MAX_ONE_CHILDREN = ("annotation",)
    _CHILD_ORDER = (("annotation",), ("simpleType",))

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
            child for child in self.processedChildren if child.__class__.__name__ == "SimpleType"
        ]

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|union.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return contName + "|union"

    def checkDeclarationLegality(self):
        """Reports a union member type that is not a simple type.

        A union member type must be a simple type definition: an atomic
        type, a list, or (transitively) another union are all legal
        (stK004), but a complex type is not (stK003). Each offending
        ``memberTypes`` name and inline member is reported as
        ``atomic-required``. An unresolvable name is left to the
        ``unknown-type`` check that runs while the union class is built.
        """
        containingName = self.getContainingTypeName()
        owner = f"union '{containingName}'"
        inline = [
            child
            for child in self.processedChildren or ()
            if child is not None and child.__class__.__name__ == "SimpleType"
        ]
        for memberName in self.memberTypes:
            if self._memberVarietyIsLegal(*self.varietyOfReference(memberName)):
                continue
            self._reportSchemaError(
                f"member type '{memberName}' of {owner} is not a simple type",
                code="atomic-required",
            )
        for child in inline:
            if self._memberVarietyIsLegal(child.simpleVariety(), child):
                continue
            self._reportSchemaError(
                f"inline member type '{child.name}' of {owner} is not a simple type",
                code="atomic-required",
            )

    @staticmethod
    def _memberVarietyIsLegal(variety, _er):
        """Whether a resolved member variety is a legal simple type.

        Atomic, list and union members are all legal; a complex type is
        not. An unresolved type (``None``) is left to the ``unknown-type``
        check so one reference does not produce two errors.
        """
        return variety in ("atomic", "list", "union", None)
