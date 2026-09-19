from pyxsd.element_representatives.xsd_type import XsdType


class SimpleType(XsdType):
    """The class for the simpleType tag."""

    #: A ``simpleType`` is an annotation plus exactly one derivation.
    _ALLOWED_CHILDREN = ("annotation", "restriction", "list", "union")
    _MAX_ONE_CHILDREN = ("annotation", "restriction", "list", "union")
    _CHILD_ORDER = (
        ("annotation",),
        ("restriction",),
        ("list",),
        ("union",),
    )
    #: Each derivation kind is its own slot; occupying one excludes any
    #: later kind (restriction+list, list+union, ...).
    _EXCLUSIVE_SLOTS = frozenset({1, 2, 3})

    def __init__(self, xsdElement, parent):
        """Creates blank variables for a series of facets, which are
        variables that specify restrictions on values.  Adds itself to
        the simpleType dictionary in the *schema* class instance.  Uses
        the XsdType ``__init__``.

        See ElementRepresentative for documentation.
        """
        self.patterns = []
        self.assertions = []
        self.length = None
        self.minLength = None
        self.maxLength = None
        self.totalDigits = None
        self.fractionDigits = None
        self.whiteSpace = None
        self.explicitTimezone = None
        self.listItemType = None
        self.minInclusive = None
        self.maxInclusive = None
        self.minExclusive = None
        self.maxExclusive = None
        super().__init__(xsdElement, parent)
        self.getSchema().simpleTypes[self.name] = self

    def checkDeclarationLegality(self):
        """Reports ``simpleType`` representation problems.

        Covers the ``name`` NCName, the rule that only a direct schema
        child may be named (an inline ``simpleType`` -- under an
        element/attribute declaration, a restriction, a list or a union
        -- is anonymous), and the requirement that the declaration
        carry exactly one of ``restriction``/``list``/``union``. The
        msData syntax family pins these (stA008-stA017, stB001).
        """
        name = self.xsdElement.get("name")
        if name is not None and "|" not in name:
            # A pipe marks an internal bookkeeping name (an inline
            # declaration's generated identifier); it is not an author
            # NCName.
            if self._invalidNCName(name):
                self._reportSchemaError(
                    f"simpleType name '{name}' is not a valid NCName",
                    code="declaration-attribute",
                )
            if not self.isGlobalDeclaration():
                self._reportSchemaError(
                    f"inline simpleType must not carry a name ('{name}')",
                    code="declaration-attribute",
                )
        derivations = [
            child
            for child in self.processedChildren or ()
            if child is not None and type(child).__name__ in ("Restriction", "List", "Union")
        ]
        if not derivations:
            self._reportSchemaError(
                f"simpleType '{self.name}' has no restriction, list or union",
                code="declaration-child",
            )
