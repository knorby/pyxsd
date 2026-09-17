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
