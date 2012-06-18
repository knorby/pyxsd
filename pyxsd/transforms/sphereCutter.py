from cellSizer import CellSizer

class SphereCutter(CellSizer):

    #============================================================
    #
    def __init__(self, root):
        self.root = root
        self.cellSizerInit()
    #============================================================
    #
    def __call__(self, rad, sphereCenter=None):
        #sphereCenter is the center of the material if left None,
        #and it is (x, y, z) in Cartesian Coords if it is not.
        return self.cut(rad, sphereCenter)

    #============================================================
    #
    def testSphereMembership(self, coords, sphereCenter, rad):
        calc = 0.0
        for f in range(0, 3):
            cms = coords[f]-sphereCenter[f]
            calc+=pow(cms, 2)
        test = (self.radSquared >= calc)
        return test
            
    #============================================================
    #
    def cut(self, rad, sphereCenter=None):
        vectorDict  = self.getBravaisVectors()
        vectors = []
        self.radSquared = pow(rad, 2)
        for vectorName in self.vectorOrder:
             vectors.append(vectorDict[vectorName])
        atoms    = self.getAtoms()
        newAtoms = []
        if not isinstance(rad, float):
            try:
                rad = float(rad)
            except:
                raise TypeError, "the radius of a sphere must be expressed as a number" 
        if not sphereCenter:
            sphereCenter = self.findCenter(vectors)
        else:
            if not len(sphereCenter) == 3 or not isinstance(sphereCenter, tuple):
                raise TypeError, "there must be three coords for the center of a sphere and they must be in a tuple"
        for atom in atoms:
            atomPos    = atom.position
            atomCoords = self.getCartesianCoords(vectors, atomPos)
            if self.testSphereMembership(atomCoords, sphereCenter, rad):
                newAtoms.append(atom)


        if len(newAtoms) == 0:
            print "Sphere Cutter Error: no atoms remain after the cut. Please check the data."
        elif len(newAtoms) == len(atoms):
            print "Sphere Cutter Error: no atoms were removed. Please check your data."

        newLattice = self.makeBravaisLattice(vectorDict, newAtoms)
        return self.makeNewXml(newLattice)
