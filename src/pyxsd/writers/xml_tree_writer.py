"""XmlTreeWriter API.

The XmlTreeWriter class and the connected XmlTagWriter class will
write a standard xml tree given a standard set of variables.
XmlTreeWriter must be passed a root element, which is the highest
level element in an xml tree. This element must contain in its
dictionary the following variables:

``_name_`` : String

    A string that is the name of the element. The name will appear as
    the first word after the '<' symbol. If the name is set to
    ``_comment_``, the element will be treated as a comment. In strict
    namespace mode (see ``ParseModes.NAMESPACED``) the name is a Clark
    name (``{uri}local``) and is written as a prefixed name.

``_attribs_`` : Dictionary

    A dictionary with the keys as the names as you will want them to
    appear in the document, and the values of the dictionary should be
    the attribute values. The values will have the ``str()`` function
    called on them, so you should make sure that the value will be
    formatted in the correct way. In strict namespace mode keys may be
    Clark names and are written as prefixed names.

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


def _clark_uri(name: str) -> str | None:
    """Returns the namespace URI of a Clark name, or ``None``."""
    if isinstance(name, str) and name.startswith("{"):
        return name[1:].split("}", 1)[0]
    return None


def _display_name(name: str, prefix_map: dict[str, str]) -> str:
    """Renders a Clark name as ``prefix:local`` using ``prefix_map``.

    Names that carry no namespace (or whose namespace has no assigned
    prefix) are returned as their local spelling.
    """
    if isinstance(name, str) and name.startswith("{"):
        uri, local = name[1:].split("}", 1)
        prefix = prefix_map.get(uri)
        return f"{prefix}:{local}" if prefix else local
    return name


class XmlTreeWriter:
    def __init__(self, root: Any, output: IO[str], namespaces: bool | None = None):
        """Initialize the writer.

        - ``root``: the root instance of a tree. Must be formatted in
          the program's tree structure.

        - ``output``: the file object to write the tree to.

        - ``namespaces``: force (or suppress) namespace-aware output.
          When ``None`` the writer infers it: a tree whose element
          names are Clark names (strict namespace mode) is written
          with generated ``xmlns`` declarations and prefixed names;
          otherwise the historical local-name output is produced
          byte-for-byte and existing behavior is unchanged.
        """
        self.output = output
        self.namespaced = _tree_is_namespaced(root) if namespaces is None else namespaces
        self.prefix_map: dict[str, str] = {}
        root_decls: dict[str, str] = {}

        if self.namespaced:
            self.prefix_map = XmlTreeWriter._build_prefix_map(root)
            root_decls = {f"xmlns:{prefix}": uri for uri, prefix in self.prefix_map.items()}
        else:
            rootAttribs = {xsi.xsi_attr_key(key): value for key, value in root._attribs_.items()}
            if "xmlns:xsi" not in rootAttribs and XmlTreeWriter._tree_uses_xsi(root):
                root._attribs_ = {**root._attribs_, "xmlns:xsi": xsi.XSI_NAMESPACE}

        self.writeHeaderInfo()

        XmlTreeWriter.passTagToTagWriter(
            root,
            0,
            self.output,
            self.prefix_map if self.namespaced else None,
            root_decls if self.namespaced else None,
        )

    @staticmethod
    def _build_prefix_map(root: Any) -> dict[str, str]:
        """Assigns a deterministic prefix to every namespace in the tree.

        ``xsi`` is reserved for the XML Schema instance namespace.
        Remaining namespaces are numbered ``ns0``, ``ns1``, ... in
        first-encounter order.
        """
        uris: list[str] = []

        def visit(element: Any) -> None:
            uri = _clark_uri(getattr(element, "_name_", ""))
            if uri and uri not in uris:
                uris.append(uri)
            for key in getattr(element, "_attribs_", {}):
                uri = _clark_uri(xsi.xsi_attr_key(key))
                if uri and uri not in uris:
                    uris.append(uri)
            for child in getattr(element, "_children_", []):
                visit(child)

        visit(root)

        prefix_map: dict[str, str] = {}
        if XmlTreeWriter._tree_uses_xsi(root):
            prefix_map[xsi.XSI_NAMESPACE] = "xsi"
        index = 0
        for uri in uris:
            if uri in prefix_map:
                continue
            prefix_map[uri] = f"ns{index}"
            index += 1
        return prefix_map

    @staticmethod
    def _tree_uses_xsi(element: Any) -> bool:
        """Returns True when any element in the tree carries an
        XSI-namespace attribute (``xsi:nil``, ``xsi:type``, ...)."""
        if any(xsi.xsi_attr_key(key).startswith("xsi:") for key in element._attribs_):
            return True
        return any(XmlTreeWriter._tree_uses_xsi(child) for child in element._children_)

    @staticmethod
    def passTagToTagWriter(
        element: Any,
        tabs: int,
        output: IO[str],
        prefix_map: dict[str, str] | None = None,
        root_decls: dict[str, str] | None = None,
    ) -> None:
        """Extracts element variables and initializes the tag writer for
        the element.

        Recursively calls itself on the element's children. Writes the
        ending tag if it has any values or any children.

        - ``element``: an element instance that follows the program's
          tree structure.

        - ``tabs``: an integer that specifies how many tabs precede an
          element. Starts at zero for the root element.

        - ``output``: the file object to write the tree to.

        - ``prefix_map``: namespace URI to prefix mapping, or ``None``
          for the historical local-name output.

        - ``root_decls``: ``xmlns`` declarations to place on the root
          element (strict namespace mode only).
        """
        name = element._name_
        if prefix_map is not None:
            name = _display_name(name, prefix_map)
        children = element._children_
        attribs: dict[str, str] = {}
        for key, value in element._attribs_.items():
            display = xsi.xsi_attr_key(key)
            if prefix_map is not None:
                display = _display_name(display, prefix_map)
            attribs[display] = value

        if tabs == 0:
            if root_decls:
                attribs = {**root_decls, **attribs}
            elif (
                prefix_map is None
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
            XmlTreeWriter.passTagToTagWriter(child, tabs, output, prefix_map, None)

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


def _tree_is_namespaced(root: Any) -> bool:
    """Returns True when any element name in the tree is a Clark name.

    Element names are local in legacy mode even for documents that use
    namespaces, so this only fires for strict namespace mode. Clark
    *attributes* do not count: legacy mode has always keyed XSI and
    namespaced attributes that way without implying namespaced output.
    """
    name = getattr(root, "_name_", "")
    if isinstance(name, str) and name.startswith("{"):
        return True
    return any(_tree_is_namespaced(child) for child in getattr(root, "_children_", []))
