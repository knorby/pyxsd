"""XmlTreeWriter API.

The XmlTreeWriter class and the connected XmlTagWriter class will
write a standard xml tree given a standard set of variables.
XmlTreeWriter must be passed a root element, which is the highest
level element in an xml tree. This element must contain in its
dictionary the following variables:

``_name_`` : String

    A string that is the name of the element. The name will appear as
    the first word after the '<' symbol. If the name is set to
    ``_comment_``, the element will be treated as a comment.

``_attribs_`` : Dictionary

    A dictionary with the keys as the names as you will want them to
    appear in the document, and the values of the dictionary should be
    the attribute values. The values will have the ``str()`` function
    called on them, so you should make sure that the value will be
    formatted in the correct way.

``_children_`` : List

    A list with the child elements (of this same type). This should be
    a blank list if there are no child elements.

``_value_`` : List or NoneType

    Either a list or None. The values should be in a form that will
    easily be converted to a string. The value for a given element
    should be its non-element child. Each value will be an entry in the
    list. These values are commonly numbers.
"""

import time
from typing import IO, Any

from pyxsd import xsi
from pyxsd.writers.xml_tag_writer import XmlTagWriter


class XmlTreeWriter:
    def __init__(self, root: Any, output: IO[str]):
        """Initialize the writer.

        - ``root``: the root instance of a tree. Must be formatted in
          the program's tree structure.

        - ``output``: the file object to write the tree to.
        """
        self.output = output

        self.writeHeaderInfo()

        rootAttribs = {xsi.xsi_attr_key(key): value for key, value in root._attribs_.items()}
        if "xmlns:xsi" not in rootAttribs and XmlTreeWriter._tree_uses_xsi(root):
            root._attribs_ = {**root._attribs_, "xmlns:xsi": xsi.XSI_NAMESPACE}

        XmlTreeWriter.passTagToTagWriter(root, 0, self.output)

    @staticmethod
    def _tree_uses_xsi(element: Any) -> bool:
        """Returns True when any element in the tree carries an
        XSI-namespace attribute (``xsi:nil``, ``xsi:type``, ...)."""
        if any(xsi.xsi_attr_key(key).startswith("xsi:") for key in element._attribs_):
            return True
        return any(XmlTreeWriter._tree_uses_xsi(child) for child in element._children_)

    @staticmethod
    def passTagToTagWriter(element: Any, tabs: int, output: IO[str]) -> None:
        """Extracts element variables and initializes the tag writer for
        the element.

        Recursively calls itself on the element's children. Writes the
        ending tag if it has any values or any children.

        - ``element``: an element instance that follows the program's
          tree structure.

        - ``tabs``: an integer that specifies how many tabs precede an
          element. Starts at zero for the root element.

        - ``output``: the file object to write the tree to.
        """
        name = element._name_
        children = element._children_
        attribs = {xsi.xsi_attr_key(key): value for key, value in element._attribs_.items()}
        if (
            tabs == 0
            and "xmlns:xsi" not in attribs
            and any(key.startswith("xsi:") for key in attribs)
        ):
            # The tree carries XSI-namespace attributes (xsi:nil,
            # xsi:type, ...); the root must declare their namespace so
            # the output parses as xml.
            attribs["xmlns:xsi"] = xsi.XSI_NAMESPACE
        value = element._value_
        hasChildren = bool(children)

        if len(children) == 1 and children[0]._name_ == "_comment_":
            hasChildren = False

        hasValue = value is not None
        tagWriter = XmlTagWriter(name, attribs, value, hasChildren, hasValue, tabs, output)
        tabs += 1

        for child in children:
            XmlTreeWriter.passTagToTagWriter(child, tabs, output)

        if hasChildren:
            tagWriter.writeEndTag()

    def writeHeaderInfo(self) -> None:
        """Writes a comment at the top of the file with the creation
        information. Includes date and time information.
        """
        self.output.write(
            "<!--File created by PyXSD at {} on {}-->\n".format(
                time.strftime("%X"), time.strftime("%x")
            )
        )
