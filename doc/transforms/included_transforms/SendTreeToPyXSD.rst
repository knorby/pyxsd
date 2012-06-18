==========================
Transform: SendTreeToPyXSD
==========================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 1 Sept 2006
:Category: Standard Transform Tools
:Description: Sends the generated XML back into pyXSD
:Copyright: pyXSD License

.. contents::

------------------

Dependencies
============

- Displayer library (part of standard transform set)

Other Information
=================

SendTreeToPyXSD is part of the pyXSD standard release

-------------------

Standard Call
=============

SendTreeToPyXSD(xsdFile=None, xmlFileOutput='_No_Output_', transformOutputName=None, transforms=[], transformFile=None, classFile=None, verbose = False, quiet=False)

Arguments (detailed)
====================

**All arguments should be made as keyword arguments to this transform!**

xsdFile=None : string or None
    The Schema file. The default is None, which would result in the program attempting to locate the schema from the `schemaLocation` tag in the xml.
xmlFileOutput='_No_Output_' : String or Boolean
    The filename for the xml output that has not been sent through transforms. The default "_No_Output_" will cause it to not write. If set to False, it will write to the default name, "tempFileParsed.xml"
transformOutputName=None : String or None
    The filename for the xml output that has been sent through the transforms. By default, it is None, which will write to the default name, "tempFileTransformed.xml"
transforms=[] : List of Strings
    The list of transform calls to make in order. Each must be a string with the call. By default, it is a blank list.
transformFile=None : transformFile


Description
===========

What It Does
------------
Runs pyXSD on the tree of data. It writes out the xml, and puts it in a temp file.
The temp file is used as the xml input. All arguments should be keyword. With the exception
of the xml input, most of the variables are exactly the same. There is also a variable
for a transform call file. The main purpose of this transform is to check schema validity
on output, but it can do anything that pyXSD can do to an xml file.

What it Returns
---------------
The tree just as it was.
