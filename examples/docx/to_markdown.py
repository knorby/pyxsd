"""Convert a real WordprocessingML document to Markdown.

This example uses the actual ECMA-376 (Office Open XML) schemas and a
real ``word/document.xml``-shaped instance, parsed in the opt-in
``NAMESPACED`` mode. Fetch the schemas first with::

    uv run python examples/docx/download_schemas.py

Then run, from this directory:

    uv run pyxsd -i document.xml -s schemas/wml.xsd \
        --namespaces strict -k -o /dev/null \
        -t "ToMarkdown('document.md')"

The transform recognizes:

- paragraph styles ``Heading1``-``Heading9`` and the list styles
  ``ListNumber``/``ListBullet`` (with ``numPr`` nesting level),
- tables (rendered as GFM pipe tables),
- hyperlinks (rendered with their relationship id as the target),
- run properties ``b``/``i``/``u``/``color``/``sz``,
- ``w:tab`` and ``w:br`` within a run.

``w:sz`` is in half-points, so ``w:sz w:val="32"`` becomes ``16pt``.
Underline, color, and size have no native Markdown, so they are emitted
as inline HTML; bold and italic use ``**``/``*``.
"""

import contextlib
import sys

from pyxsd.transforms import Displayer

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

HEADING_STYLES = {f"Heading{n}": n for n in range(1, 10)}
LIST_ORDERED = "ListNumber"
LIST_BULLET = "ListBullet"


def local(name):
    """The local part of a possibly Clark-notated element name."""
    return name.split("}")[-1] if name else name


class ToMarkdown(Displayer):
    """Renders a WordprocessingML body as Markdown."""

    def __init__(self, root):
        super().__init__(root)

    # -- tree helpers -------------------------------------------------

    def _children(self, node, name):
        return [child for child in node._children_ if local(child._name_) == name]

    def _child(self, node, name):
        for child in node._children_:
            if local(child._name_) == name:
                return child
        return None

    def _attr(self, node, name):
        """Read an attribute by local name (Clark keys in strict mode)."""
        value = node.__dict__.get(name)
        if value is None:
            try:
                value = getattr(node, name)
            except Exception:
                value = None
        if value is not None:
            return value
        for key, attrValue in node._attribs_.items():
            if key == name or key.endswith("}" + name):
                return attrValue
        return None

    def _text(self, node):
        value = node._value_
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return "".join("" if item is None else str(item) for item in value)
        return str(value)

    def _value(self, node):
        """The ``w:val`` value as a string, or None."""
        value = self._attr(node, "val")
        return None if value is None else str(value)

    # -- inline content ----------------------------------------------

    def _run(self, run):
        """One ``w:r`` as inline Markdown/HTML."""
        text = ""
        for child in run._children_:
            name = local(child._name_)
            if name == "t":
                text += self._text(child)
            elif name == "tab":
                text += "\t"
            elif name == "br":
                text += "<br>"
        rPr = self._child(run, "rPr")
        if rPr is not None:
            if self._child(rPr, "b") is not None:
                text = f"**{text}**"
            if self._child(rPr, "i") is not None:
                text = f"*{text}*"
            if self._child(rPr, "u") is not None:
                text = f"<u>{text}</u>"
            text = self._styled(text, rPr)
        return text

    def _styled(self, text, rPr):
        """Wrap text in the color/size inline HTML the run properties ask for."""
        styles = []
        color = self._child(rPr, "color")
        if color is not None:
            value = self._value(color)
            if value:
                styles.append(f"color:#{value}")
        size = self._child(rPr, "sz")
        if size is not None:
            value = self._value(size)
            if value:
                with contextlib.suppress(ValueError):
                    styles.append(f"font-size:{int(value) / 2:g}pt")
        if styles:
            text = f'<span style="{";".join(styles)}">{text}</span>'
        return text

    def _inline(self, paragraph):
        """The inline content of a paragraph: runs and hyperlinks."""
        parts = []
        for child in paragraph._children_:
            name = local(child._name_)
            if name == "r":
                parts.append(self._run(child))
            elif name == "hyperlink":
                inner = "".join(self._run(run) for run in self._children(child, "r"))
                target = self._attr(child, "id") or self._attr(child, "anchor") or ""
                parts.append(f"[{inner}]({target})")
        return "".join(parts)

    # -- block content -----------------------------------------------

    def _style_name(self, paragraph):
        pPr = self._child(paragraph, "pPr")
        if pPr is None:
            return None
        pStyle = self._child(pPr, "pStyle")
        return None if pStyle is None else self._value(pStyle)

    def _list_level(self, paragraph):
        pPr = self._child(paragraph, "pPr")
        if pPr is None:
            return None
        numPr = self._child(pPr, "numPr")
        if numPr is None:
            return None
        ilvl = self._child(numPr, "ilvl")
        level = 0 if ilvl is None else self._value(ilvl) or 0
        try:
            return int(level)
        except (TypeError, ValueError):
            return 0

    def _paragraph(self, paragraph, counters):
        text = self._inline(paragraph)
        style = self._style_name(paragraph)
        if style in HEADING_STYLES:
            counters.clear()
            return f"{'#' * HEADING_STYLES[style]} {text}"

        level = self._list_level(paragraph)
        if style == LIST_BULLET or (level is not None and style != LIST_ORDERED):
            counters.clear()
            return f"{'  ' * (level or 0)}- {text}"
        if style == LIST_ORDERED or level is not None:
            level = level or 0
            counters[level] = counters.get(level, 0) + 1
            return f"{'  ' * level}{counters[level]}. {text}"

        counters.clear()
        return text

    def _table(self, table):
        rows = []
        for row in self._children(table, "tr"):
            cells = []
            for cell in self._children(row, "tc"):
                paragraphs = [self._inline(p) for p in self._children(cell, "p")]
                cells.append(" ".join(paragraphs).strip())
            rows.append(cells)
        if not rows:
            return ""
        header = rows[0]
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        return "\n".join(lines)

    def toMarkdown(self):
        body = self._child(self.root, "body")
        if body is None:
            return ""
        blocks = []
        counters = {}
        for child in body._children_:
            name = local(child._name_)
            if name == "p":
                blocks.append(self._paragraph(child, counters))
            elif name == "tbl":
                counters.clear()
                blocks.append(self._table(child))
        return "\n".join(blocks)

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
