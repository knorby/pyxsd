======================
Transform: CoordViewer
======================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Computational Materials Science
:Description: Writes out atom positions in cartesian coordinates in order to help write and use transforms
:Copyright: pyXSD License

.. contents::

------------------

Dependencies
============

- CellSizer Transform Library
  -Vector class
  -Atom class
  -BravaisLattice class
- Displayer (part of the standard (part of standard transform set)

Other Information
=================

CoordViewer is part of the pyXSD standard release

-------------------

Standard Call
=============

CoordViewer(fileName=None)

Arguments (detailed)
====================

fileName=None : string
    The fileName to use for the output. If not specified, the data will be sent to stdout.

Description
===========

What It Does
------------
Writes to a file or stdout the cartesian coordinates for all the atoms in the tree.
Can be useful to check the data at a particular point.

What it Returns
---------------
The tree just as it was.
