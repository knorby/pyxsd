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

from typing import IO, Any

from pyxsd import xsi
from pyxsd.namespaces import XML_NS
from pyxsd.writers.xml_tag_writer import XmlTagWriter, _escape_text


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


def _display_attribute(key: str, prefix_map: dict[str, str] | None) -> str:
    """Renders an attribute key for output.

    XSI-namespace keys get their ``xsi:`` spelling. When no prefix map
    is active (unqualified output) an attribute in the reserved XML
    namespace still has to use the implicit ``xml`` prefix; writing its
    Clark name would not be well-formed XML.
    """
    display = xsi.xsi_attr_key(key)
    if prefix_map is not None:
        return _display_name(display, prefix_map)
    if isinstance(display, str) and display.startswith("{"):
        uri, local = display[1:].split("}", 1)
        if uri == XML_NS:
            return f"xml:{local}"
    return display


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
            # The xml prefix is implicit in every XML document; declaring
            # it would be redundant.
            root_decls = {
                f"xmlns:{prefix}": uri for uri, prefix in self.prefix_map.items() if uri != XML_NS
            }
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

        ``xsi`` is reserved for the XML Schema instance namespace and
        ``xml`` for the reserved XML namespace; both prefixes may not be
        reused. Remaining namespaces are numbered ``ns0``, ``ns1``, ...
        in first-encounter order.
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

        prefix_map: dict[str, str] = {XML_NS: "xml"}
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
        *,
        inline: bool = False,
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

        - ``inline``: write the element inside mixed content: no
          indentation precedes it and no newline follows its end tag,
          since that layout whitespace would become character data. A
          mixed element writes its children this way.
        """
        name = element._name_
        if prefix_map is not None:
            name = _display_name(name, prefix_map)
        children = element._children_
        attribs: dict[str, str] = {}
        for key, value in element._attribs_.items():
            attribs[_display_attribute(key, prefix_map)] = value

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

        if bool(children) and getattr(element, "_elementOnly_", True) is False:
            XmlTreeWriter._writeMixedElement(
                name, attribs, value, children, tabs, output, prefix_map, inline
            )
            return

        tagWriter = XmlTagWriter(
            name, attribs, value, hasChildren, hasValue, tabs, output, inline=inline
        )
        tabs += 1

        for child in children:
            XmlTreeWriter.passTagToTagWriter(child, tabs, output, prefix_map, None)

        if hasChildren:
            tagWriter.writeEndTag()

    @staticmethod
    def _writeMixedElement(
        name: str,
        attribs: dict[str, str],
        value: Any,
        children: list[Any],
        tabs: int,
        output: IO[str],
        prefix_map: dict[str, str] | None,
        inline: bool,
    ) -> None:
        """Writes a mixed-content element without layout whitespace.

        The element's character data is significant, so nothing the
        writer generates may land inside it: the open tag is closed
        without a newline, the bound leading text follows immediately,
        and each child starts immediately after the preceding text with
        no indentation or trailing newline of its own, followed by its
        escaped ``_tail_`` when the source had one. An element-only
        child's interior block layout is unchanged: that whitespace is
        insignificant inside the child. The element's own end tag
        carries no indentation either.

        - ``name``: the element's display-spelled name.
        - ``attribs``: the element's display-spelled attributes.
        - ``value``: the bound leading text, or ``None``.
        - ``children``: the bound child elements.
        - ``tabs``: how many tabs precede the element in block layout.
        - ``output``: the file object to write to.
        - ``prefix_map``: namespace URI to prefix mapping, or ``None``.
        - ``inline``: True when the element itself is written inside
          mixed content, so its indentation and trailing newline belong
          to the parent's layout.
        """
        tagWriter = XmlTagWriter(
            name,
            attribs,
            value,
            True,
            value is not None,
            tabs,
            output,
            inline=inline,
            mixed=True,
        )
        if value is not None:
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                output.write(_escape_text(entry))
        for child in children:
            XmlTreeWriter.passTagToTagWriter(child, tabs + 1, output, prefix_map, None, inline=True)
            tail = getattr(child, "_tail_", None)
            if tail is not None:
                output.write(_escape_text(tail))
        tagWriter.writeEndTag()

    def writeHeaderInfo(self) -> None:
        """Hook called before the root element is written.

        The default writes nothing so that serialization is
        deterministic: writing the same tree twice must produce the
        same bytes, and a generated timestamp would break that.
        Subclasses may override this method to emit their own header.
        """
        return None


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
