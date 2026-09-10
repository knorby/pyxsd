from pyxsd.element_representatives.element_representative import ElementRepresentative


class All(ElementRepresentative):
    """The class for the ``all`` tag.

    Like ``sequence`` and ``choice``, but its element children may
    appear in any order in an instance document (each at most once in
    XSD 1.0). Adds itself to the ``sequencesOrChoices`` list in its
    containing type, so the compositor machinery treats it uniformly.
    """

    def __init__(self, xsdElement, parent):
        """Adds itself to the sequencesOrChoices list in its containing
        type.  Makes a blank list for element children.  Uses the ER
        ``__init__``.  See ElementRepresentative for more
        documentation.
        """
        self.elements = []
        super().__init__(xsdElement, parent)
        self.getContainingType().sequencesOrChoices.append(self)

    def getName(self):
        """Makes a name like this- ``all````some id number``."""
        allNum = len(self.getContainingType().sequencesOrChoices) + 1
        return f"all{allNum}"
