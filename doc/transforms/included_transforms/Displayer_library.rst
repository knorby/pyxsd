============================
Transform Library: Displayer
============================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Standard Transform Tools
:Description: Library containing functions to help print data
:Copyright: pyXSD License

.. contents:

------------------

Dependencies
============

None

Other Information
=================

Displayer is part of the pyXSD standard release

-------------------

Functions
=========

openFile(fileName=None)
+++++++++++++++++++++++

 **Args**: 
 
 fileName : String
     fileName to open. By default, it is None which will cause it to open stdout, or if the sting reads "stdout"

 **Description**: Opens a file, or returns stdout

writeTree(file)
+++++++++++++++

 **Args**: 
 
 file : file object
     file to write to

 **Description**: Sends the tree to the writer

makeTempFileOfTree()
++++++++++++++++++++

 **Args**: None
 
 **Description**: Writes out the xml of the tree to a temp file, and returns the temp file.
