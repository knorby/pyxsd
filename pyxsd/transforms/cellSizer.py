from transform      import Transform
from atom           import Atom
from vector         import Vector
from bravaisLattice import BravaisLattice

"""
============================
Transform Library: CellSizer
============================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Computational Materials Science
:Description: Library containing functions to change the size and shape of a crystal lattice structure
:Copyright: pyXSD License

Includes as part of the CellSizer Library:

- Atom
- Vector
- BravaisLattice

"""

class CellSizer (Transform):

    #============================================================
    #
    def cellSizerInit(self):
        self.atoms          = []
        self.bravaisVectors = {}
        self.vectorOrder    = []

    #============================================================
    #
    def getBravaisVectors(self):
        if not len(self.bravaisVectors) == 0:
            return self.bravaisVectors
        vectorNumCount = 1
        self.vectorOrder = []
        vectorsInXml = self.getElementsByName(self.root, 'bravaisVector') 
        for vector in vectorsInXml:
            vectorDef = vector._value_
            if not len(vectorDef) == 3:
                raise "For now, the getBravaisVectors() method is only meant for 3D vectors"
            i = float(vectorDef[0])
            j = float(vectorDef[1])
            k = float(vectorDef[2])
            obj = Vector((i, j, k))
            self.bravaisVectors['a%i' % vectorNumCount] = obj
            self.vectorOrder.append('a%i' % vectorNumCount)
            vectorNumCount+=1
        return self.bravaisVectors

    #============================================================
    #
    def getVectorList(self):
        vectorDict  = self.getBravaisVectors()
        vectors = []
        for vectorName in self.vectorOrder:
           vectors.append(vectorDict[vectorName])
        return vectors
                        
    
    #============================================================
    #
    def getAtoms(self):
        if not len(self.atoms) == 0:
            return self.atoms
        atomsInXml = self.getElementsByName(self.root, 'site')
        for atom in atomsInXml:
            atomDict = self.getAllSubElements(atom)
            position = atomDict['position'][0]._value_
            atomType = atomDict['atom'][0]._attribs_['ref']
            obj = Atom(position, atomType)
            self.atoms.append(obj)
        return self.atoms

    #============================================================
    #
    def makeNewXml (self, bravaisLattice):
        vectors = bravaisLattice.vectors
        basis   = bravaisLattice.basis
        xmlCrystalBasis = self.getElementsByName(self.root, 'crystalBasis')[0]
        xmlCrystalBasis._children_ = []
        for atom in basis:
            newAtomElement = self.makeNewXmlAtomElements(atom)
            xmlCrystalBasis._children_.append(newAtomElement)
        xmlBravaisLattice = self.getElementsByName(self.root, 'bravaisLattice')[0]
        xmlBravaisLattice._children_ = []
        for vectorName in self.vectorOrder:
            vector = vectors[vectorName]
            bravaisVector         = self.makeElemObj('bravaisVector')
            bravaisVector._value_ = vector
            xmlBravaisLattice._children_.append(bravaisVector)
            continue

        return self.root

    #============================================================
    #
    def makeNewXmlAtomElements(self, atom):
        
        position                 = atom.position
        atomType                 = atom.atomType
        siteObj                  = self.makeElemObj('site')
        positionObj              = self.makeElemObj('position')
        positionObj._value_      = position
        occupantObj              = self.makeElemObj('occupant')
        atomObj                  = self.makeElemObj('atom')
        atomObj._attribs_['ref'] = atomType

        occupantObj._children_.append(atomObj)
        siteObj._children_.append(occupantObj)
        siteObj._children_.append(positionObj)

        return siteObj
    #============================================================
    #
    def makeAtom(self, position, atomType):
        return Atom(position, atomType)
    #============================================================
    #
    def makeBravaisLattice(self, newVectors, newAtoms):
        return  BravaisLattice(newVectors, newAtoms)

    #============================================================
    #
    def getCartesianCoords(self, vectors, position):
        coords = []
        for g in range(0, 3):
            total = 0
            currentPos = position[g]
            for vector in vectors:
                total+= currentPos * vector[g]
            coords.append(total)
        return coords

    #============================================================
    #
    def findCenter(self, vectors):
        centerPos = [.5, .5, .5] #in terms of vector
        return self.getCartesianCoords(vectors, centerPos) #centerPos
           
