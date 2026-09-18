"""Type-resolution spine tests.

Pins the Area-F mechanisms: anonymous inline types on XSD declarations
that ride a re-bound default xmlns (NIST bug 9922), built-in resolution
for unprefixed ``type`` references under a default xmlns bound to the
XSD namespace, advisory handling of failing ``xsi:schemaLocation``
hints (``schema-hint`` warning, never masking explicit-input errors),
and transitive substitution-group admission in the compiled content
model.
"""

from __future__ import annotations

import io

import pytest

from pyxsd.binding import ParseModes
from pyxsd.parser import PyXSD

XSD = "http://www.w3.org/2001/XMLSchema"
XSI = "http://www.w3.org/2001/XMLSchema-instance"


def _parse(schema_text, instance_text, tmp_path, mode=ParseModes.NAMESPACED, **kwargs):
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(schema_text)
    return PyXSD(
        io.StringIO(instance_text),
        xsdFile=str(schema_path),
        xmlFileOutput=False,
        mode=mode,
        **kwargs,
    )


def _codes(parser):
    return [issue.code for issue in parser.report.issues]


def _errors(parser):
    return [issue for issue in parser.report.issues if issue.severity.name == "ERROR"]


class TestReboundDefaultXmlns:
    """XSD declarations whose content rides a re-bound default xmlns."""

    BRIEF_SCHEMA = (
        f'<xs:schema xmlns:xs="{XSD}" xmlns="urn:nist" targetNamespace="urn:nist">'
        f'<xs:element xmlns="{XSD}" name="out">'
        "<complexType><sequence>"
        '<element name="a" type="string" minOccurs="0" maxOccurs="99"/>'
        "</sequence>"
        '<attribute name="attr" type="string"/></complexType>'
        "</xs:element></xs:schema>"
    )

    def test_brief_repro_lacks_unknown_type(self, tmp_path):
        parser = _parse(
            self.BRIEF_SCHEMA, '<out xmlns="urn:nist" attr="foo"><a>foo</a></out>', tmp_path
        )
        assert "unknown-type" not in _codes(parser)

    def test_rebound_inline_complex_type_binds_cleanly(self, tmp_path):
        schema = (
            f'<xs:schema xmlns:xs="{XSD}" xmlns="urn:nist" targetNamespace="urn:nist" '
            'elementFormDefault="qualified">'
            f'<xs:element xmlns="{XSD}" name="out">'
            "<complexType><sequence>"
            '<element name="a" type="string" minOccurs="0" maxOccurs="99"/>'
            "</sequence>"
            '<attribute name="attr" type="string"/></complexType>'
            "</xs:element></xs:schema>"
        )
        parser = _parse(schema, '<out xmlns="urn:nist" attr="foo"><a>foo</a></out>', tmp_path)
        assert parser.report.issues == []

    def test_prefixed_control_binds_cleanly(self, tmp_path):
        schema = (
            f'<xs:schema xmlns:xs="{XSD}" xmlns="urn:nist" targetNamespace="urn:nist" '
            'elementFormDefault="qualified">'
            '<xs:element name="out">'
            "<xs:complexType><xs:sequence>"
            '<xs:element name="a" type="xs:string" minOccurs="0" maxOccurs="99"/>'
            "</xs:sequence>"
            '<xs:attribute name="attr" type="xs:string"/></xs:complexType>'
            "</xs:element></xs:schema>"
        )
        parser = _parse(schema, '<out xmlns="urn:nist" attr="foo"><a>foo</a></out>', tmp_path)
        assert parser.report.issues == []


class TestUnprefixedBuiltinsUnderDefaultXsdNs:
    """``type="string"`` with the default xmlns bound to the XSD namespace."""

    SCHEMA = (
        f'<xs:schema xmlns:xs="{XSD}" xmlns="{XSD}" targetNamespace="urn:t" '
        'elementFormDefault="qualified">'
        '<element name="out"><complexType><sequence>'
        '<element name="a" type="string"/>'
        "</sequence>"
        '<attribute name="attr" type="string"/></complexType></element>'
        "</xs:schema>"
    )

    def test_schema_compiles_and_instance_validates(self, tmp_path):
        parser = _parse(self.SCHEMA, '<out xmlns="urn:t" attr="foo"><a>foo</a></out>', tmp_path)
        assert parser.report.issues == []

    def test_unqualified_local_form_also_binds(self, tmp_path):
        schema = (
            f'<xs:schema xmlns:xs="{XSD}" xmlns="{XSD}" targetNamespace="urn:t">'
            '<element name="out"><complexType><sequence>'
            '<element name="a" type="string"/>'
            "</sequence></complexType></element></xs:schema>"
        )
        parser = _parse(
            schema,
            '<out xmlns="urn:t"><a xmlns="">foo</a></out>',
            tmp_path,
        )
        assert parser.report.issues == []

    def test_builtin_reference_does_not_shadow_user_type_in_other_namespace(self, tmp_path):
        schema = (
            f'<xs:schema xmlns:xs="{XSD}" xmlns="{XSD}" xmlns:u="urn:u" '
            'targetNamespace="urn:u">'
            '<complexType name="string">'
            '<simpleContent><restriction base="xs:string">'
            '<enumeration value="only"/>'
            "</restriction></simpleContent>"
            "</complexType>"
            '<element name="builtinRef" type="string"/>'
            '<element name="userRef" type="u:string"/>'
            "</xs:schema>"
        )
        # The unprefixed reference resolves to the xs:string built-in,
        # which accepts any text; the user type named "string" in urn:u
        # would reject this value.
        parser = _parse(schema, '<builtinRef xmlns="urn:u">anything goes</builtinRef>', tmp_path)
        assert parser.report.issues == []
        # The prefixed reference resolves to the user type in urn:u.
        parser = _parse(schema, '<userRef xmlns="urn:u">wrong</userRef>', tmp_path)
        assert _errors(parser)
        parser = _parse(schema, '<userRef xmlns="urn:u">only</userRef>', tmp_path)
        assert parser.report.issues == []


class TestSchemaLocationHints:
    """Failing ``xsi:schemaLocation`` hints are advisory warnings."""

    MAIN_SCHEMA = (
        f'<xs:schema xmlns:xs="{XSD}" targetNamespace="urn:m" '
        'elementFormDefault="qualified">'
        '<xs:element name="root"><xs:complexType><xs:sequence>'
        '<xs:element name="a" type="xs:string"/>'
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )

    INSTANCE = (
        f'<root xmlns="urn:m" xmlns:xsi="{XSI}" '
        'xsi:schemaLocation="urn:m missing.xsd">'
        "<a>hi</a></root>"
    )

    def test_missing_hint_is_warning_not_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        parser = _parse(
            self.MAIN_SCHEMA,
            self.INSTANCE,
            tmp_path,
        )
        # The hint resolves against the working directory (the instance
        # arrived as a stream, so it has no directory of its own) and
        # cannot be opened: a warning, never an error.
        assert "schema-hint" in _codes(parser)
        assert "import-unresolved" not in _codes(parser)
        assert not _errors(parser)

    def test_explicit_namespace_schemas_failure_stays_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        parser = _parse(
            self.MAIN_SCHEMA,
            '<root xmlns="urn:m"><a>hi</a></root>',
            tmp_path,
            namespace_schemas={"urn:o": str(tmp_path / "nosuch-explicit.xsd")},
        )
        unresolved = [
            issue
            for issue in parser.report.issues
            if issue.code == "import-unresolved" and issue.severity.name == "ERROR"
        ]
        assert unresolved
        assert "schema-hint" not in _codes(parser)

    def test_loadable_hint_still_splices(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "extra.xsd").write_text(
            f'<xs:schema xmlns:xs="{XSD}" targetNamespace="urn:o">'
            '<xs:element name="o" type="xs:string"/></xs:schema>'
        )
        parser = _parse(
            self.MAIN_SCHEMA,
            f'<root xmlns="urn:m" xmlns:xsi="{XSI}" '
            'xsi:schemaLocation="urn:m extra.xsd">'
            "<a>hi</a></root>",
            tmp_path,
        )
        assert parser.report.issues == []


class TestTransitiveSubstitutionDispatch:
    """A member may appear where any transitive head is declared."""

    SCHEMA = (
        f'<xs:schema xmlns:xs="{XSD}" xmlns="urn:sub" targetNamespace="urn:sub" '
        'elementFormDefault="qualified">'
        '<xs:element name="root"><xs:complexType><xs:sequence>'
        '<xs:element name="inner" minOccurs="0"><xs:complexType><xs:sequence>'
        '<xs:element ref="head" maxOccurs="unbounded"/>'
        "</xs:sequence></xs:complexType></xs:element>"
        '<xs:element name="midSlot" minOccurs="0"><xs:complexType><xs:sequence>'
        '<xs:element ref="mid" maxOccurs="unbounded"/>'
        "</xs:sequence></xs:complexType></xs:element>"
        "</xs:sequence></xs:complexType></xs:element>"
        '<xs:element name="head"><xs:complexType><xs:sequence>'
        '<xs:element name="payload" type="xs:string"/>'
        "</xs:sequence></xs:complexType></xs:element>"
        '<xs:element name="mid" substitutionGroup="head">'
        "<xs:complexType><xs:sequence>"
        '<xs:element name="payload" type="xs:string"/>'
        "</xs:sequence></xs:complexType></xs:element>"
        '<xs:element name="leaf" substitutionGroup="mid">'
        "<xs:complexType><xs:sequence>"
        '<xs:element name="payload" type="xs:string"/>'
        "</xs:sequence></xs:complexType></xs:element>"
        "</xs:schema>"
    )

    def test_transitive_member_admitted_under_root_head(self, tmp_path):
        instance = (
            '<root xmlns="urn:sub"><inner>'
            "<head><payload>x</payload></head>"
            "<mid><payload>x</payload></mid>"
            "<leaf><payload>x</payload></leaf>"
            "</inner></root>"
        )
        parser = _parse(self.SCHEMA, instance, tmp_path)
        assert parser.report.issues == []

    def test_member_admitted_under_own_declaration(self, tmp_path):
        instance = (
            '<root xmlns="urn:sub"><midSlot>'
            "<mid><payload>x</payload></mid>"
            "<leaf><payload>x</payload></leaf>"
            "</midSlot></root>"
        )
        parser = _parse(self.SCHEMA, instance, tmp_path)
        assert parser.report.issues == []

    def test_member_head_chain_admits_head_itself_repeatedly(self, tmp_path):
        instance = (
            '<root xmlns="urn:sub"><inner>'
            "<leaf><payload>x</payload></leaf>"
            "<head><payload>x</payload></head>"
            "<leaf><payload>y</payload></leaf>"
            "</inner></root>"
        )
        parser = _parse(self.SCHEMA, instance, tmp_path)
        assert parser.report.issues == []


@pytest.mark.parametrize("mode", [ParseModes.NAMESPACED, ParseModes.STRICT])
def test_legacy_mode_hint_failure_is_still_advisory(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    schema = (
        f'<xs:schema xmlns:xs="{XSD}" targetNamespace="urn:m" '
        'elementFormDefault="qualified">'
        '<xs:element name="root" type="xs:string"/></xs:schema>'
    )
    instance = (
        f'<root xmlns="urn:m" xmlns:xsi="{XSI}" xsi:schemaLocation="urn:m missing.xsd">hi</root>'
    )
    parser = _parse(schema, instance, tmp_path, mode=mode)
    if mode.namespaces == "strict":
        assert "schema-hint" in _codes(parser)
        assert "import-unresolved" not in _codes(parser)
    else:
        # Legacy mode does not consume extra hint pairs at all; the
        # parse must not fail either way.
        assert not _errors(parser)


class TestXsiTypeDispatch:
    """``xsi:type`` admission and QName lexical handling.

    An element with no declared type has the ur-type (``xs:anyType``) as
    its type, so an ``xsi:type`` naming any concrete type is valid
    (SUN typeDef01201m1, MS particlesIg001/002). The value is a QName,
    whose whitespace facet is *collapse*, so surrounding whitespace and
    newlines must not leak into the prefix (SUN typeDef00601m1).
    """

    UNTYPED_ROOT = (
        f'<xs:schema xmlns:xs="{XSD}"><xs:element name="root" nillable="true"/></xs:schema>'
    )

    def test_untyped_element_admits_simple_xsi_type(self, tmp_path):
        instance = (
            f'<root xmlns:xsi="{XSI}" xmlns:xsd="{XSD}" xsi:nil="true" xsi:type="xsd:string"/>'
        )
        parser = _parse(self.UNTYPED_ROOT, instance, tmp_path)
        assert not _errors(parser), [i.format() for i in parser.report.issues]

    def test_untyped_child_admits_simple_xsi_type(self, tmp_path):
        schema = (
            f'<xs:schema xmlns:xs="{XSD}">'
            '<xs:complexType name="base"><xs:choice><xs:element name="e2"/></xs:choice>'
            "</xs:complexType>"
            '<xs:element name="doc" type="base"/>'
            "</xs:schema>"
        )
        instance = f'<doc xmlns:xsi="{XSI}" xmlns:xsd="{XSD}"><e2 xsi:type="xsd:Name">a</e2></doc>'
        parser = _parse(schema, instance, tmp_path)
        assert not _errors(parser), [i.format() for i in parser.report.issues]

    def test_xsi_type_ws_collapsed(self, tmp_path):
        schema = (
            f'<xs:schema xmlns:xs="{XSD}">'
            '<xs:element name="root" type="xs:anySimpleType"/>'
            "</xs:schema>"
        )
        instance = (
            f'<root xmlns:xsi="{XSI}" xmlns:xsd="{XSD}" '
            'xsi:type="\n    xsd:boolean\n    ">true</root>'
        )
        parser = _parse(schema, instance, tmp_path)
        assert not _errors(parser), [i.format() for i in parser.report.issues]

    def test_declared_type_still_rejects_unrelated_xsi_type(self, tmp_path):
        schema = f'<xs:schema xmlns:xs="{XSD}"><xs:element name="r" type="xs:string"/></xs:schema>'
        instance = f'<r xmlns:xsi="{XSI}" xmlns:xsd="{XSD}" xsi:type="xsd:int">1</r>'
        parser = _parse(schema, instance, tmp_path)
        assert "xsi-type" in _codes(parser)

    def test_abstract_dynamic_type_is_reported(self, tmp_path):
        schema = (
            f'<xs:schema xmlns:xs="{XSD}">'
            '<xs:complexType name="abstractType" abstract="true">'
            '<xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
            "</xs:complexType>"
            '<xs:element name="root" type="abstractType"/>'
            "</xs:schema>"
        )
        instance = f'<root xmlns:xsi="{XSI}"><a>x</a></root>'
        parser = _parse(schema, instance, tmp_path)
        assert "abstract-type" in _codes(parser)
