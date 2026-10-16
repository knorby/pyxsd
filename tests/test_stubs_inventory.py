"""The stub inventory maps generated classes to stub-safe names.

Every rule is pinned at the seam later rendering tasks consume:
which classes are inventoried (root-dispatch class skipped, class
objects deduplicated), how stub names are derived (named types keep
their name, inline types are synthesized and sanitized), and how name
collisions are suffixed deterministically.
"""

import io
import pathlib
import tempfile

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema
from pyxsd.stubs import _inventory, _match_type_er

SIMPLE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="itemType"><xs:sequence>
    <xs:element name="name" type="xs:string"/>
  </xs:sequence></xs:complexType>
  <xs:element name="inventory"><xs:complexType><xs:sequence>
    <xs:element name="item" type="itemType" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

INLINE_COLLISION = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="aType"><xs:sequence>
    <xs:element name="shipTo"><xs:complexType><xs:sequence>
      <xs:element name="x" type="xs:string"/>
    </xs:sequence></xs:complexType></xs:element>
  </xs:sequence></xs:complexType>
  <xs:complexType name="bType"><xs:sequence>
    <xs:element name="shipTo"><xs:complexType><xs:sequence>
      <xs:element name="y" type="xs:string"/>
    </xs:sequence></xs:complexType></xs:element>
  </xs:sequence></xs:complexType>
  <xs:complexType name="cType"><xs:sequence>
    <xs:element name="shipTo"><xs:complexType><xs:sequence>
      <xs:element name="z" type="xs:string"/>
    </xs:sequence></xs:complexType></xs:element>
  </xs:sequence></xs:complexType>
</xs:schema>"""


def compile_schema(xsd: str) -> Schema:
    return Schema.compile(io.StringIO(xsd))


def test_inventory_skips_schema_dispatch_class():
    entries = _inventory(compile_schema(SIMPLE))
    assert all(entry.name != "schema" for entry in entries)


def test_inventory_deduplicates_classes():
    entries = _inventory(compile_schema(SIMPLE))
    ids = [id(entry.cls) for entry in entries]
    assert len(ids) == len(set(ids))


def test_named_type_keeps_its_name():
    entries = _inventory(compile_schema(SIMPLE))
    assert any(entry.name == "itemType" and entry.note is None for entry in entries)


def test_inline_type_gets_synthesized_pascal_name():
    entries = _inventory(compile_schema(SIMPLE))
    inline = [e for e in entries if e.note and "|complexType" in e.note]
    assert len(inline) == 1
    assert inline[0].name == "Inventory"  # PascalCase of the declaring element
    assert inline[0].er is not None


def test_strict_namespace_collision_suffixes():
    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
      xmlns:a="urn:a" xmlns:b="urn:b" targetNamespace="urn:a"
      elementFormDefault="qualified">
      <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
      <xs:complexType name="thing"><xs:sequence>
        <xs:element name="x" type="xs:string"/>
      </xs:sequence></xs:complexType>
      <xs:element name="root" type="a:thing"/>
    </xs:schema>"""
    other = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
      targetNamespace="urn:b" elementFormDefault="qualified">
      <xs:complexType name="thing"><xs:sequence>
        <xs:element name="y" type="xs:int"/>
      </xs:sequence></xs:complexType>
    </xs:schema>"""

    with tempfile.TemporaryDirectory() as tmp:
        pathlib.Path(tmp, "b.xsd").write_text(other)
        pathlib.Path(tmp, "a.xsd").write_text(xsd)
        # Strict namespace handling is ParseModes.NAMESPACED; the default
        # (legacy) mode merges same-local-name imports into one component,
        # so only strict mode produces two classes.
        schema = Schema.compile(pathlib.Path(tmp, "a.xsd"), mode=ParseModes.NAMESPACED)
    names = [e.name for e in _inventory(schema)]
    assert "thing" in names and "thing_ns2" in names


def test_invalid_identifier_characters_are_sanitized():
    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="item-type"><xs:sequence>
        <xs:element name="x" type="xs:string"/>
      </xs:sequence></xs:complexType>
      <xs:element name="root" type="item-type"/>
    </xs:schema>"""
    entries = _inventory(compile_schema(xsd))
    entry = next(e for e in entries if e.name == "item_type")
    assert entry.note is not None and "item-type" in entry.note
    assert entry.er is not None


def test_leading_digit_is_prefixed_with_underscore():
    # The name is illegal in XSD (an error on the report), but the
    # inventory must still produce a usable identifier: rendering never
    # requires a valid schema.
    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="9lives"><xs:sequence>
        <xs:element name="x" type="xs:string"/>
      </xs:sequence></xs:complexType>
    </xs:schema>"""
    entries = _inventory(compile_schema(xsd))
    entry = next(e for e in entries if e.name == "_9lives")
    assert entry.note is not None and "9lives" in entry.note


def test_colliding_inline_names_get_numeric_suffixes():
    entries = _inventory(compile_schema(INLINE_COLLISION))
    names = [e.name for e in entries]
    assert "ShipTo" in names and "ShipTo_2" in names and "ShipTo_3" in names
    inline = [e for e in entries if e.note and "|complexType" in e.note]
    assert len(inline) == 3


def test_name_with_no_usable_characters_becomes_underscore():
    # An empty element name makes the inline type's bookkeeping name
    # ``|complexType`` (both an error on the report and the only name
    # there is); the inventory still has to produce an identifier.
    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name=""><xs:complexType><xs:sequence>
        <xs:element name="x" type="xs:string"/>
      </xs:sequence></xs:complexType></xs:element>
    </xs:schema>"""
    entries = _inventory(compile_schema(xsd))
    entry = next(e for e in entries if e.cls.__name__ == "|complexType")
    assert entry.name == "_"
    assert entry.note is not None and "|complexType" in entry.note


def test_class_without_a_type_er_gets_no_er():
    # An overlay (or builtin) class is not in the component table; the
    # inventory lists it without guessing a representative. Overlays enter
    # ``schema.classes`` the same way ``load_overlay_classes`` does.
    schema = compile_schema(SIMPLE)

    class Overlaid:
        pass

    schema.classes["Overlaid"] = Overlaid
    entry = next(e for e in _inventory(schema) if e.cls is Overlaid)
    assert entry.name == "Overlaid"
    assert entry.note is None
    assert entry.er is None


def test_ambiguous_type_er_without_a_matching_key_is_not_guessed():
    # Two same-local-name declarations are told apart by the class's
    # Clark-name key; a key that identifies neither must not pick one.
    class FakeEr:
        def __init__(self, name, namespace):
            self.name = name
            self._namespace = namespace

        def getNamespace(self):
            return self._namespace

    candidates = [FakeEr("thing", "urn:a"), FakeEr("thing", "urn:b")]
    assert _match_type_er(candidates, ["{urn:b}thing"]) is candidates[1]
    assert _match_type_er(candidates, ["thing_extra"]) is None


def test_same_name_collision_without_a_namespace_note():
    # Same-local-name classes whose declaration is in no namespace get
    # the namespace suffix and a note saying the namespace is absent.
    schema = compile_schema(SIMPLE)

    class itemType:  # a stand-in for a class the table has no entry for
        pass

    schema.classes["itemType_replacement"] = itemType
    entry = next(e for e in _inventory(schema) if e.cls is itemType)
    assert entry.name == "itemType_ns2"
    assert entry.note is not None and "namespace absent" in entry.note
