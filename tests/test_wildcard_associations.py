"""Wildcard binding follows the particle that admitted each child.

The content matcher returns child-to-particle associations, and binding
consumes them. These regressions pin the reviewed defects:

* a sequence of several wildcards must use each particle's own
  ``processContents`` (not the first compatible one);
* a wildcard contributed by a named group must reach class metadata and
  binding;
* a wildcard particle may consume a declared name when it is the
  particle active at that position (positional matching);
* ``##targetNamespace``/``##other`` resolve against the declaring
  document, not the inheriting schema;
* strict wildcards look up *global* declarations only;
* ``##other`` never admits the absent namespace.
"""

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD

XSD_OPEN = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'


def parse(tmp_path, schema_body, instance):
    (tmp_path / "schema.xsd").write_text(schema_body)
    (tmp_path / "instance.xml").write_text(instance)
    return PyXSD(
        str(tmp_path / "instance.xml"),
        str(tmp_path / "schema.xsd"),
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
    )


def codes(parser):
    return [issue.code for issue in parser.report]


def child_names(parser):
    root = parser.schemaRootInstance
    return [child._name_ for child in root._children_]


def test_second_wildcard_uses_its_own_process_contents(tmp_path):
    """skip then strict: the second child must meet the strict particle."""
    schema = (
        XSD_OPEN
        + "<xs:element name='r'><xs:complexType><xs:sequence>"
        + "<xs:any processContents='skip'/>"
        + "<xs:any processContents='strict'/>"
        + "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    parser = parse(tmp_path, schema, "<r><a/><b/></r>")
    assert codes(parser) == ["wildcard-no-declaration"]
    # The rejected child is reported, but (as before) binding keeps a
    # generic placeholder so the tree still mirrors the document.
    assert child_names(parser) == ["a", "b"]


def test_group_wildcard_binds_generic_child(tmp_path):
    """A wildcard inside a named group reaches class metadata and binding."""
    schema = (
        XSD_OPEN
        + "<xs:group name='g'><xs:sequence>"
        + "<xs:any processContents='skip'/>"
        + "</xs:sequence></xs:group>"
        + "<xs:element name='r'><xs:complexType>"
        + "<xs:group ref='g'/>"
        + "</xs:complexType></xs:element></xs:schema>"
    )
    parser = parse(tmp_path, schema, "<r><a/></r>")
    assert codes(parser) == []
    assert child_names(parser) == ["a"]


def test_wildcard_can_consume_a_declared_name_positionally(tmp_path):
    """any then a: the first a satisfies the wildcard, the second the element."""
    schema = (
        XSD_OPEN
        + "<xs:element name='r'><xs:complexType><xs:sequence>"
        + "<xs:any processContents='skip'/>"
        + "<xs:element name='a' type='xs:int' maxOccurs='2'/>"
        + "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    (tmp_path / "v").mkdir(exist_ok=True)
    valid = parse(tmp_path / "v", schema, "<r><a>1</a><a>2</a></r>")
    assert codes(valid) == []
    # The first child was admitted by the wildcard, so it is generic
    # content: only the declared occurrence reaches the typed accessor.
    assert valid.schemaRootInstance.a == [2]

    # One a is not enough for both a minOccurs=1 wildcard and the element.
    (tmp_path / "short").mkdir(exist_ok=True)
    short = parse(tmp_path / "short", schema, "<r><a>1</a></r>")
    assert short.report.has_errors
    assert "occurrence-min" in codes(short)

    # A wildcard-absorbable name followed by the declared element is fine.
    (tmp_path / "mixed").mkdir(exist_ok=True)
    mixed = parse(tmp_path / "mixed", schema, "<r><b/><a>1</a></r>")
    assert codes(mixed) == []
    assert child_names(mixed) == ["b", "a"]


def test_inherited_wildcard_keeps_source_target_namespace(tmp_path):
    """##targetNamespace in an imported base type means the base's namespace."""
    main = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
        ' targetNamespace="urn:h" xmlns:a="urn:a" elementFormDefault="unqualified">'
        '  <xs:import namespace="urn:a" schemaLocation="a.xsd"/>'
        '  <xs:complexType name="D"><xs:complexContent>'
        '    <xs:extension base="a:T"><xs:sequence>'
        '      <xs:element name="end" type="xs:string"/>'
        "    </xs:sequence></xs:extension>"
        "  </xs:complexContent></xs:complexType>"
        '  <xs:element name="r" type="D"/>'
        "</xs:schema>"
    )
    imported = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
        ' targetNamespace="urn:a" elementFormDefault="unqualified">'
        '  <xs:complexType name="T"><xs:sequence>'
        '    <xs:any namespace="##targetNamespace" processContents="skip"/>'
        "  </xs:sequence></xs:complexType>"
        "</xs:schema>"
    )
    (tmp_path / "a.xsd").write_text(imported)
    valid = parse(
        tmp_path,
        main,
        '<h:r xmlns:h="urn:h" xmlns:a="urn:a"><a:x/><end/></h:r>',
    )
    assert codes(valid) == []
    assert child_names(valid) == ["{urn:a}x", "end"]

    (tmp_path / "bad").mkdir(exist_ok=True)
    invalid = parse(
        tmp_path / "bad",
        main,
        '<h:r xmlns:h="urn:h" xmlns:a="urn:a"><h:x/><end/></h:r>',
    )
    assert invalid.report.has_errors


def test_strict_wildcard_ignores_local_declarations(tmp_path):
    """A local declaration in an unrelated type is not a global one."""
    schema = (
        XSD_OPEN
        + "<xs:complexType name='Unused'><xs:sequence>"
        + "<xs:element name='a' type='xs:int'/>"
        + "</xs:sequence></xs:complexType>"
        + "<xs:element name='r'><xs:complexType><xs:sequence>"
        + "<xs:any processContents='strict'/>"
        + "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )
    parser = parse(tmp_path, schema, "<r><a>7</a></r>")
    assert "wildcard-no-declaration" in codes(parser)


def test_strict_attribute_wildcard_ignores_local_declarations(tmp_path):
    """The same global-only rule applies to anyAttribute strict."""
    schema = (
        XSD_OPEN
        + "<xs:complexType name='Unused'>"
        + "<xs:attribute name='a' type='xs:string'/>"
        + "</xs:complexType>"
        + "<xs:element name='r'><xs:complexType>"
        + "<xs:anyAttribute processContents='strict'/>"
        + "</xs:complexType></xs:element></xs:schema>"
    )
    parser = parse(tmp_path, schema, '<r a="7"/>')
    assert "wildcard-no-declaration" in codes(parser)


def test_optional_suffix_does_not_hide_wildcard_associations(tmp_path):
    """A zero-width optional suffix must not erase earlier associations."""
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        "  <xs:element name='r'><xs:complexType><xs:sequence>"
        "    <xs:any processContents='skip'/>"
        "    <xs:any processContents='strict'/>"
        "    <xs:element name='end' type='xs:string' minOccurs='0'/>"
        "  </xs:sequence></xs:complexType></xs:element>"
        "</xs:schema>"
    )
    parser = parse(tmp_path, schema, "<r><a/><b/></r>")
    # ''a'' matches the first (skip) wildcard generically; ''b'' falls to the
    # strict wildcard, which finds no global declaration.
    assert codes(parser) == ["wildcard-no-declaration"]
    assert child_names(parser) == ["a", "b"]


def test_other_namespace_excludes_unqualified_content(tmp_path):
    """##other admits present namespaces except the target, never absent."""
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
        ' targetNamespace="urn:t" elementFormDefault="qualified">'
        "  <xs:element name='r'><xs:complexType><xs:sequence>"
        "    <xs:any namespace='##other' processContents='skip'/>"
        "  </xs:sequence></xs:complexType></xs:element>"
        "</xs:schema>"
    )
    (tmp_path / "bad").mkdir(exist_ok=True)
    invalid = parse(
        tmp_path / "bad",
        schema,
        '<t:r xmlns:t="urn:t"><a/></t:r>',
    )
    assert invalid.report.has_errors

    (tmp_path / "good").mkdir(exist_ok=True)
    valid = parse(
        tmp_path / "good",
        schema,
        '<t:r xmlns:t="urn:t" xmlns:o="urn:o"><o:a/></t:r>',
    )
    assert codes(valid) == []
