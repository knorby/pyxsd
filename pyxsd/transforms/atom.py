class Atom (object):
    """

    ==================================
    Transform Library: CellSizer: Atom
    ==================================

    :Author: Kali Norby <kali.norby@gmail.com>
    :Date: Fri, 1 Sept 2006
    :Category: Computational Materials Science
    :Description: A class for atoms
    :Copyright: pyXSD License

    Included as part of the CellSizer Library

    """
    #=======================================================
    #

    def __init__(self, position, atomType):

        self.position = position
        self.atomType = atomType
