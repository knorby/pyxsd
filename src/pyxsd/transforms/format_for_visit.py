from pyxsd.transforms.cell_sizer import CellSizer
from pyxsd.transforms.displayer import Displayer


class FormatForVisit(CellSizer, Displayer):
    def __init__(self, root):
        self.root = root
        self.cellSizerInit()

    def __call__(self, scale=1, fileName=None):
        self.writeFormat(scale, self.openFile(fileName))
        return self.root

    def writeFormat(self, scale, fd):
        print(scale, file=fd)
        atoms = self.getAtoms()
        vectors = self.getVectorList()
        for vector in vectors:
            print(vector[0], vector[1], vector[2], file=fd)
        print(len(atoms), file=fd)
        for atom in atoms:
            print(
                atom.atomType[4:],
                atom.position[0],
                atom.position[1],
                atom.position[2],
                file=fd,
            )
