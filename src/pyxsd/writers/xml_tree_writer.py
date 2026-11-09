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
from pyxsd.data_conversion.values import selected_value
from pyxsd.namespaces import XML_NS
from pyxsd.writers.xml_tag_writer import XmlTagWriter, _escape_text
from pyxsd.xsd_data_types import QName, XsdDataType, XsdList, qname_context


def _clark_uri(name: str | None) -> str | None:
    """Returns the namespace URI of a Clark name, or ``None``."""
    if isinstance(name, str) and name.startswith("{"):
        return name[1:].split("}", 1)[0]
    return None


def _actual_type_name(element: Any) -> str | None:
    """The type QName's original resolution, never a guess from the bound class.

    Retain redundant and invalid markers too. A caller's later lexical edit
    must not be overwritten with the previous type's namespace evidence.
    """
    evidence = getattr(element, "_xsi_type_context_", None)
    if evidence is None:
        return None
    raw = element._attribs_.get("xsi:type", element._attribs_.get(xsi.XSI_TYPE))
    if " ".join(str(raw).split()) != evidence[0]:
        return None
    try:
        with qname_context(evidence[1]):
            return QName(evidence[0]).to_expanded_name(strict=True)
    except (TypeError, ValueError):
        return None


def _resolved_qname_uri(value: Any) -> str | None:
    """The namespace URI a resolved QName value carries, or ``None``."""
    value = selected_value(value)
    if isinstance(value, QName) and value._resolved_ and value._uri_:
        return value._uri_
    return None


def _qname_value_uris(value: Any) -> list[str]:
    """Namespace URIs used by resolved QName values inside *value*.

    A simple-content element carries its typed value on the instance
    itself (a QName subclass); an ``xs:list`` value carries it on its
    items. Values built without a resolution context never contribute:
    their namespace is unknown, not merely undeclared.
    """
    uris: list[str] = []
    value = selected_value(value)
    uri = _resolved_qname_uri(value)
    if uri is not None:
        uris.append(uri)
    if isinstance(value, XsdList):
        for item in value:
            uris.extend(_qname_value_uris(item))
    return uris


def _respelled_qname(value: Any, prefix_map: dict[str, str]) -> str | None:
    """A writer-prefixed spelling for a resolved QName, or ``None``."""
    value = selected_value(value)
    uri = _resolved_qname_uri(value)
    if uri is None:
        return None
    prefix = prefix_map.get(uri)
    if prefix is None:
        return None
    return f"{prefix}:{value._local_}"


def _respelled_lexical(value: Any, prefix_map: dict[str, str]) -> str | None:
    """A writer-prefixed spelling for a QName or QName-list value.

    Returns ``None`` when *value* holds no resolved QName, so callers
    keep the recorded lexical content. An ``xs:list`` re-spells its
    resolved items and keeps unresolved ones verbatim.
    """
    original = value
    value = selected_value(value)
    spelled = _respelled_qname(value, prefix_map)
    if spelled is not None:
        return spelled
    if isinstance(value, XsdList):
        parts: list[str] = []
        respelled = False
        for item in value:
            item_spelling = _respelled_lexical(item, prefix_map)
            if item_spelling is None:
                parts.append(str(item))
            else:
                parts.append(item_spelling)
                respelled = True
        if respelled:
            return " ".join(parts)
    if value is not original and isinstance(value, XsdDataType):
        return value.lexical()
    return None


def _typed_attribute(element: Any, key: str) -> Any:
    """The typed value stored for one declared attribute, or ``None``.

    ``_attribs_`` holds lexical strings; the declaration's converted
    value (a resolved QName for ``xs:QName`` uses) is stored under the
    descriptor's storage key. Wildcard and XSI attributes have no
    declaration and return ``None``.
    """
    getter = getattr(element, "descAttributes", None)
    if not callable(getter):
        return None
    for storage, descriptor in getter().items():
        if key == storage or key == getattr(descriptor, "expandedName", None):
            return element.__dict__.get(storage)
    return None


def _display_content(element: Any, value: Any, prefix_map: dict[str, str]) -> Any:
    """Re-spell an element's QName content with the writer's prefixes.

    The recorded ``_value_`` holds the input's lexical spelling, whose
    prefixes the output may not declare (a value-only namespace) or may
    reuse for a different namespace. A resolved QName element or
    ``xs:list`` is rewritten with the writer's prefixes; unresolved
    values, plain strings, and multi-entry mixed values pass through
    unchanged.
    """
    if value is None:
        return None
    if isinstance(value, list) and len(value) != 1:
        return value
    if getattr(type(element), "_unionMembers", None):
        recorded = str(value[0]) if isinstance(value, list) else str(value)
        if recorded != str(element):
            # A caller edited the lexical container. Do not replace that edit
            # with the old bound member and thereby hide validation failures.
            return value
    spelled = _respelled_lexical(element, prefix_map)
    if spelled is None:
        return value
    return [spelled] if isinstance(value, list) else spelled


def _display_attribute_value(typed: Any, raw: Any, prefix_map: dict[str, str]) -> Any:
    """Re-spell a QName attribute value with the writer's prefixes."""
    if typed is None:
        return raw
    if getattr(type(typed), "_unionMembers", None) and str(raw) != str(typed):
        return raw
    spelled = _respelled_lexical(typed, prefix_map)
    return spelled if spelled is not None else raw


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
            # Namespaces used only by QName values must be declared in
            # the output too, or the value's prefix would resolve
            # differently (or not at all) when the output is reparsed.
            for value_uri in _qname_value_uris(element):
                if value_uri not in uris:
                    uris.append(value_uri)
            for key, _raw in getattr(element, "_attribs_", {}).items():
                uri = _clark_uri(xsi.xsi_attr_key(key))
                if uri and uri not in uris:
                    uris.append(uri)
                if xsi.xsi_attr_key(key) == "xsi:type":
                    type_uri = _clark_uri(_actual_type_name(element))
                    if type_uri and type_uri not in uris:
                        uris.append(type_uri)
                for value_uri in _qname_value_uris(_typed_attribute(element, key)):
                    if value_uri not in uris:
                        uris.append(value_uri)
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
            if prefix_map is not None:
                actual_type = (
                    _actual_type_name(element) if xsi.xsi_attr_key(key) == "xsi:type" else None
                )
                if actual_type is not None:
                    value = _display_name(actual_type, prefix_map)
                else:
                    value = _display_attribute_value(
                        _typed_attribute(element, key), value, prefix_map
                    )
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
        value = _display_content(element, value, prefix_map or {})
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
    """Whether names or resolved values require namespace-aware output.

    Value-only namespaces count even in an otherwise unqualified tree.
    Ordinary Clark attributes count too; the conventional XSI keys alone
    need only the historical implicit ``xsi`` declaration.
    """
    name = getattr(root, "_name_", "")
    if isinstance(name, str) and name.startswith("{"):
        return True
    if _qname_value_uris(root):
        return True
    for key in getattr(root, "_attribs_", {}):
        if _clark_uri(key) and not key.startswith("{" + xsi.XSI_NAMESPACE + "}"):
            return True
        if _qname_value_uris(_typed_attribute(root, key)):
            return True
        if xsi.xsi_attr_key(key) == "xsi:type" and _clark_uri(_actual_type_name(root)):
            return True
    return any(_tree_is_namespaced(child) for child in getattr(root, "_children_", []))
