from elementRepresentative import ElementRepresentative

#============================================================
#
class Sequence(ElementRepresentative):
    """
    The class for the sequence tag. Subclass of *ElementRepresentative*.
    """
    def __init__(self, xsdElement, parent):
        """
        Adds itself to the sequencesOrChoices list in its containing complexType.
        Makes a blank list for element children.
        Uses the ER '__init__`.
        See *ElementRepresentative* for more documentation.
        """
        self. elements = []

        ElementRepresentative.__init__(self, xsdElement, parent)

        self.getContainingType().sequencesOrChoices.append(self)

    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- sequence`some id number`. 
        """
        choiceNum = len(self.getContainingType().sequencesOrChoices) + 1

        name = "sequence%i" % choiceNum

        return name

