=========
Overview:
=========

- Creates python classes for all types defined in an XSD schema file (xml)
- Reads in a xml file and builds a new pythonic tree according to classes. This tree of instances maintains the same overall structure of the original xml document.
- Provides some xml/schema parsing with non-fatal errors in order to help the user write a valid xml document, without requiring it
- Transforms the pythonic reprsentation according to built-in and add-on "transform" classes that the user specifies
- Sends data to a writer to write the pythonic tree back into an xml file

=========
Features:
=========

- Transforms allow users to easily adapt pyXSD to vast number of applications
	    + Provides a framework and libraries to write transform so the user can more easily write these transform functions
	    + Allows the user to specify the desired transform classes with arguments and the order in a file so the user can create a sort of custom tool
	    + Allows for transforms that can export to other formats, giving pyXSD powerful flexibility
- The pythonic data tree format uses a very simple structure that allows for an easily understood API, so that users can easily manipute this tree in transforms and use the writer in other programs
- Can be used as a standalone program at the command line or as a library in other programs
- uses the cElementTree library for fast reading of the xml and xsd files

