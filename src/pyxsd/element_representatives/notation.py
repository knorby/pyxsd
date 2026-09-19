from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.xsd_data_types import NCName


class Notation(ElementRepresentative):
    """The class for the ``notation`` tag.

    A notation declaration records its ``name``, ``public`` and
    ``system`` values from ``tagAttributes``; it has no content model
    beyond an optional leading annotation.
    """

    #: ``notation`` may carry a single ``annotation`` child and nothing
    #: else (XSD 1.0/1.1).
    _ALLOWED_CHILDREN = ("annotation",)
    _MAX_ONE_CHILDREN = ("annotation",)

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation.

        ``xs:notation`` is only legal as a top-level declaration; a
        nested one (for example inside an attribute or an extension)
        is reported as misplaced so the schema is not silently
        accepted.
        """
        super().__init__(xsdElement, parent)
        if self.parent is not None and self.parent.__class__.__name__ != "Schema":
            self.misplacement = (
                "misplaced-declaration",
                f"notation '{self.name}' must be a top-level declaration",
            )

    def getName(self):
        """Returns the notation's schema name."""
        return self.xsdElement.get("name")

    def checkDeclarationLegality(self):
        """Reports notation attribute (XML) constraints.

        A notation must carry at least one of ``public``/``system`` and
        its ``name`` must be a valid NCName. The ``id`` (lexical and
        uniqueness) is checked generically by the parser.
        """
        self._checkUnexpectedAttributes()
        if self._hasCharacterContent():
            self._reportSchemaError(
                f"notation '{self.name}' must be empty; character content "
                "is not allowed inside <notation>",
                code="declaration-child",
            )
        if self.tagAttributes.get("public") is None and self.tagAttributes.get("system") is None:
            self._reportSchemaError(
                f"notation '{self.name}' must have a public or system identifier",
                code="declaration-attribute",
            )
        name = self.xsdElement.get("name")
        if name is not None:
            try:
                NCName(name)
            except TypeError:
                self._reportSchemaError(
                    f"notation name '{name}' is not a valid NCName",
                    code="declaration-attribute",
                )

    #: The only attributes a notation declaration may carry, unqualified
    #: (the ``id`` is validated generically by the parser).
    _ALLOWED_ATTRIBUTES = frozenset({"name", "id", "public", "system"})

    def _checkUnexpectedAttributes(self) -> None:
        """Reports an unqualified attribute that is not the notation's own.

        ``notatE003`` writes ``foo="bar"``; a namespaced attribute (an
        XML-Schema-namespace one is ``notatE002``) is handled by the
        parser's schema-attribute check.
        """
        for attr in self.xsdElement.attrib:
            if attr.startswith("{"):
                continue
            if attr not in self._ALLOWED_ATTRIBUTES:
                self._reportSchemaError(
                    f"attribute '{attr}' is not allowed on <notation>",
                    code="unexpected-attribute",
                )

    def _hasCharacterContent(self) -> bool:
        """Whether the notation element carries non-whitespace text.

        The schema-for-schemas gives ``notation`` empty content; text or
        an entity reference (``notatG001``/``notatG003``) is illegal.
        """
        if self.xsdElement.text and self.xsdElement.text.strip():
            return True
        return any(child.tail and child.tail.strip() for child in self.xsdElement)
