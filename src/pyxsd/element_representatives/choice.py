from pyxsd.element_representatives.element_representative import ElementRepresentative


class Choice(ElementRepresentative):
    """The class for the choice tag."""

    def __init__(self, xsdElement, parent):
        """Adds itself to the sequencesOrChoices list in its containing
        complexType.  Makes a blank list for element children.  Uses the
        ER ``__init__``.

        See ElementRepresentative for more documentation.
        """
        self.elements = []
        super().__init__(xsdElement, parent)
        self.getContainingType().sequencesOrChoices.append(self)

    def getName(self):
        """Makes a name like this- choice``some id number``."""
        choiceNum = len(self.getContainingType().sequencesOrChoices) + 1
        return f"choice{choiceNum}"

    def getMinOccurs(self):
        """Retrieves the minOccurs value for elements in the choice.  Sets
        it to the default of 1 if it is not specified.
        """
        return int(getattr(self, "minOccurs", 1))

    def getMaxOccurs(self):
        """Retrieves the maxOccurs value for elements in the choice.  Sets
        it to the default of 1 if it is not specified.  Sets
        'unbounded' values to 99999, since it needs to be an integer.
        """
        maxOccurs = getattr(self, "maxOccurs", 1)
        if maxOccurs == "unbounded":
            return 99999
        return int(maxOccurs)
