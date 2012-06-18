==============
PyXSD 0.1 TODO
==============

:Authors: Kali Norby, Mike Summers, Paul Kent
:Last to Update: Kali Norby <kali.norby@gmail.com>
:Last Update: Fri, 8 Sept 2006

.. contents:


Top Priority:
=============


* Web Site Development
      - Plone Site
	    + Text about the subversion site, access policies, politeness etc.
      - Tracker Site
	    + Mike: setup tracker ticket categories
      - Transform documentation system
            + write some system that will standardize the transform documentation process
                  - Create a web form to create a standard rst file for the documentation
                  - Create a web form to convert rst to html 
* Documentation:
	    + Write a Reference Manual describing all of the coding techniques (terse)
		  - Describe metaprogramming tech.
* Publicize
      - Find the best ways to publicize pyXSD
      - Contact experts in the Python/XML field
      - Go on mailing lists and forums 


Wish List:
==========

* Testing:
      - Make a test suite and assocaiated scripts
      - find a tester, who will download, install, and use the software
* Implement class constuction with a metaclass instead of a class factory
      - Add ability to write generated classes out to a physical file
* write a transform to export a bravais lattice to some sort of visualizer
* Better support for xml/xsd parsing
      - Add more xml/xsd element types to the program
	    + add some of the odd primitive data types
	    + check the ElementRepresentative system to make sure it handles all possible uses of the schema file and correct problems found
		  - add regexp support for the pattern facet by mapping it to the regexp support in python. The two use the same format.
      - compare the programs parser errors to the errors flagged by a more traditional parser, and refine the program to raise more accurate (but also non-fatal) errors
* GUI interface
      - add a GUI program that simplifies the constuction of transform calls
      - GUI frontend to the entire program?
* add support for more of the formats from the xml realm
      - support for dtd?
	    + possibily by converting to schema, and then parsing?
      - XSL and XSLT integration to transforms
      - XPath
      - Support new implementations for the xml web uses
      - etc
* add some sort of web interface to the program?
