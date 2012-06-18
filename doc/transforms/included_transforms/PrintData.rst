====================
Transform: PrintData
====================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Standard Transform Tools
:Description: sends tree to the writer
:Copyright: pyXSD License

.. contents::

------------------

Dependencies
============

- Displayer library (part of standard transform set)

Other Information
=================

PrintData is part of the pyXSD standard release

-------------------

Standard Call
=============

PrintData(fileName=None)

Arguments (detailed)
====================

fileName=None : string
    The fileName to use for the output. If not specified, the xml will be sent to stdout.

Description
===========

What It Does
------------
Sends the tree to writer. Uses stdout if `fileName` is not specified. Opens/creates 
the file if it is not.

What it Returns
---------------
The tree just as it was.
