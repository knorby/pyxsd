from pyxsd.element_representatives.element_representative import ElementRepresentative

#: Constraining facets a restriction may apply. ``minExclusive`` and
#: friends are the XSD 1.0 set; ``assertion``, ``explicitTimezone`` and
#: the scale bounds are XSD 1.1 additions. ``last-day-of-month`` is a
#: hypothetical later-version facet tolerated here (saxonData VC/vc003
#: is valid under XSD 1.1 because such a versioned facet is ignored).
_FACET_CHILDREN = (
    "minExclusive",
    "minInclusive",
    "maxExclusive",
    "maxInclusive",
    "totalDigits",
    "fractionDigits",
    "length",
    "minLength",
    "maxLength",
    "enumeration",
    "whiteSpace",
    "pattern",
    "assertion",
    "explicitTimezone",
    "maxScale",
    "minScale",
    "last-day-of-month",
)

#: Facets that may appear at most once in a single restriction step
#: (XSD 1.1 §4.3.2 / §3.14.1).  ``pattern``, ``enumeration`` and
#: ``assertion`` are repeatable, so they are deliberately absent.
_SINGLETON_FACETS = frozenset(
    {
        "minExclusive",
        "minInclusive",
        "maxExclusive",
        "maxInclusive",
        "totalDigits",
        "fractionDigits",
        "length",
        "minLength",
        "maxLength",
        "whiteSpace",
        "explicitTimezone",
    }
)


class Restriction(ElementRepresentative):
    """The class for the restriction tag."""

    #: A restriction serves two contexts: a simple type restriction
    #: (``simpleType`` plus facets) and a complex type restriction (a
    #: particle plus attributes). The table is the union of both.
    _ALLOWED_CHILDREN = (
        "annotation",
        "openContent",
        "simpleType",
        "group",
        "all",
        "choice",
        "sequence",
        *_FACET_CHILDREN,
        "attribute",
        "attributeGroup",
        "anyAttribute",
        "assert",
    )
    _MAX_ONE_CHILDREN = (
        "annotation",
        "openContent",
        "simpleType",
        "group",
        "all",
        "choice",
        "sequence",
        "anyAttribute",
    )
    _CHILD_ORDER = (
        ("annotation",),
        ("openContent",),
        ("simpleType",),
        ("group", "all", "choice", "sequence"),
        _FACET_CHILDREN,
        ("attribute", "attributeGroup"),
        ("anyAttribute",),
        ("assert",),
    )
    #: The particle slot is an alternative: a restriction holds at most
    #: one of group/all/choice/sequence.
    _ONE_OF_SLOTS = frozenset({3})

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

    def checkDeclarationLegality(self):
        """Reports facet and base problems of a restriction.

        XSD 1.1 §4.3.2 forbids repeating a facet other than ``pattern``,
        ``enumeration`` and ``assertion`` within one restriction step; a
        repeated ``explicitTimezone`` (D4_3_16si02) or any other singleton
        facet makes the declaration invalid.

        A restriction serves both the simple-type and complex-type
        contexts. In the simple-type context (the containing type is a
        ``simpleType``) its children are the facets plus an optional
        inline ``simpleType`` -- no attribute or particle children
        (stC029) -- and its base must be a simple type that is not an
        ur-type (stC003, stI004, stZ005).
        """
        super().checkDeclarationLegality()
        counts: dict[str, int] = {}
        for child in self.xsdElement:
            local = child.tag.rpartition("}")[2]
            if local in _SINGLETON_FACETS:
                counts[local] = counts.get(local, 0) + 1
        for name in sorted(counts):
            if counts[name] > 1:
                self._reportSchemaError(
                    f"facet {name!r} is specified more than once in a restriction",
                    code="facet",
                )
        containing = type(self.getContainingType()).__name__
        if containing == "ComplexType":
            # A complex-content restriction carries a particle and
            # attributes, never constraining facets (addB112: a facet
            # inside ``complexContent``/``restriction``). A
            # simpleContent restriction may apply facets because it
            # constrains a simple value.
            parent = self.parent
            if parent is None or type(parent).__name__ != "SimpleContent":
                for tag in self.childTags:
                    if tag in _FACET_CHILDREN:
                        self._reportSchemaError(
                            f"<{tag}> is not allowed inside a complex content restriction",
                            code="declaration-child",
                        )
            return
        if containing != "SimpleType":
            return
        allowed = {"annotation", "simpleType", *_FACET_CHILDREN}
        for tag in self.childTags:
            if tag not in allowed:
                self._reportSchemaError(
                    f"<{tag}> is not allowed inside a simple type restriction",
                    code="declaration-child",
                )
        base = self.tagAttributes.get("base")
        if base is None:
            return
        variety, _er = self.varietyOfReference(base)
        if variety in ("complex", "non-atomic"):
            self._reportSchemaError(
                f"the base type '{base}' of a simple type restriction is not a simple type",
                code="invalid-base",
            )
