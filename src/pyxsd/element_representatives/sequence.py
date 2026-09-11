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
        self.getContainingType().sequencesOrChoices.append(self)

    def getName(self):
        """Makes a name like this- sequence``some id number``."""
        sequenceNum = len(self.getContainingType().sequencesOrChoices) + 1
        return f"sequence{sequenceNum}"
