"""Instance-phase attribute wildcard (``xs:anyAttribute``) enforcement.

An attribute wildcard admits undeclared attributes only when the derived
type's *effective* wildcard admits their namespace.  The effective
wildcard combines several contributions:

* a wildcard inside a referenced ``xs:attributeGroup`` belongs to the
  referring type;
* several wildcards on one type intersect;
* an extension unions its wildcard with the base's, a restriction
  intersects (XSD 1.1 §3.4.2.4, errata E1-10);
* ``##other`` keeps the absent namespace out unless ``##local`` is part
  of the constraint.

An attribute the effective wildcard rejects is an error
(``wildcard-namespace``), reported once (the generic
``unexpected-attribute`` warning is suppressed).  A type with no
attribute wildcard at all keeps the historical warning-only behavior.

``xs:anyType`` is the ur-type: its lax ``##any`` content wildcard admits
undeclared children and its any-attribute equivalent admits any
attribute, so an anyType root is not rejected for carrying child
elements.
"""

import io

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD


def run(schema, xml, mode=ParseModes.NAMESPACED):
    return PyXSD(
        io.StringIO(xml),
        io.StringIO(schema),
        xmlFileOutput=False,
        transformOutputName=None,
        mode=mode,
    )


def run_files(tmp_path, schema_files, instance, schema_name="main.xsd"):
    for name, text in schema_files.items():
        (tmp_path / name).write_text(text)
    (tmp_path / "instance.xml").write_text(instance)
    return PyXSD(
        str(tmp_path / "instance.xml"),
        str(tmp_path / schema_name),
        xmlFileOutput=False,
        transformOutputName=None,
        mode=ParseModes.NAMESPACED,
    )


def codes(parser):
    return [issue.code for issue in parser.report]


def errors(parser):
    return [issue.code for issue in parser.report.errors]


# --- Rule 3: the effective wildcard decides admission ----------------------

WILDO032 = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="http://foobar">'
    '<xs:element name="foo"><xs:complexType>'
    '<xs:anyAttribute namespace="http://foobar"/>'
    "</xs:complexType></xs:element>"
    '<xs:attribute name="name" type="xs:string"/>'
    "</xs:schema>"
)


def test_namespace_literal_wildcard_rejects_foreign_attribute():
    parser = run(
        WILDO032,
        '<foo xmlns="http://foobar" xmlns:att="http://foo" att:name="bar"/>',
    )
    assert errors(parser) == ["wildcard-namespace"]
    # One diagnostic per attribute: the generic warning is suppressed.
    assert "unexpected-attribute" not in codes(parser)


def test_namespace_literal_wildcard_admits_listed_attribute():
    parser = run(
        WILDO032,
        '<foo xmlns="http://foobar" xmlns:att="http://foobar" att:name="bar"/>',
    )
    assert codes(parser) == []


NSCONSTRAINT_OTHER = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="nsConstraint" xmlns="nsConstraint">'
    '<xs:element name="a"><xs:complexType>'
    '<xs:anyAttribute namespace="##other" processContents="skip"/>'
    "</xs:complexType></xs:element>"
    '<xs:attribute name="date" type="xs:date"/>'
    "</xs:schema>"
)


def test_other_wildcard_rejects_target_namespace_attribute():
    parser = run(
        NSCONSTRAINT_OTHER,
        '<test:a xmlns:test="nsConstraint" test:date="2002-04-29"/>',
    )
    assert errors(parser) == ["wildcard-namespace"]


def test_other_wildcard_admits_foreign_namespace_attribute():
    parser = run(
        NSCONSTRAINT_OTHER,
        '<test:a xmlns:test="nsConstraint" xmlns:test1="ns_test1" test1:date="2002-04-29"/>',
    )
    assert codes(parser) == []


NSCONSTRAINT_LIST = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="nsConstraint" xmlns="nsConstraint">'
    '<xs:element name="a"><xs:complexType>'
    '<xs:anyAttribute namespace="ns_test1 ns_test2" processContents="skip"/>'
    "</xs:complexType></xs:element>"
    '<xs:attribute name="date" type="xs:date"/>'
    "</xs:schema>"
)


def test_namespace_list_wildcard_rejects_unlisted_namespace():
    parser = run(
        NSCONSTRAINT_LIST,
        '<test:a xmlns:test="nsConstraint" test:date="2002-04-29"/>',
    )
    assert errors(parser) == ["wildcard-namespace"]


def test_namespace_list_wildcard_admits_listed_namespaces():
    parser = run(
        NSCONSTRAINT_LIST,
        '<test:a xmlns:test="nsConstraint" xmlns:test1="ns_test1"'
        ' xmlns:test2="ns_test2" test1:date="2002-04-29" test2:time="15:15:00"/>',
    )
    assert codes(parser) == []


# --- Rule 1: attribute groups contribute their wildcards -------------------

GROUP_WILDCARD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="urn:t" xmlns:t="urn:t">'
    '<xs:attributeGroup name="g">'
    '<xs:anyAttribute namespace="urn:ok" processContents="skip"/>'
    "</xs:attributeGroup>"
    '<xs:element name="r"><xs:complexType>'
    '<xs:attributeGroup ref="t:g"/>'
    "</xs:complexType></xs:element>"
    "</xs:schema>"
)


def test_attribute_group_wildcard_reaches_binding():
    bad = run(
        GROUP_WILDCARD,
        '<t:r xmlns:t="urn:t" xmlns:x="urn:x" x:a="1"/>',
    )
    assert errors(bad) == ["wildcard-namespace"]

    good = run(
        GROUP_WILDCARD,
        '<t:r xmlns:t="urn:t" xmlns:o="urn:ok" o:a="1"/>',
    )
    assert codes(good) == []


NESTED_GROUP_WILDCARD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="urn:t" xmlns:t="urn:t">'
    '<xs:attributeGroup name="inner">'
    '<xs:anyAttribute namespace="urn:ok" processContents="skip"/>'
    "</xs:attributeGroup>"
    '<xs:attributeGroup name="outer">'
    '<xs:attributeGroup ref="t:inner"/>'
    "</xs:attributeGroup>"
    '<xs:element name="r"><xs:complexType>'
    '<xs:attributeGroup ref="t:outer"/>'
    "</xs:complexType></xs:element>"
    "</xs:schema>"
)


def test_nested_attribute_group_wildcard_reaches_binding():
    bad = run(
        NESTED_GROUP_WILDCARD,
        '<t:r xmlns:t="urn:t" xmlns:x="urn:x" x:a="1"/>',
    )
    assert errors(bad) == ["wildcard-namespace"]

    good = run(
        NESTED_GROUP_WILDCARD,
        '<t:r xmlns:t="urn:t" xmlns:o="urn:ok" o:a="1"/>',
    )
    assert codes(good) == []


# --- Rule 2: combination semantics (test328873, wildZ011) ------------------

MAIN_328873 = (
    '<xs:schema elementFormDefault="qualified"'
    ' xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="a" xmlns:a="a" xmlns:b="b">'
    ' <xs:import schemaLocation="imp.xsd"/>'
    ' <xs:element name="sub" type="a:derived2"/>'
    ' <xs:element name="sub2" type="a:derived3"/>'
    ' <xs:element name="sub3" type="a:derived4"/>'
    ' <xs:element name="sub4" type="a:derived5"/>'
    ' <xs:element name="sub5" type="a:intersection1"/>'
    ' <xs:element name="sub6" type="a:intersection2"/>'
    ' <xs:complexType name="base2"><xs:sequence/>'
    '  <xs:attributeGroup ref="a:attG1-54"/></xs:complexType>'
    ' <xs:complexType name="derived2"><xs:complexContent>'
    '  <xs:extension base="a:base2"><xs:sequence/>'
    '   <xs:attributeGroup ref="a:attG2-54"/>'
    "  </xs:extension></xs:complexContent></xs:complexType>"
    ' <xs:complexType name="base3"><xs:sequence/>'
    '  <xs:attributeGroup ref="a:attG1-51"/></xs:complexType>'
    ' <xs:complexType name="derived3"><xs:complexContent>'
    '  <xs:extension base="a:base3"><xs:sequence/>'
    '   <xs:attributeGroup ref="a:attG2-51"/>'
    "  </xs:extension></xs:complexContent></xs:complexType>"
    ' <xs:complexType name="base4"><xs:sequence/>'
    '  <xs:attributeGroup ref="attG1-61"/></xs:complexType>'
    ' <xs:complexType name="derived4"><xs:complexContent>'
    '  <xs:extension base="a:base4"><xs:sequence/>'
    '   <xs:attributeGroup ref="attG2-61"/>'
    "  </xs:extension></xs:complexContent></xs:complexType>"
    ' <xs:complexType name="derived5"><xs:complexContent>'
    '  <xs:extension base="a:base4"><xs:sequence/>'
    '   <xs:attributeGroup ref="attG3-61"/>'
    "  </xs:extension></xs:complexContent></xs:complexType>"
    ' <xs:complexType name="intersection1">'
    '  <xs:attributeGroup ref="a:attG-a1"/>'
    '  <xs:attributeGroup ref="a:attG2-3"/>'
    " </xs:complexType>"
    ' <xs:complexType name="intersection2">'
    '  <xs:attributeGroup ref="a:attG-a1"/>'
    '  <xs:attributeGroup ref="attG1-61"/>'
    " </xs:complexType>"
    ' <xs:attributeGroup name="attG-a1">'
    '  <xs:anyAttribute namespace="##other" processContents="lax"/>'
    " </xs:attributeGroup>"
    ' <xs:attributeGroup name="attG2-3">'
    '  <xs:anyAttribute namespace="##local b c" processContents="lax"/>'
    " </xs:attributeGroup>"
    ' <xs:attributeGroup name="attG1-54">'
    '  <xs:anyAttribute namespace="##other" processContents="lax"/>'
    " </xs:attributeGroup>"
    ' <xs:attributeGroup name="attG2-54">'
    '  <xs:anyAttribute namespace="b c" processContents="lax"/>'
    " </xs:attributeGroup>"
    ' <xs:attributeGroup name="attG1-51">'
    '  <xs:anyAttribute namespace="##other" processContents="lax"/>'
    " </xs:attributeGroup>"
    ' <xs:attributeGroup name="attG2-51">'
    '  <xs:anyAttribute namespace="##targetNamespace ##local b c"'
    ' processContents="lax"/>'
    " </xs:attributeGroup>"
    "</xs:schema>"
)

IMP_328873 = (
    '<xs:schema elementFormDefault="qualified"'
    ' xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    ' <xs:attributeGroup name="attG1-61">'
    '  <xs:anyAttribute namespace="##other" processContents="lax"/>'
    " </xs:attributeGroup>"
    ' <xs:attributeGroup name="attG2-61">'
    '  <xs:anyAttribute namespace="##local b c" processContents="lax"/>'
    " </xs:attributeGroup>"
    ' <xs:attributeGroup name="attG3-61">'
    '  <xs:anyAttribute namespace="b c" processContents="lax"/>'
    " </xs:attributeGroup>"
    "</xs:schema>"
)


def suite_328873(tmp_path, instance):
    return run_files(
        tmp_path,
        {"main.xsd": MAIN_328873, "imp.xsd": IMP_328873},
        instance,
    )


def test_extension_union_keeps_base_exclusions(tmp_path):
    """derived2: ##other plus b c is still not(a) and not-absent."""
    parser = suite_328873(
        tmp_path,
        '<a:sub a:att1="abc" att2="bc" b:att3="foo" att="a"'
        ' xmlns:a="a" xmlns:b="b" xmlns:x="x"></a:sub>',
    )
    assert "wildcard-namespace" in errors(parser)


def test_extension_union_to_any_when_own_admits_target_and_local(tmp_path):
    parser = suite_328873(
        tmp_path,
        '<a:sub2 a:att1="abc" att2="bc" b:att3="foo" x:att4="val" att="a"'
        ' xmlns:a="a" xmlns:b="b" xmlns:x="x"/>',
    )
    assert codes(parser) == []


def test_extension_union_to_any_when_base_other_has_no_target(tmp_path):
    parser = suite_328873(
        tmp_path,
        '<a:sub3 a:att1="abc" att2="bc" b:att3="foo" x:att4="val" att="a"'
        ' xmlns:a="a" xmlns:b="b" xmlns:x="x"/>',
    )
    assert codes(parser) == []


def test_extension_union_keeps_absent_excluded(tmp_path):
    """derived5: ##other without a target plus b c still rejects absent."""
    parser = suite_328873(
        tmp_path,
        '<a:sub4 a:att1="abc" att2="bc" att="a" xmlns:a="a" xmlns:b="b" xmlns:x="x"/>',
    )
    assert "wildcard-namespace" in errors(parser)


def test_group_intersection_restricts_to_the_common_set(tmp_path):
    """intersection1: ##other(a) intersected with ##local b c is b c."""
    parser = suite_328873(
        tmp_path,
        '<a:sub5 b:att1="abc" att2="bc" att="a" xmlns:a="a" xmlns:b="b" xmlns:x="x"/>',
    )
    assert "wildcard-namespace" in errors(parser)


def test_group_intersection_of_two_others(tmp_path):
    """intersection2: ##other(a) intersected with ##other(no target)."""
    parser = suite_328873(
        tmp_path,
        '<a:sub6 a:att1="abc" att2="bc" att="a" xmlns:a="a" xmlns:b="b" xmlns:x="x"/>',
    )
    assert "wildcard-namespace" in errors(parser)


WILDZ011_MAIN = (
    '<xs:schema elementFormDefault="qualified"'
    ' xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="a" xmlns:a="a" xmlns:b="b">'
    ' <xs:import namespace="b" schemaLocation="b.xsd"/>'
    ' <xs:element name="doc" type="a:metadata"/>'
    ' <xs:complexType name="metadata"><xs:sequence/>'
    '  <xs:attributeGroup ref="a:attG-a"/>'
    '  <xs:attributeGroup ref="b:attG-b"/>'
    '  <xs:anyAttribute namespace="##any" processContents="lax"/>'
    " </xs:complexType>"
    ' <xs:attributeGroup name="attG-a">'
    '  <xs:anyAttribute namespace="##targetNamespace" processContents="lax"/>'
    " </xs:attributeGroup>"
    "</xs:schema>"
)

WILDZ011_B = (
    '<xs:schema elementFormDefault="qualified"'
    ' xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="b" xmlns="b">'
    ' <xs:attributeGroup name="attG-b">'
    '  <xs:anyAttribute namespace="##any"/>'
    " </xs:attributeGroup>"
    "</xs:schema>"
)


def test_empty_intersection_rejects_every_attribute(tmp_path):
    parser = run_files(
        tmp_path,
        {"main.xsd": WILDZ011_MAIN, "b.xsd": WILDZ011_B},
        '<a:doc x:blah="a" xmlns:a="a" xmlns:b="b" xmlns:x="x"/>',
    )
    assert errors(parser) == ["wildcard-namespace"]


def test_intersection_admits_the_common_target_namespace(tmp_path):
    parser = run_files(
        tmp_path,
        {"main.xsd": WILDZ011_MAIN, "b.xsd": WILDZ011_B},
        '<a:doc a:blah="a" xmlns:a="a" xmlns:b="b" xmlns:x="x"/>',
    )
    assert codes(parser) == []


RESTRICTION_SCHEMA = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="urn:t" xmlns:t="urn:t">'
    ' <xs:complexType name="base"><xs:sequence/>'
    '  <xs:anyAttribute namespace="##any" processContents="lax"/>'
    " </xs:complexType>"
    ' <xs:complexType name="narrowed"><xs:complexContent>'
    '  <xs:restriction base="t:base"><xs:sequence/>'
    '   <xs:anyAttribute namespace="urn:ok" processContents="lax"/>'
    "  </xs:restriction></xs:complexContent></xs:complexType>"
    ' <xs:element name="doc" type="t:narrowed"/>'
    "</xs:schema>"
)


def test_restriction_intersects_with_a_permissive_base():
    bad = run(
        RESTRICTION_SCHEMA,
        '<t:doc xmlns:t="urn:t" xmlns:x="urn:x" x:a="1"/>',
    )
    assert errors(bad) == ["wildcard-namespace"]

    good = run(
        RESTRICTION_SCHEMA,
        '<t:doc xmlns:t="urn:t" xmlns:o="urn:ok" o:a="1"/>',
    )
    assert codes(good) == []


# --- Rule 4: xsd:anyType roots admit undeclared children -------------------

ANY_TYPE_SCHEMA = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' targetNamespace="urn:t" xmlns:x="urn:t">'
    ' <xs:element name="root_elem" type="xs:anyType"/>'
    ' <xs:complexType name="ctype_foo"><xs:sequence>'
    '  <xs:element name="a" type="xs:string"/>'
    " </xs:sequence></xs:complexType>"
    "</xs:schema>"
)


def test_anytype_root_binds_undeclared_children():
    parser = run(
        ANY_TYPE_SCHEMA,
        '<x:root_elem xmlns:x="urn:t"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<x:myelem xsi:type="x:ctype_foo"><a>hello</a></x:myelem>'
        "</x:root_elem>",
    )
    assert codes(parser) == []
    root = parser.schemaRootInstance
    assert [child._name_ for child in root._children_] == ["{urn:t}myelem"]


def test_anytype_root_validates_a_childs_xsi_type():
    parser = run(
        ANY_TYPE_SCHEMA,
        '<x:root_elem xmlns:x="urn:t"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<x:myelem xsi:type="x:ctype_foo"><b/></x:myelem>'
        "</x:root_elem>",
    )
    assert "unexpected-element" in errors(parser)


# --- controls ---------------------------------------------------------------

WILD_I001_SCHEMA = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:a="foo">'
    '<xs:element name="foo"><xs:complexType><xs:sequence>'
    '<xs:any id="bar" namespace="##other" processContents="lax"'
    ' maxOccurs="2" minOccurs="1" a:b="c"/>'
    "</xs:sequence></xs:complexType></xs:element></xs:schema>"
)


def test_foreign_qualified_attribute_on_a_wildcard_stays_valid():
    from pyxsd.validation import IssueSeverity

    parser = run(WILD_I001_SCHEMA, "<foo/>")
    schema_errors = [
        issue
        for issue in parser.report.for_phase("schema")
        if issue.severity is IssueSeverity.ERROR
    ]
    assert schema_errors == []


def test_no_attribute_wildcard_keeps_the_warning_only():
    schema = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:complexType name="t"><xs:sequence>'
        '<xs:element name="v" type="xs:string"/></xs:sequence></xs:complexType>'
        '<xs:element name="root" type="t"/></xs:schema>'
    )
    parser = run(schema, '<root stray="1"><v>x</v></root>')
    assert not parser.report.has_errors
    assert "unexpected-attribute" in codes(parser)
