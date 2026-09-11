"""Shared library for the crystallography example transforms.

CellSizer holds the machinery the example transforms build on:
reading atom positions and Bravais vectors from a parsed instance
tree, converting to Cartesian coordinates, and cutting/moving atoms.
The concrete transforms are :class:`ExpandCell`, :class:`SphereCutter`,
:class:`CoordViewer`, and :class:`FormatForVisit`.

Historical note: these were shipped in ``pyxsd.transforms`` in 0.1;
they moved to ``examples/legacy/`` in pyxsd 1.0 (breaking change).
"""

from atom import Atom
from bravais_lattice import BravaisLattice
from vector import Vector

from pyxsd.transforms import Transform


class CellSizer(Transform):
    def cellSizerInit(self):
        self.atoms = []
        self.bravaisVectors = {}
        self.vectorOrder = []

    def getBravaisVectors(self):
        if self.bravaisVectors:
            return self.bravaisVectors
        vectorNumCount = 1
        self.vectorOrder = []
        vectorsInXml = self.getElementsByName(self.root, "bravaisVector")
        for vector in vectorsInXml:
            vectorDef = vector._value_
            if len(vectorDef) != 3:
                raise TypeError(
                    "For now, the getBravaisVectors() method is only meant for 3D vectors"
                )
            i = float(vectorDef[0])
            j = float(vectorDef[1])
            k = float(vectorDef[2])
            obj = Vector((i, j, k))
            self.bravaisVectors[f"a{vectorNumCount}"] = obj
            self.vectorOrder.append(f"a{vectorNumCount}")
            vectorNumCount += 1
        return self.bravaisVectors

    def getVectorList(self):
        vectorDict = self.getBravaisVectors()
        vectors = []
        for vectorName in self.vectorOrder:
            vectors.append(vectorDict[vectorName])
        return vectors

    def getAtoms(self):
        if self.atoms:
            return self.atoms
        atomsInXml = self.getElementsByName(self.root, "site")
        for atom in atomsInXml:
            atomDict = self.getAllSubElements(atom)
            position = atomDict["position"][0]._value_
            atomType = atomDict["atom"][0]._attribs_["ref"]
            obj = Atom(position, atomType)
            self.atoms.append(obj)
        return self.atoms

    def makeNewXml(self, bravaisLattice):
        vectors = bravaisLattice.vectors
        basis = bravaisLattice.basis
        xmlCrystalBasis = self.getElementsByName(self.root, "crystalBasis")[0]
        xmlCrystalBasis._children_ = []
        for atom in basis:
            newAtomElement = self.makeNewXmlAtomElements(atom)
            xmlCrystalBasis._children_.append(newAtomElement)
        xmlBravaisLattice = self.getElementsByName(self.root, "bravaisLattice")[0]
        xmlBravaisLattice._children_ = []
        for vectorName in self.vectorOrder:
            vector = vectors[vectorName]
            bravaisVector = self.makeElemObj("bravaisVector")
            bravaisVector._value_ = vector
            xmlBravaisLattice._children_.append(bravaisVector)

        return self.root

    def makeNewXmlAtomElements(self, atom):
        position = atom.position
        atomType = atom.atomType
        siteObj = self.makeElemObj("site")
        positionObj = self.makeElemObj("position")
        positionObj._value_ = position
        occupantObj = self.makeElemObj("occupant")
        atomObj = self.makeElemObj("atom")
        atomObj._attribs_["ref"] = atomType

        occupantObj._children_.append(atomObj)
        siteObj._children_.append(occupantObj)
        siteObj._children_.append(positionObj)

        return siteObj

    def makeAtom(self, position, atomType):
        return Atom(position, atomType)

    def makeBravaisLattice(self, newVectors, newAtoms):
        return BravaisLattice(newVectors, newAtoms)

    def getCartesianCoords(self, vectors, position):
        coords = []
        for g in range(0, 3):
            total = 0
            currentPos = position[g]
            for vector in vectors:
                total += currentPos * vector[g]
            coords.append(total)
        return coords

    def findCenter(self, vectors):
        centerPos = [0.5, 0.5, 0.5]  # in terms of vector
        return self.getCartesianCoords(vectors, centerPos)
