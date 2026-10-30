"""Writer determinism tests: default serialization is byte-stable.

The historical writer stamped every document with a creation-time
comment, so writing the same bound tree twice produced different text.
Issue #48 removes that default while keeping ``writeHeaderInfo`` as an
overridable hook for subclasses that want their own header.
"""

import io

from pyxsd.schema import Schema
from pyxsd.writers import XmlTreeWriter

SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root" type="xs:string"/>
</xs:schema>
"""


def parse_document():
    """Compile the schema and parse a small bound document."""
    return Schema.compile(io.StringIO(SCHEMA)).parse(io.StringIO("<root>hi</root>"))


def write_tree(root) -> str:
    """Serialize a bound root with the default writer."""
    output = io.StringIO()
    XmlTreeWriter(root, output)
    return output.getvalue()


class _CustomHeaderWriter(XmlTreeWriter):
    """A writer that deliberately adds its own header."""

    def writeHeaderInfo(self) -> None:
        self.output.write("<!--custom-->\n")


def test_default_writer_has_no_generated_header():
    """Default output starts with the root element, not a timestamp comment."""
    output = write_tree(parse_document().root)

    assert "File created by PyXSD" not in output
    assert output.startswith("<root>hi</root>")


def test_repeated_document_writes_are_identical():
    """Writing the same bound document twice produces identical text."""
    document = parse_document()

    first = document.to_string()
    second = document.to_string()

    assert first == second
    assert "File created by PyXSD" not in first


def test_custom_header_hook_is_preserved():
    """A subclass header hook still writes exactly one custom header."""
    output = io.StringIO()
    _CustomHeaderWriter(parse_document().root, output)
    text = output.getvalue()

    assert text.startswith("<!--custom-->\n<root>hi</root>")
    assert text.count("<!--custom-->") == 1
