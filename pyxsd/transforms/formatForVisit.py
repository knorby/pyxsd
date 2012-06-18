from cellSizer import CellSizer
from displayer import Displayer

class FormatForVisit (CellSizer, Displayer):
    #============================================================
    #   
    def __init__(self, root):
        self.root = root
        self.cellSizerInit()

    #============================================================
    #
    def __call__(self, scale=1, fileName=None):
        self.writeFormat(scale, self.openFile(fileName))
        return self.root

    #============================================================
    #
    def writeFormat(self, scale, file):
        print >> file, scale
        atoms = self.getAtoms()
        vectors = self.getVectorList()
        for vector in vectors:
            print >> file, vector[0], vector[1], vector[2]
        print >> file, len(atoms)
        for atom in atoms:
            print >> file, atom.atomType[4:], atom.position[0], atom.position[1], atom.position[2]
