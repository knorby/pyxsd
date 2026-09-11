"""Example transform: write atom positions as Cartesian coordinates.

Historical note: shipped in ``pyxsd.transforms`` in 0.1; moved to
``examples/legacy/`` in pyxsd 1.0 (breaking change).
"""

from cell_sizer import CellSizer

from pyxsd.transforms import Displayer


class CoordViewer(CellSizer, Displayer):
    def __init__(self, root):
        self.root = root
        self.cellSizerInit()

    def __call__(self, fileName=None):
        self.displayCoords(self.openFile(fileName))
        return self.root

    def displayCoords(self, fd):
        atoms = self.getAtoms()
        vectors = self.getVectorList()
        for atom in atoms:
            coords = self.getCartesianCoords(vectors, atom.position)
            coords = tuple(coords)
            print(coords, file=fd)
