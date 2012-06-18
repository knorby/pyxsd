=====================
Transform: ExpandCell
=====================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Computational Materials Science
:Description: Expands a cell by given parameters
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

ExpandCell is part of the pyXSD standard release

-------------------

Standard Call
=============

ExpandCell(a1Expand, a2Expand, a3Expand)

Arguments (detailed)
====================

a1Expand : Number
    Expansion factor for the first vector
a2Expand : Number
    Expansion factor for the second vector
a3Expand : Number
    Expansion factor for the third vector

Description
===========

What It Does
------------
Expands a primitive cell (or any cell) by the expansion factors stated. Changes the
positions of the atoms so that they are in terms of the new vectors. Adds new atoms.

What it Returns
---------------
The tree with the same general structure as before, but changes data and adds atoms. 
