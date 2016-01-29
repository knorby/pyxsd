from cellSizer import CellSizer

"""
=====================
Transform: ExpandCell
=====================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Computational Materials Science
:Description: Expands a cell by given parameters
:Copyright: pyXSD License

"""



class ExpandCell (CellSizer):

    def __init__(self, root):
        self.root = root
        self.cellSizerInit()

    def __call__(self, a1Expand, a2Expand, a3Expand):
        return self.expand(a1Expand, a2Expand, a3Expand)

    def expand(self, a1Expand, a2Expand, a3Expand):
        atoms = self.getAtoms()
        newAtoms = []

        vectors = self.getBravaisVectors()

        a1 = vectors['a1']
        a2 = vectors['a2']
        a3 = vectors['a3']

        a1Length = a1.findLength()
        a2Length = a2.findLength()
        a3Length = a3.findLength()

        newA1 = a1 * a1Expand
        newA2 = a2 * a2Expand
        newA3 = a3 * a3Expand

        newA1Length = newA1.findLength()
        newA2Length = newA2.findLength()
        newA3Length = newA3.findLength()

        newVectors = dict([('a1', newA1), ('a2', newA2), ('a3', newA3)])

        for x in range(0, a1Expand):
            for y in range(0, a2Expand):
                for z in range(0, a3Expand):
                    for atom in atoms:
                        position = atom.position
                        position = map(lambda x: float(x), position)
                        newPosition = []
                        newPosition.append(
                            ((position[0] + x) * a1Length) / newA1Length)
                        # /newA2Length)
                        newPosition.append(
                            ((position[1] + y) * a2Length) / newA2Length)
                        # /newA3Length)
                        newPosition.append(
                            ((position[2] + z) * a3Length) / newA3Length)
                        newAtom = self.makeAtom(newPosition, atom.atomType)
                        newAtoms.append(newAtom)

        newLattice = self.makeBravaisLattice(newVectors, newAtoms)

        return self.makeNewXml(newLattice)
