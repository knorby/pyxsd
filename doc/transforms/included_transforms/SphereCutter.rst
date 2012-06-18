=======================
Transform: SphereCutter
=======================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Computational Materials Science
:Description: Cleaves off atoms that are not in a sphere that a user specifies
:Copyright: pyXSD License

.. contents::

------------------

Dependencies
============

- CellSizer Transform Library
  -Vector class
  -Atom class
  -BravaisLattice class

Other Information
=================

SphereCutter is part of the pyXSD standard release

-------------------

Standard Call
=============

SphereCutter(rad, sphereCenter=None)

Arguments (detailed)
====================

rad : Number
    The radius of the sphere. The radius is in terms of the units used for the cartesian coordinates in the system.
sphereCenter : Tuple (length=3)
    Optional, uses the cell center if not specified. Lets you specify the center of the sphere in cartesian coordinates. Looks like: (x, y, z) where `x`, `y`, and `z` are numbers.    

Description
===========

What It Does
------------

Tests each atom to see if it is in the sphere that was specified. If it is not, that
atom is deleted from the tree. The arguments must be made in whatever unit the cartesian
system is in, so these numbers are *not* in terms of the vectors. There are a few other
transforms which print out the cartesian coordinates for each atom. These can help users
better understand the data. NOTE: when used with a large cell, SphereCutter can cause pyXSD
to run longer than it normally does.

What it Returns
---------------
The tree with the same general structure as before, but with fewer atoms.
