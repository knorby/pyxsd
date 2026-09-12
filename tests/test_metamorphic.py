"""Metamorphic properties for the repaired binding pipeline.

These are not example-by-example regressions; they assert equivalences
that must hold for every generated input:

* reordering type declarations does not change validation;
* parsing an unrelated document cannot disturb an earlier parser;
* parse -> write -> parse preserves values exactly.
"""

import io
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from pyxsd.parser import PyXSD
from pyxsd.writers import XmlTreeWriter

XSD = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

NAMES = st.lists(
    st.from_regex(r"[a-z][a-z0-9]{0,5}", fullmatch=True),
    min_size=2,
    max_size=4,
    unique=True,
)


def parse(directory: Path, schema: str, instance: str) -> PyXSD:
    """Write a schema/instance pair and run the full pipeline."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "schema.xsd").write_text(schema)
    (directory / "instance.xml").write_text(instance)
    return PyXSD(
        str(directory / "instance.xml"),
        str(directory / "schema.xsd"),
        xmlFileOutput=False,
        transformOutputName=None,
    )


def codes(parser: PyXSD) -> list[str]:
    return [issue.code for issue in parser.report]


def group_schema(names: list[str], optional_first: bool) -> str:
    """A group referenced with and without minOccurs=0, in either order."""
    group_items = "\n".join(f'      <xs:element name="{name}" type="xs:int"/>' for name in names)
    optional = (
        '<xs:complexType name="Optional">'
        "<xs:sequence>"
        '<xs:group ref="g" minOccurs="0"/>'
        "</xs:sequence>"
        "</xs:complexType>"
    )
    required = (
        '<xs:complexType name="Required">'
        "<xs:sequence>"
        '<xs:group ref="g"/>'
        "</xs:sequence>"
        "</xs:complexType>"
    )
    order = [optional, required] if optional_first else [required, optional]
    return (
        f"<xs:schema {XSD}>\n"
        '<xs:group name="g">\n'
        "<xs:sequence>\n"
        f"{group_items}\n"
        "</xs:sequence>\n"
        "</xs:group>\n" + "\n".join(order) + '\n<xs:element name="r" type="Required"/>\n'
        "</xs:schema>"
    )


@settings(max_examples=20, deadline=None)
@given(names=NAMES)
def test_group_occurrence_is_declaration_order_invariant(names):
    """The Optional/Required type order must never change what validates."""
    missing = "<r/>"
    complete = "<r>" + "".join(f"<{name}>1</{name}>" for name in names) + "</r>"
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)

        first = parse(base / "first", group_schema(names, optional_first=True), missing)
        second = parse(base / "second", group_schema(names, optional_first=False), missing)
        assert codes(first) == codes(second)
        assert "occurrence-min" in codes(first)

        first_ok = parse(base / "first-ok", group_schema(names, optional_first=True), complete)
        second_ok = parse(base / "second-ok", group_schema(names, optional_first=False), complete)
        assert codes(first_ok) == codes(second_ok) == []

        def snapshot(parser):
            root = parser.schemaRootInstance
            children = [child._name_ for child in root._children_]
            values = [str(root.__dict__[name]) for name in names]
            return children, values

        # Declaration order must not change what is bound, only how the
        # schema is written down.
        assert snapshot(first_ok) == snapshot(second_ok)
        assert snapshot(first_ok) == (names, ["1"] * len(names))


@settings(max_examples=10, deadline=None)
@given(names=NAMES, unrelated_count=st.integers(min_value=1, max_value=3))
def test_unrelated_parses_do_not_disturb_an_earlier_parser(names, unrelated_count):
    """A later parse (even a failing one) leaves an earlier parser intact."""
    child = names[0]
    schema = (
        f"<xs:schema {XSD}>"
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        f'<xs:element name="{child}" type="xs:int"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        parser = parse(base / "a", schema, f"<r><{child}>7</{child}></r>")
        assert codes(parser) == []
        before = str(parser.schemaRootInstance.__dict__[child])

        for index in range(unrelated_count):
            # An unrelated document with an unresolvable type still runs
            # through schema construction and instance binding.
            other = f'<xs:schema {XSD}><xs:element name="x" type="nope:Type"/></xs:schema>'
            parse(base / f"other{index}", other, "<x/>")

        assert codes(parser) == []
        assert str(parser.schemaRootInstance.__dict__[child]) == before

        parser.parseXML()
        assert "unknown-type" not in codes(parser)
        assert str(parser.schemaRootInstance.__dict__[child]) == before


ROUND_TRIP_SCHEMA = (
    f"<xs:schema {XSD}>"
    '<xs:element name="r"><xs:complexType><xs:sequence>'
    '<xs:element name="text" type="xs:string"/>'
    "</xs:sequence>"
    '<xs:attribute name="note" type="xs:string" use="required"/>'
    "</xs:complexType></xs:element></xs:schema>"
)

# Characters that exercise escaping, XML end-of-line handling, and
# non-ASCII content.
TEXT = st.text(alphabet="ab &<>\"'\r\n\t\u00e9", max_size=10)


def text_to_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\r", "&#13;")
        .replace("\n", "&#10;")
        .replace("\t", "&#9;")
    )


def attribute_to_xml(value: str) -> str:
    return text_to_xml(value).replace('"', "&quot;")


@settings(max_examples=30, deadline=None)
@given(text=TEXT, note=st.text(alphabet="ab &<>\"'\r\n\t", max_size=8))
def test_parse_write_parse_preserves_values(text, note):
    """Writer output rebinds to the same values in a fresh parser."""
    instance = f'<r note="{attribute_to_xml(note)}"><text>{text_to_xml(text)}</text></r>'
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        parser = parse(base, ROUND_TRIP_SCHEMA, instance)
        assert not parser.report.has_errors

        root = parser.schemaRootInstance
        assert str(root.text) == text
        assert str(root.note) == note

        buffer = io.StringIO()
        XmlTreeWriter(root, buffer)
        serialized = buffer.getvalue()

        # The element tree view must carry the exact values...
        reparsed = ET.fromstring(serialized)
        assert (reparsed.findtext("text") or "") == text
        assert reparsed.attrib["note"] == note

        # ...and a full second parse must validate and rebind them.
        second = PyXSD(
            io.StringIO(serialized),
            str(base / "schema.xsd"),
            xmlFileOutput=False,
            transformOutputName=None,
        )
        assert not second.report.has_errors
        second_root = second.schemaRootInstance
        assert str(second_root.text) == text
        assert str(second_root.note) == note
