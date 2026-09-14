from pyxsd.element_representatives.element_representative import ElementRepresentative


class Sequence(ElementRepresentative):
    """The class for the sequence tag."""

    def __init__(self, xsdElement, parent):
        """Adds itself to the sequencesOrChoices list in its containing
        complexType.  Makes a blank list for element children.  Uses the
        ER ``__init__``.  See ElementRepresentative for more
        documentation.
        """
        self.elements = []
        super().__init__(xsdElement, parent)
        containingType = self.getContainingType()
        compositors = getattr(containingType, "sequencesOrChoices", None)
        if compositors is not None:
            compositors.append(self)
        else:
            self.misplacement = (
                "misplaced-declaration",
                f"sequence cannot appear inside {containingType.__class__.__name__}",
            )

    def getName(self):
        """Makes a name like this- sequence``some id number``."""
        compositors = getattr(self.getContainingType(), "sequencesOrChoices", None)
        sequenceNum = len(compositors) + 1 if compositors is not None else 1
        return f"sequence{sequenceNum}"
