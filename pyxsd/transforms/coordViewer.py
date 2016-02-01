from cellSizer import CellSizer
from displayer import Displayer

"""
======================
Transform: CoordViewer
======================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Computational Materials Science
:Description: Writes out atom positions in cartesian coordinates in order to
              help write and use transforms
:Copyright: pyXSD License
"""


class CoordViewer (CellSizer, Displayer):

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
            print >> fd, coords
