from typing import IO, Any


def _escape_text(text: Any) -> str:
    """Escapes characters that are illegal in element text.

    Values are stored decoded; markup characters must be re-encoded
    when they are written back out so the output stays well-formed.
    Carriage returns are written as character references because XML
    end-of-line handling would otherwise fold them into newlines on the
    next read.
    """
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\r", "&#13;")
    )


def _escape_attribute(value: Any) -> str:
    """Escapes characters that are illegal inside a double-quoted
    attribute value.

    In addition to markup characters, newlines and tabs are written as
    character references so the XML attribute-value normalization rules
    cannot change them on a later read. Carriage returns are already
    character-referenced by :func:`_escape_text`.
    """
    escaped = _escape_text(value).replace('"', "&quot;")
    return escaped.replace("\n", "&#10;").replace("\t", "&#9;")


class XmlTagWriter:
    """Writes one element.

    Each tag has its own instance of this class. It contains a
    function to write the end tag if the element has children, but
    this function is called from the tree writer. See XmlTreeWriter
    for the API and other information. This class should only be
    initialized by XmlTreeWriter.

    - ``name``: a string of the name of the tag

    - ``attribs``: a dictionary of the tag attributes

    - ``value``: a list of the element values or None if there are no
      values

    - ``hasChildren``: a boolean to indicate if the tag has children

    - ``hasValue``: a boolean to indicate if the tag has any value

    - ``tabs``: an integer that indicates the number of tabs over the
      element is in the document

    - ``output``: the file object to write to
    """

    def __init__(
        self,
        name: str,
        attribs: dict[str, str],
        value: list[Any] | None,
        hasChildren: bool,
        hasValue: bool,
        tabs: int,
        output: IO[str],
    ):
        self.name = name
        self.attribs = attribs
        self.sortedKeyList = sorted(self.attribs.keys())
        self.value = value
        self.hasValue = hasValue
        self.hasChildren = hasChildren
        self.tabs = tabs
        self.output = output
        self.writeTag()

    def writeTag(self) -> None:
        """Writes the tag. Called from the init function. All its
        non-necessary formatting is standard and is not dependent upon
        specifics of the format of the data.
        """
        self.writeTabs()
        if self.name == "_comment_":
            self.writeComment()
            return None

        self.output.write(f"<{self.name}")
        longestNameLen = 0

        for attrKey in self.sortedKeyList:
            nameLen = len(attrKey)
            if nameLen > longestNameLen:
                longestNameLen = nameLen

        longestNameLen += 1
        for key in self.sortedKeyList:
            value = str(self.attribs[key])
            self.output.write("\n")
            self.writeTabs(7)
            self.output.write(key)
            nameLen = len(key)
            spaces = longestNameLen - nameLen
            self.writeTabs(spaces, 0)
            self.output.write(f'= "{_escape_attribute(value)}"')

        if not self.hasChildren and not self.hasValue:
            self.output.write("/>\n")
            return None

        if self.hasValue and not self.hasChildren and self._isSingleTextValue():
            # A text-only element is written inline so that no layout
            # whitespace can be mistaken for part of its value. The end
            # tag is written directly: writeEndTag would indent, and
            # that indentation would land inside the element.
            self.output.write(">")
            single = self.value[0] if isinstance(self.value, list) else self.value
            self.output.write(_escape_text(single))
            self.output.write(f"</{self.name}>\n")
            return None

        self.output.write(">\n")

        if self.hasValue:
            if not isinstance(self.value, list):
                self.value = [self.value]
            for line in self.value:
                self.writeTabs(3)
                self.output.write(f"{_escape_text(line)}\n")
            if not self.hasChildren:
                self.writeEndTag()
        return None

    def _isSingleTextValue(self) -> bool:
        """True when the element's value is one scalar text string.

        Such an element can be written inline. Lists of several values
        (a documented mixed-content shape) keep the historical block
        layout.
        """
        if not isinstance(self.value, list):
            return True
        return len(self.value) == 1 and isinstance(self.value[0], str)

    def writeComment(self) -> None:
        """If ``name`` is set to '_comment_' this function is called.

        A comment can be in the tree only if it is included in a
        transform or the writer is used by another program.
        """
        self.output.write(f"<!--{self.value}-->")

    def writeEndTag(self) -> None:
        """Writes the ending tag for an element.

        Called by the tree writer if the element has children. It is
        called only after the other children have been written.

        An end tag appears as follows::

            </TagName>
        """
        self.writeTabs()
        self.output.write(f"</{self.name}>\n")

    def writeTabs(self, tabSpec: int | None = None, tabs: int | None = None) -> None:
        """Writes out the tabs before an element.

        Can also write a certain number of spaces after the tabs have
        been written, if ``tabSpec`` is specified.

        - ``tabSpec``: an integer. By default, it is a NoneType. If
          specified, the program will write the specified number of
          spaces after the tab it writes.

        - ``tabs``: an integer that indicates the number of tabs over
          the element is in the document. By default, it is set to
          self.tabs, which is the ``tabs`` value provided in the
          initialization.
        """
        if not tabs:
            tabs = self.tabs

        tab = "    " * tabs
        if tabSpec:
            tab += " " * tabSpec

        self.output.write(tab)
