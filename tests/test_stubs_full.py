"""The full-mode stub renderer emits the documented .pyi surface.

The golden file is the human-reviewable contract for a small schema:
class order, inheritance mirrored from XSD derivation (the base class
owns its members, the derived class only its own), simple types as
datatype subclasses with facet comments, and root elements as
module-level aliases. The other tests pin syntax validity and overlay
visibility.
"""

import io
from pathlib import Path

from pyxsd.schema import Schema
from pyxsd.stubs import emit_stub

ORDERS = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="SKU"><xs:restriction base="xs:string">
    <xs:pattern value="\\d{3}-[A-Z]{2}"/>
  </xs:restriction></xs:simpleType>
  <xs:complexType name="Item"><xs:sequence>
    <xs:element name="sku" type="SKU"/>
    <xs:element name="quantity" type="xs:int"/>
    <xs:element name="note" type="xs:string" minOccurs="0"/>
  </xs:sequence>
  <xs:attribute name="id" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:element name="orders"><xs:complexType><xs:sequence>
    <xs:element name="item" type="Item" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""


def test_orders_full_stub_matches_golden():
    schema = Schema.compile(io.StringIO(ORDERS))
    text = emit_stub(schema)
    golden = Path(__file__).parent / "fixtures" / "stubs" / "orders.pyi"
    assert text == golden.read_text()


def test_stub_is_valid_python_syntax():
    import ast

    schema = Schema.compile(io.StringIO(ORDERS))
    ast.parse(emit_stub(schema))  # raises SyntaxError if the stub is malformed


def test_comment_only_members_keep_the_block_parseable():
    # A member whose XML name has no usable Python accessor renders as a
    # comment, and comments are not statements: the block still needs
    # ``...`` or it is an indentation error.
    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="rec"><xs:sequence>
        <xs:element name="ship-to" type="xs:string"/>
      </xs:sequence></xs:complexType>
      <xs:element name="root" type="rec"/>
    </xs:schema>"""
    import ast

    stub = emit_stub(Schema.compile(io.StringIO(xsd)))
    ast.parse(stub)
    rec_block = stub.split("class rec(SchemaBase):")[1]
    assert "# element 'ship-to' is not dot-accessible" in rec_block
    assert "    ..." in rec_block


def test_xsd_derivation_mirrors_class_inheritance():
    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="base"><xs:sequence>
        <xs:element name="a" type="xs:string"/></xs:sequence></xs:complexType>
      <xs:complexType name="derived"><xs:complexContent><xs:extension base="base">
        <xs:sequence><xs:element name="b" type="xs:int"/></xs:sequence>
      </xs:extension></xs:complexContent></xs:complexType>
      <xs:element name="root" type="derived"/>
    </xs:schema>"""
    text = emit_stub(Schema.compile(io.StringIO(xsd)))
    assert "class derived(base):" in text
    derived_block = text.split("class derived(base):")[1]
    assert "a:" not in derived_block.split("class ")[0]  # inherited, not re-rendered


def test_overlay_members_appear_in_stub(tmp_path):
    overlay = tmp_path / "overlay.py"
    overlay.write_text(
        "from pyxsd.schema_base import SchemaBase\n\n"
        "class Item(SchemaBase):\n"
        "    name = 'Item'\n"
        "    @property\n"
        "    def label(self) -> str:\n"
        "        return 'x'\n"
        "    def double(self, count: int) -> str:\n"
        "        return 'x' * count\n"
    )
    schema = Schema.compile(io.StringIO(ORDERS), overlay=overlay)
    text = emit_stub(schema)
    # The decorator form is a new construct in the emitted block; it
    # still has to be parseable Python.
    import ast

    ast.parse(text)
    # A property reads as a value, not a call: the stub must carry the
    # decorator and no parameter list.
    assert "    @property\n    def label(self) -> str: ..." in text
    # A plain overlay method stays an ordinary ``def``.
    assert "    def double(self, count: int) -> str: ..." in text


ROOT_CLASS_COLLISION = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="Item"><xs:sequence>
    <xs:element name="n" type="xs:int"/></xs:sequence></xs:complexType>
  <xs:element name="Item" type="Item"/>
</xs:schema>"""


def test_root_name_colliding_with_a_class_renders_as_a_comment():
    # ``class Item`` plus ``Item: type[Item]`` is a duplicate definition
    # mypy rejects (``name already defined``); the root keeps its name
    # in a comment instead.
    import ast

    text = emit_stub(Schema.compile(io.StringIO(ROOT_CLASS_COLLISION)))
    ast.parse(text)
    assert "class Item(SchemaBase):" in text
    assert "Item: type[Item]" not in text
    assert "# root element 'Item': name already used by class 'Item'" in text


def test_root_name_colliding_with_a_datatype_import_renders_as_a_comment():
    # ``Int: type[Int]`` beside ``from pyxsd.xsd_data_types import Int``
    # is a duplicate definition; the alias would also read as a
    # self-referring annotation.
    import ast

    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="Int" type="xs:int"/>
    </xs:schema>"""
    text = emit_stub(Schema.compile(io.StringIO(xsd)))
    ast.parse(text)
    assert "Int: type[Int]" not in text
    assert "# root element 'Int': name already used by an import" in text


def test_root_names_colliding_with_unconditional_imports_render_as_comments():
    # ``Any`` and ``SchemaBase`` are imported by every full stub, so a
    # root carrying either name can never alias.
    import ast

    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="a"><xs:sequence>
        <xs:element name="x" type="xs:int"/></xs:sequence></xs:complexType>
      <xs:element name="Any" type="a"/>
      <xs:element name="SchemaBase" type="a"/>
      <xs:element name="plain" type="a"/>
    </xs:schema>"""
    text = emit_stub(Schema.compile(io.StringIO(xsd)))
    ast.parse(text)
    assert "Any: type[a]" not in text
    assert "SchemaBase: type[a]" not in text
    assert "# root element 'Any': name already used by an import" in text
    assert "# root element 'SchemaBase': name already used by an import" in text
    # The positive control: an ordinary root name still aliases.
    assert "plain: type[a]" in text
