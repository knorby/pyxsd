"""Binding consumes the declaration carried by the matched particle.

These regressions pin the reviewed defects where name-based lookup
overrode particle attribution:

* a strict wildcard admits an undeclared name even though a later
  declaration shares that name; the child must be validated through the
  wildcard, not the deeper declaration;
* a skip wildcard's positional child must be preserved generically
  instead of being coerced through the later declaration's type;
* two same-named declarations validate each occurrence through the
  declaration of the particle that consumed it (including ``fixed``);
* an extension's inherited declaration still enforces its constraints
  because the base particle carries the base declaration.
"""

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema

XSD_OPEN = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'


def parse(tmp_path, schema_body, instance, mode=ParseModes.NAMESPACED):
    (tmp_path / "schema.xsd").write_text(schema_body)
    (tmp_path / "instance.xml").write_text(instance)
    return Schema.compile(str(tmp_path / "schema.xsd"), mode=mode).parse(
        str(tmp_path / "instance.xml")
    )


def codes(doc):
    return [issue.code for issue in doc.report]


def child_names(doc):
    root = doc.root
    return [child._name_ for child in root._children_]


def test_strict_wildcard_admitting_a_declared_name_is_validated(tmp_path):
    """The first child belongs to the strict wildcard, not the later int."""
    schema = (
        XSD_OPEN
        + "<xs:element name='r'><xs:complexType><xs:sequence>"
        + "<xs:any processContents='strict'/>"
        + "<xs:element name='a' type='xs:int'/>"
        + "</xs:sequence></xs:complexType></xs:element>"
        + "</xs:schema>"
    )
    doc = parse(tmp_path, schema, "<r><a>1</a><a>2</a></r>")
    assert codes(doc) == ["wildcard-no-declaration"]
    assert child_names(doc) == ["a", "a"]


def test_skip_wildcard_positional_child_keeps_generic_value(tmp_path):
    """A skip child must not be coerced through a later declaration."""
    schema = (
        XSD_OPEN
        + "<xs:element name='r'><xs:complexType><xs:sequence>"
        + "<xs:any processContents='skip'/>"
        + "<xs:element name='a' type='xs:int'/>"
        + "</xs:sequence></xs:complexType></xs:element>"
        + "</xs:schema>"
    )
    doc = parse(tmp_path, schema, "<r><a>bad</a><a>2</a></r>")
    assert codes(doc) == []
    assert child_names(doc) == ["a", "a"]
    root = doc.root
    assert [child._value_ for child in root._children_] == [["bad"], ["2"]]


def test_repeated_declarations_use_each_occurrences_fixed_value(tmp_path):
    """The second child belongs to the first declaration's two uses."""
    schema = (
        XSD_OPEN
        + "<xs:element name='r'><xs:complexType><xs:sequence>"
        + "<xs:element name='a' type='xs:int' minOccurs='2' maxOccurs='2' fixed='1'/>"
        + "<xs:element name='a' type='xs:int' fixed='2'/>"
        + "</xs:sequence></xs:complexType></xs:element>"
        + "</xs:schema>"
    )
    doc = parse(tmp_path, schema, "<r><a>1</a><a>2</a><a>2</a></r>")
    assert codes(doc) == ["fixed-element"]


def test_extension_inherited_declaration_keeps_its_fixed_value(tmp_path):
    """The composed model's base particle enforces the base declaration."""
    schema = (
        XSD_OPEN
        + "<xs:complexType name='B'><xs:sequence>"
        + "<xs:element name='a' type='xs:int' fixed='1'/>"
        + "</xs:sequence></xs:complexType>"
        + "<xs:complexType name='D'><xs:complexContent>"
        + "<xs:extension base='B'><xs:sequence>"
        + "<xs:element name='a' type='xs:int' fixed='2'/>"
        + "</xs:sequence></xs:extension></xs:complexContent></xs:complexType>"
        + "<xs:element name='r' type='D'/>"
        + "</xs:schema>"
    )
    invalid = parse(tmp_path, schema, "<r><a>2</a><a>2</a></r>")
    assert codes(invalid) == ["fixed-element"]

    valid = parse(tmp_path, schema, "<r><a>1</a><a>2</a></r>")
    assert codes(valid) == []


def test_strict_wildcard_admits_a_child_with_xsi_type(tmp_path):
    """A strict wildcard accepts an undeclared child carrying xsi:type.

    MS addB116: the xsi:type supplies the governing type, so the
    element need not have a top-level declaration.
    """
    schema = (
        XSD_OPEN
        + "<xs:element name='foo'><xs:complexType><xs:sequence>"
        + "<xs:element name='a'/>"
        + "<xs:any namespace='##any' processContents='strict'"
        + " minOccurs='0' maxOccurs='unbounded'/>"
        + "</xs:sequence></xs:complexType></xs:element>"
        + "</xs:schema>"
    )
    instance = (
        '<foo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        "<a/><b xsi:type='xsd:string'>abc</b>"
        "<c xsi:type='xsd:int'>123</c></foo>"
    )
    doc = parse(tmp_path, schema, instance)
    assert codes(doc) == []
