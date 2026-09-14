from pyxsd.element_representatives.element_representative import ElementRepresentative


class Notation(ElementRepresentative):
    """The class for the ``notation`` tag.

    A notation declaration records its ``name``, ``public`` and
    ``system`` values from ``tagAttributes``; it has no content model
    and takes no part in instance parsing beyond making schemas that
    declare or reference notations load.
    """

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
