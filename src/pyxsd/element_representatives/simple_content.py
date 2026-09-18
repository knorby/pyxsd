from pyxsd.element_representatives.element_representative import ElementRepresentative


class SimpleContent(ElementRepresentative):
    """The class for the simpleContent tag."""

    #: ``simpleContent`` wraps exactly one derivation.
    _ALLOWED_CHILDREN = ("annotation", "restriction", "extension")
    _MAX_ONE_CHILDREN = ("annotation", "restriction", "extension")
    _CHILD_ORDER = (("annotation",), ("restriction", "extension"))
    _ONE_OF_SLOTS = frozenset({1})

    #: Model groups, which carry element content and are therefore
    #: illegal under a simple content model.
    _MODEL_GROUP_CLASSES = ("Group", "All", "Choice", "Sequence")

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)

    def checkDeclarationLegality(self) -> None:
        """Reports a model group inside a ``simpleContent`` derivation.

        ``simpleContent`` has simple content: its ``restriction``/
        ``extension`` may add attributes (and facets) but no element
        content, so a ``group``/``all``/``choice``/``sequence`` child is
        a schema error even though the shared ``Restriction`` and
        ``Extension`` grammar admits those tags for the
        ``complexContent`` case.
        """
        for derivation in self.processedChildren or ():
            if derivation is None or type(derivation).__name__ not in (
                "Restriction",
                "Extension",
            ):
                continue
            for child in derivation.processedChildren or ():
                if child is None:
                    continue
                if type(child).__name__ in self._MODEL_GROUP_CLASSES:
                    self._reportSchemaError(
                        "simpleContent must not contain a "
                        f"{type(child).__name__.lower()} model group",
                        code="declaration-child",
                    )

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|simpleContent.
        The name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return contName + "|simpleContent"
