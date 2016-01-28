class BravaisLattice (object):
    """
    ============================================
    Transform Library: CellSizer: BravaisLattice
    ============================================

    :Author: Kali Norby <kali.norby@gmail.com>
    :Date: Fri, 1 Sept 2006
    :Category: Computational Materials Science
    :Description: Class for a bravais lattice
    :Copyright: pyXSD License

    Included as part of the CellSizer Library
    """
    #=======================================================
    #

    def __init__(self, vectors, basis):

        self.vectors = vectors
        self.basis = basis
