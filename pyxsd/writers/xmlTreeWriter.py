from xmlTagWriter import XmlTagWriter
import time

"""
XmlTreeWriter API 
=====================================
*API found in writers/xmlTreeWriter.py*

:Author: Kali Norby <kali.norby@gmail.com>

**Basic API for the writer. Use the class itself as a straight foreward xml tree writer, 
or use this api as reference for transforms.**

The XmlTreeWriter class and the connected XmlTagWriter class will write a
standard xml tree given a standard set of variables. XmlTreeWriter must be
passed a root element, which is the highest level element in an xml tree.
This element must contain in its dictionary the following variables:
    
*_name_* : String 
    A string that is the name of the element. The name will appear in as the first word after the '<' symbol. If the name is set to `_comment_`, the element will be treated as a comment.
     
*_attribs_* : Dictionary
    A dictionary with the keys as the names as you will want them to appear in the document, and the values of the dictionary should be the attributes values. The values will have the str() function called on them, so you should make sure that the value will be formatted in the correct way.

*_children_* : List
    A list with the child elements (of this same type). This should be a blank list if there are no child elements.

*_value_* : List or NoneType
    either a list or None. The values should be in a from that will easily be converted to a string. The value for a given element should be its non-element child. Each value will be an entry in the list. These values are commonly numbers. 


To start writing, instantiate the class as follows:

     XmlTreeWriter(root, outputFile)

Variable Quick Reference:
-------------------------
+------------+-----------------+
|*_name_*    |String           |
+------------+-----------------+
|*_children_*|List             |
+------------+-----------------+
|*_attribs_* |Dictionary       |
+------------+-----------------+
|*_values_*  |List or NoneType |
+------------+-----------------+
"""


class XmlTreeWriter(object):

    def __init__(self, root, output):
        """
        Initialize the writer.

        Parameters:

        - `root`: The root instance of a tree. Must be formatted in program's tree structure.
        - `output`: The file object to write the tree to.

        """

        self.output = output

        self.writeHeaderInfo()

        XmlTreeWriter.passTagToTagWriter(root, 0, self.output)

    def passTagToTagWriter(element, tabs, output):
        """
        Extracts element variables and initializes the tag writer for the element.
        Recursively calls itself on its element children. Writes the ending tag if it has any values or any children.        

        Parameters:
        - `element`: An element instance that follows the program's tree structure.
        - `tabs`: An integer that specifies how many tabs preceed an element. Starts at zero for the root element.
        - `output`: The file object to write the tree to.
        """

        name = element._name_

        children = element._children_

        attribs = element._attribs_

        value = element._value_

        hasChildren = len(children) > 0

        if len(children) == 1:
            if children[0]._name_ == '_comment_':
                hasChildren = False

        hasValue = (not value == None)

        tagWriter = XmlTagWriter(name, attribs, value,
                                 hasChildren, hasValue, tabs, output)

        tabs += 1

        for child in children:

            XmlTreeWriter.passTagToTagWriter(child, tabs, output)

        if hasChildren:

            tagWriter.writeEndTag()

    passTagToTagWriter = staticmethod(passTagToTagWriter)

    def writeHeaderInfo(self):
        """
        Writes a comment at the top of the file with the creation information. Includes data and time information.

        Takes no arguments
        """

        print >> self.output, "<!--File created by PyXSD at %s on %s-->" % (
            time.strftime('%X'), time.strftime('%x'))
