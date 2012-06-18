===================
Xml Tree Writer API 
===================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Wed, 30 Aug 2006
:Description: Basic API for the writer. Use the class itself as a straightforward xml tree writer, or use this api as reference for transforms.

The XmlTreeWriter class and the connected XmlTagWriter class will write a
standard xml tree given a standard set of variables. XmlTreeWriter must be
passed a root element, which is the highest level element in an xml tree.
In every element, this same structure must follow. Note that each variable
name has a underscore on each side. This naming scheme prevents most
conceivable nam conflicts with xml or xsd names. This element must contain
in its dictionary the following variables:
    
*_name_* : String 
    A string that is the name of the element. The name will appear in as the first word after the **<** symbol.
     
*_attribs_* : Dictionary
    A dictionary with the keys as the names as you will want them to appear in the document, and the values of the dictionary should be the attributes values. The values will have the str() function called on them, so you should make sure that the value will be formatted in the correct way.

*_children_* : List
    A list with the child elements (of this same type). This should be a blank list if there are no child elements.

*_value_* : List or NoneType
    either a list or None. The values should be in a from that will easily be converted to a string. The value for a given element should be its non-element child. Each value will be an entry in the list. These values are commonly numbers. 

To start writing, instantiate the class as follows::

     XmlTreeWriter(root, outputFile)

Transform developers should note the makeElemObj() method in the *Transform* class, which requires the name of the element as an argument and return an instance with the above variables without any data in them. 
