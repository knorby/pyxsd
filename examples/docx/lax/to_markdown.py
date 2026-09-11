"""Convert a simplified WordprocessingML document to Markdown.

Run from this directory so pyxsd can find the module:

    uv run pyxsd -i instance.xml -s schema.xsd --mode lax \
        -k -o /dev/null -t "ToMarkdown('document.md')"

The transform recognizes the paragraph styles ``Heading1``-``Heading3``
and ``ListBullet`` and the run properties ``b`` and ``i``. It writes the
Markdown itself (and returns ``None``), so pyxsd does not write an XML
tree afterwards.
"""

import sys

from pyxsd.transforms import Displayer

HEADING_PREFIX = {"Heading1": "#", "Heading2": "##", "Heading3": "###"}


class ToMarkdown(Displayer):
    """Renders ``p``/``r``/``t`` content as Markdown."""

    def __init__(self, root):
        super().__init__(root)

    def runText(self, run):
        """The text of one run, wrapped for bold/italic properties."""
        textNode = self.find("t", run)
        text = str(textNode) if textNode is not None else ""
        rPr = self.find("rPr", run)
        if rPr is not None:
            if self.find("b", rPr) is not None:
                text = f"**{text}**"
            if self.find("i", rPr) is not None:
                text = f"*{text}*"
        return text

    def paragraphText(self, paragraph):
        """One paragraph rendered as a Markdown line."""
        style = None
        pPr = self.find("pPr", paragraph)
        if pPr is not None:
            pStyle = self.find("pStyle", pPr)
            if pStyle is not None:
                style = str(pStyle)

        text = "".join(self.runText(child) for child in paragraph._children_ if child._name_ == "r")
        if style in HEADING_PREFIX:
            return f"{HEADING_PREFIX[style]} {text}"
        if style == "ListBullet":
            return f"- {text}"
        return text

    def toMarkdown(self):
        body = self.find("body", self.root)
        if body is None:
            return ""
        paragraphs = [child for child in body._children_ if child._name_ == "p"]
        return "\n".join(self.paragraphText(p) for p in paragraphs)

    def __call__(self, fileName=None):
        markdown = self.toMarkdown()
        output = self.openFile(fileName)
        try:
            output.write(markdown)
            if markdown and not markdown.endswith("\n"):
                output.write("\n")
        finally:
            if output is not sys.stdout:
                output.close()
        return None
