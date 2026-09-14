from pyxsd.element_representatives.element_representative import ElementRepresentative


class Choice(ElementRepresentative):
    """The class for the choice tag."""

    #: A ``choice`` may carry at most one ``annotation``; its particle
    #: children (element/group/choice/sequence/any) may repeat.
    _MAX_ONE_CHILDREN = ("annotation",)

    def __init__(self, xsdElement, parent):
        """Adds itself to the sequencesOrChoices list in its containing
        complexType.  Makes a blank list for element children.  Uses the
        ER ``__init__``.

        See ElementRepresentative for more documentation.
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
                f"choice cannot appear inside {containingType.__class__.__name__}",
            )

    def getName(self):
        """Makes a name like this- choice``some id number``."""
        compositors = getattr(self.getContainingType(), "sequencesOrChoices", None)
        choiceNum = len(compositors) + 1 if compositors is not None else 1
        return f"choice{choiceNum}"

    def getMinOccurs(self):
        """Retrieves the minOccurs value for elements in the choice.  Sets
        it to the default of 1 if it is not specified.
        """
        return self._occursValue("minOccurs")

    def getMaxOccurs(self):
        """Retrieves the maxOccurs value for elements in the choice.  Sets
        it to the default of 1 if it is not specified.  Sets
        'unbounded' values to 99999, since it needs to be an integer.
        """
        return self._occursValue("maxOccurs")
