============================
Transform Library: CellSizer
============================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Computational Materials Science
:Description: Library containing functions to change the size and shape of a crystal lattice structure
:Copyright: pyXSD License

.. contents:

------------------

Dependencies
============

outside of library
------------------

None

part of library
---------------

- `Vector` class
- `Atom` class
- `BravaisLattice` class

Other Information
=================

CellSizer is part of the pyXSD standard release

-------------------

Functions
=========

CellSizer methods
-----------------

cellSizerInit()
+++++++++++++++

 **Args**: *No Args*

 **Description**: Adds on Init function for use with any class dependent on *CellSizer*


getBravaisVectors()
+++++++++++++++++++

 **Args**: *No Args*

 **Description**: Gets the vectors from the tree, makes them instances of *Vector*, 
 stores them in self.bravaisVectors, and returns self.bravaisVectors. self.bravaisVector
 is a dictionary with `a1`, `a2`, and `a3` as keys. Stores order in self.vectorOrder.

getVectorList()
+++++++++++++++

 **Args**: *No Args*

 **Description**: Returns the vectors that getBravaisVectors() produces in an ordered
 list.

getAtoms()
+++++++++++

 **Args**: *No Args*

 **Description**: Gets the atoms from the tree, and creates instances of these atoms with the class  *Atom*.
 In the instance, the variable `position` is a list containing the components of the position in  terms of
 the vectors, and the variable `atomType` is a string containg the atom type. Stores the instances  in the
 list self.atoms, and returns that list.


makeNewXml(bravaisLattice)
++++++++++++++++++++++++++

 **Args**:

 bravaisLattice : Instance of *BravaisLattice*
     An instance of the class *BravaisLattice*, which contains the vectors and atoms
    
 **Description**: Replaces the old vectors and atoms with the new ones in the tree.
 Works only for xml documents following the ORNL CMS schema. Returns root.

makeNewXmlAtomElements(atom)
++++++++++++++++++++++++++++

 **Args**:

 atom : Instance of *atom*
     An instance of the class *atom*
    
 **Description**: formats an atom correctly for the xml tree.


makeAtom(position, atomType)
++++++++++++++++++++++++++++

 **Args**:

 position : list
     A list that has the component of the atom position in terms of the vectors
 atomType : string
     A string that states the atom type
    
 **Description**: Makes an instance of the *Atom* class with the given information.
 Use this function to avoid importing *Atom* in a transform class.

makeBravaisLattice(newVectors, newAtoms)
++++++++++++++++++++++++++++++++++++++++

 **Args**:

 newVectors : list
     A list of the Bravais vectors to use in the Bravais Lattice
 newAtoms : list
     A list of atoms to include in the Bravais Lattice
    
 **Description**: Makes an instance of the *BravaisLattice* class with the given information.
 Use this function to avoid importing *BravaisLattice* in a transform class.

getCartesianCoords(vectors, position)
+++++++++++++++++++++++++++++++++++++

 **Args**:

 vectors : list
     A list of the Bravais vectors
 position : list
     A list that has the component of the atom position in terms of the vectors
    
 **Description**: returns a list of the cartesian coords for an atom


findCenter(vectors)
+++++++++++++++++++

 **Args**:

 vectors : list
     A list of the Bravais vectors
    
 **Description**: returns a the center of a cell in cartesian coords


Vector Class Methods
--------------------

*Vector is a subclass of tuple*

__init__(val)
++++++++++++++

 **Args**:

 val : Sequence
    The i, j, k values that define a vector
    
 **Description**: Initializes the vector as a tuple, so `self` is a tuple of the values
 NOTE: `self` will not be mention in the list of arguments, but will be stated as an
 argument for for other vector methods.

__mul__(self, scalar)
+++++++++++++++++++++

 **Args**:

 scalar : Number
     The scalar to use
    
 **Description**: defines scalar multiplication. lets the user use the `\*` for multiplication.
 The scalar must be the second term.


__add__(self, vector)
+++++++++++++++++++++

 **Args**:

 vector : instance of the *Vector* class
     The vector to use
    
 **Description**: Defines vector addition. Allows one to use the *+* for vector additon.
 Returns a new vector.


__sub__(self, vector)
+++++++++++++++++++++

 **Args**:

 vector : instance of the *Vector* class
     The vector to use
    
 **Description**: Defines vector subtraction. Allows one to use the *-* for vector subtraction.
 Returns a new vector.


findDotProduct(self, vector)
++++++++++++++++++++++++++++

 **Args**:

 vector : instance of the *Vector* class
     The vector to use
    
 **Description**: Returns the dot product.


distance(self, vector)
++++++++++++++++++++++

 **Args**:

 vector : instance of the *Vector* class
     The vector to use
    
 **Description**: Returns the distance between two vectors.


findLength(self, vector)
++++++++++++++++++++++++

 **Args**:

 vector : instance of the *Vector* class
     The vector to use
    
 **Description**: returns the length of a vector

