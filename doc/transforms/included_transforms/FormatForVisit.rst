=========================
Transform: FormatForVisit
=========================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Computational Materials Science
:Description: Writes out data to be a format that can easily be converted for use with Visit
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

FormatForVisit is part of the pyXSD standard release

-------------------

Standard Call
=============

FormatForVisit(scale=1, fileName=None)

Arguments (detailed)
====================
Scale=1 : number
    Scale factor to use
fileName=None : string
    The fileName to use for the output. If not specified, the data will be sent to stdout.

Description
===========

What It Does
------------
Writes to a file or stdout the following format which can easily be converted to Visit:

 Scale factor for Cell vectors
 Cell vector 1
 Cell vector 2
 Cell vector 3
 Total # atoms 
 Species, atoms in cell vector fractions
 ...

What it Returns
---------------
The tree just as it was.
