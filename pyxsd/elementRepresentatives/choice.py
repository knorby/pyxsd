from elementRepresentative import ElementRepresentative

#============================================================
#
class Choice(ElementRepresentative):
    """
    The class for the choice tag. Subclass of *ElementRepresentative*.
    """

    def __init__(self, xsdElement, parent):
        """
        Adds itself to the sequencesOrChoices list in its containing complexType.
        Makes a blank list for element children.
        Uses the ER '__init__`.
        See *ElementRepresentative* for more documentation.
        """
        self.elements = []
        
        ElementRepresentative.__init__(self, xsdElement, parent)

        self.getContainingType().sequencesOrChoices.append(self)
        
    #============================================================
    #
    def getName(self):
        """
        Makes a name like this- choice`some id number`. 
        """
        choiceNum = len(self.getContainingType().sequencesOrChoices) + 1
        
        contName = self.getContainingTypeName()

        name = "choice%i" % (choiceNum)

        return name

    #============================================================ 
    #
    def getMinOccurs(self):
        """
        retrieves the minOccurs value for elements in the choice.
        Sets it to the default of 1 if it is not specified.

        No parameters.
        """

        return int(getattr(self, 'minOccurs', 1)) 

    #============================================================ 
    # 
    def getMaxOccurs(self):
        """
        retrieves the maxOccurs value for elements in the choice.
        Sets it to the default of 1 if it is not specified.
        Sets 'unbounded' values to 99999, since it needs to be an
        integer.

        No parameters.
        """
        maxOccurs = getattr(self, 'maxOccurs', 1)
        if maxOccurs == 'unbounded':
            return 99999
        return int(maxOccurs)


        
