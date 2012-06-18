=============
README: pyXSD
=============

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Fri, 8 Sept 2006
:Web Site: http://pyxsd.org/
:Copyright: pyXSD License. SEE LICENSE
:Version: 0.1

.. contents::

Introduction
============

PyXSD was developed in order to map XML and the related schema (XSD) files
into the programming language Python. The program builds a Pythonic 
representation of the XML tree according to the specifications in the schema 
and raises non-fatal parser errors whenever possible in order to help 
the user validate their XML document. The program allows the user to specify 
*transform* classes, which manipulate and transform the XML tree in various 
ways. The program then writes the tree back out into XML. This program was 
written in order to replace many of the old tools that were written in FORTRAN 
with the more modern XML format and the more modern and powerful Python 
programming language. PyXSD allows users to create their own transform classes 
with the help of a transform library. These classes are fairly simple to write, 
making the system highly adaptable to very specific uses, as one might find 
in many scientific applications; however, the program has potential uses in 
other fields, since XML is widely used. The program allows the user to specify 
the desired transform classes, along with their arguments and sequence of 
application, so the user can create customised tools. The program can be used 
either as a standalone command line program or as a library in other programs.

Documentation
=============

Documentation can be found on the pyXSD website at http://pyxsd.org/documentation.
Documentation is also located in the doc/ directory and the examples/ directory
of the installation archive. 

Requirements
============

pyXSD requires Python_ 2.3 or later and the ElementTree_ Library. The cElementTree
library is recommended. Python 2.5 users already have ElementTree and cElementTree
installed, and pyXSD is configured to use the versions in the standard library. 

.. _Python: http://python.org
.. _ElementTree: http://effbot.org/zone/element-index.htm

Installation
============

Unix, Linux, Mac OS X, .... (All Platforms)
-------------------------------------------

If you need root permission on your system to install packages, 
make sure that you have them before you install. From there, follow these steps:

1. Unpack the archieve
2. In the directory you expanded the archieve tool, run::

    python setup.py install

Windows
-------

Run the exe installer, and it should take care of the rest. The self-installer does
not check to see if you have ElementTree installed, so you may encounter errors when
attempting to run pyXSD if you do not. If you do not want to
use the graphical installer, you can use the instuctions for unix OSes to install
on your machine as well.  

Using pyXSD
===========

pyXSD can be used a library or as a command-line program. 

Command-line
------------

To run pyXSD from the command-line, use the script called 'pyXSD.py.' On UNIX-based
systems, this script should be in */usr/local/bin/*, but it may be in another location.
On windows systems, it should be in the scripts directory of the python installation.

Library
-------

All pyXSD packages are in the directory *pyxsd* in *site-packages*. To import any module
or sub-package, you must use an import that starts like the following::

    import pyxsd

If you want to import the pyXSD class, which you will need to use for almost any use of
the program, use this::

    from pyxsd.pyXSD import PyXSD

That imports the class PyXSD. This statement does not require that you have any prefix on
the class name. Another possible import is the writer::

    from pyxsd.writers.xmlTreeWriter import XmlTreeWriter

For anything else, you will have to figure it out yourself. For any use of pyXSD as a 
library, you should consult the API documentation in the reference manual. It is found
at http://kanorben.net/pyXSD/epydoc/. It is assumed that you have some knowledge of python
if you are using pyXSD as a library, so the documentation is not as extensive for this use.
  

Usage
=====

usage: ./pyXSD.py [options] arg

options:
  --version             show program's version number and exit
  -h, --help            show this help message and exit
  -iINPUTXMLFILE, --inputXml=INPUTXMLFILE
                        filename for the xml file to read in. Reads from stdin
                        by default.
  -sINPUTXSDFILE, --inputXsd=INPUTXSDFILE, --schema=INPUTXSDFILE
                        filename for the xsd (schema) file to read in. Trys to
                        determine location from the input xml file by default.
  -pPARSEDOUTPUTFILE, --parsedXml=PARSEDOUTPUTFILE, --parsedOutput=PARSEDOUTPUTFILE
                        filename for the xml file that contains the parsed
                        output of the xml file, which contains no further
                        transformation. By default, the filename is the xml
                        input filename followed by 'Parsed.'
  -k, --ParsedFile      outputs a parsed version of the xml file without
                        transform. Use for debugging. Off by default. If no
                        filename is specified, it will be determined from the
                        xml filename.
  -oTRANSFORMOUTPUTFILE, --transformOutput=TRANSFORMOUTPUTFILE
                        filename for the output after the xml has been parsed
                        and transformed. Output is sent to stdout by default.
                        Any specified filename will override this option.
  -d, --useDefaultFile  Uses the default filename for transformed output. If not
                        specified and no filename is specified, uses stdout
  -tTRANSFORMCALL, --transform=TRANSFORMCALL
                        the transform class with args. See the documentation for
                        syntax and further information.
  -TTRANSFORMFILE, --transformFile=TRANSFORMFILE
                        file with transform class calls. See the documentation
                        for information on the this function
  -cCLASSFILE, --overlayClassesFile=CLASSFILE
                        Experimental. Allows for user defined schemas to
                        override and add to the types defined in the schema
                        file. See the documentation for information on the this
                        function
  -v, --verbose         uses the verbose mode. Experts Only. (limited
                        functionality)
  -q, --quiet           uses the quiet mode. Few errors reported. (limited
                        functionalily)

