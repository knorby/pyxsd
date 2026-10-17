"""Element declarations from composed documents stay distinct by namespace.

Two composed documents may declare types whose names differ only by case
(``Foo_Bar`` in an imported namespace, ``Foo_bar`` in the main one) and
each declare an element of the same local name (``e``) against its own
type.  A content model that pulls one particle from each document — a
type in the imported document referencing a group of the main one, the
shape ``shared-math.xsd``'s ``CT_R`` has in the WordprocessingML corpus
— is deterministic and consistent: the two ``e`` particles carry
different *expanded* names (``{urn:side}e`` and ``{urn:main}e``), so
Element Declarations Consistent (cos-element-consistent) does not apply
and the particles cannot be confused with one another.

Legacy namespace mode merges every composed document into the main
schema's target namespace, and the content-model sweeps used to read a
declaration's namespace back through ``getNamespace()``: both particles
reported ``{urn:main}`` and the checks conflated them — the EDC sweep
reported the (correctly different) types as conflicting and the UPA
sweep reported the pair as ambiguous.  The sweeps now compare the
namespace of the document that declared each particle, so the pair is
seen for what it is.  xmlschema (the oracle) compiles the shape with no
errors.

The guards below pin the other direction: same-expanded-name
declarations still collide across an ``xs:include`` (one namespace by
definition), a genuine conflicting-type model still reports, and a
genuine UPA violation (a repeated particle) still reports.
"""

from __future__ import annotations

from pyxsd import Schema

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

#: The imported document: its ``CT_R`` mirrors ``shared-math.xsd``'s —
#: a local ``e`` of its own type, then a reference to the main
#: document's group, which declares a same-named ``e`` of *its* type.
SIDE = (
    f'<xs:schema {XS} targetNamespace="urn:side"'
    ' xmlns="urn:side" xmlns:m="urn:main" elementFormDefault="qualified">'
    '  <xs:import namespace="urn:main" schemaLocation="main.xsd"/>'
    '  <xs:complexType name="Foo_Bar">'
    '    <xs:sequence><xs:element name="x" type="xs:string"/></xs:sequence>'
    "  </xs:complexType>"
    '  <xs:complexType name="CT_R">'
    "    <xs:sequence>"
    '      <xs:element name="e" type="Foo_Bar" minOccurs="0"/>'
    '      <xs:group ref="m:Group" minOccurs="0"/>'
    "    </xs:sequence>"
    "  </xs:complexType>"
    "</xs:schema>"
)

MAIN = (
    f'<xs:schema {XS} targetNamespace="urn:main"'
    ' xmlns="urn:main" xmlns:s="urn:side" elementFormDefault="qualified">'
    '  <xs:import namespace="urn:side" schemaLocation="side.xsd"/>'
    '  <xs:complexType name="Foo_bar">'
    '    <xs:sequence><xs:element name="y" type="xs:string"/></xs:sequence>'
    "  </xs:complexType>"
    '  <xs:group name="Group">'
    '    <xs:sequence><xs:element name="e" type="Foo_bar" minOccurs="0"/></xs:sequence>'
    "  </xs:group>"
    '  <xs:element name="root" type="s:CT_R"/>'
    "</xs:schema>"
)


def _compile(tmp_path, main=MAIN, side=SIDE, **kwargs):
    (tmp_path / "side.xsd").write_text(side)
    (tmp_path / "main.xsd").write_text(main)
    return Schema.compile(str(tmp_path / "main.xsd"), **kwargs)


def _codes(schema):
    return [error.code for error in schema.report.errors]


class TestComposedDocumentsStayDistinct:
    def test_case_distinct_types_do_not_conflict(self, tmp_path):
        """The two ``e`` declarations have different expanded names."""
        schema = _compile(tmp_path)
        assert _codes(schema) == []

    def test_form_distinct_locals_do_not_conflict(self, tmp_path):
        """An unqualified local and a qualified global share no name.

        ``e`` declared unqualified has the absent namespace; the global
        ``e`` has the target namespace.  They are different expanded
        names, so the declarations-consistent rule does not compare
        their types (particlesL010/L030's reading).
        """
        main = (
            f'<xs:schema {XS} targetNamespace="urn:main"'
            ' xmlns="urn:main" elementFormDefault="unqualified">'
            '  <xs:complexType name="Foo_bar">'
            '    <xs:sequence><xs:element name="y" type="xs:string"/></xs:sequence>'
            "  </xs:complexType>"
            '  <xs:complexType name="Foo_Bar">'
            '    <xs:sequence><xs:element name="x" type="xs:string"/></xs:sequence>'
            "  </xs:complexType>"
            '  <xs:element name="e" type="Foo_Bar"/>'
            '  <xs:element name="root"><xs:complexType><xs:sequence>'
            '    <xs:element name="e" type="Foo_bar"/>'
            '    <xs:element ref="e"/>'
            "  </xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        schema = _compile(tmp_path, main=main)
        assert _codes(schema) == []


class TestRejectionNetStaysClosed:
    def test_conflicting_types_in_one_document_still_report(self, tmp_path):
        """Same expanded name, different types: EDC still reports.

        Both declarations live in the main document, so the two ``e``
        particles share the target namespace and the sweep must compare
        their types.
        """
        main = (
            f'<xs:schema {XS} targetNamespace="urn:main"'
            ' xmlns="urn:main" elementFormDefault="qualified">'
            '  <xs:complexType name="Foo_bar">'
            '    <xs:sequence><xs:element name="y" type="xs:string"/></xs:sequence>'
            "  </xs:complexType>"
            '  <xs:complexType name="Foo_Bar">'
            '    <xs:sequence><xs:element name="x" type="xs:string"/></xs:sequence>'
            "  </xs:complexType>"
            '  <xs:element name="root"><xs:complexType><xs:sequence>'
            '    <xs:element name="e" type="Foo_bar"/>'
            '    <xs:element name="e" type="Foo_Bar"/>'
            "  </xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        schema = _compile(tmp_path, main=main)
        assert "all-rule" in _codes(schema)

    def test_same_namespace_across_an_include_still_reports(self, tmp_path):
        """An include shares the namespace, so the declarations collide.

        The included document declares its ``e`` against its own type in
        the same target namespace as the includer's: one expanded name,
        two types, and the consistency rule applies.
        """
        side = (
            f'<xs:schema {XS} xmlns="urn:main" elementFormDefault="qualified">'
            '  <xs:complexType name="Foo_Bar">'
            '    <xs:sequence><xs:element name="x" type="xs:string"/></xs:sequence>'
            "  </xs:complexType>"
            '  <xs:group name="Group">'
            '    <xs:sequence><xs:element name="e" type="Foo_Bar"/></xs:sequence>'
            "  </xs:group>"
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS} targetNamespace="urn:main"'
            ' xmlns="urn:main" elementFormDefault="qualified">'
            '  <xs:include schemaLocation="side.xsd"/>'
            '  <xs:complexType name="Foo_bar">'
            '    <xs:sequence><xs:element name="y" type="xs:string"/></xs:sequence>'
            "  </xs:complexType>"
            '  <xs:element name="root"><xs:complexType><xs:sequence>'
            '    <xs:element name="e" type="Foo_bar"/>'
            '    <xs:group ref="Group"/>'
            "  </xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        schema = _compile(tmp_path, main=main, side=side)
        assert "all-rule" in _codes(schema)

    def test_repeated_particle_still_reports_upa(self, tmp_path):
        """A repeated particle is still ambiguous (cos-nonambig)."""
        main = (
            f'<xs:schema {XS} targetNamespace="urn:main"'
            ' xmlns="urn:main" elementFormDefault="qualified">'
            '  <xs:element name="e" type="xs:string"/>'
            '  <xs:element name="root"><xs:complexType><xs:sequence>'
            '    <xs:element ref="e" maxOccurs="unbounded"/>'
            '    <xs:element ref="e"/>'
            "  </xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        schema = _compile(tmp_path, main=main)
        assert "upa" in _codes(schema)

    def test_repeated_cross_document_ref_still_reports_upa(self, tmp_path):
        """Two references to one imported declaration share its name.

        The authored namespace keeps a declaration of the imported
        document distinct from a same-local-name one of the main
        document; it must not make two references to the *same*
        imported declaration distinct.
        """
        side = (
            f'<xs:schema {XS} targetNamespace="urn:side"'
            ' xmlns="urn:side" elementFormDefault="qualified">'
            '  <xs:element name="e" type="xs:string"/>'
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS} targetNamespace="urn:main"'
            ' xmlns="urn:main" xmlns:s="urn:side" elementFormDefault="qualified">'
            '  <xs:import namespace="urn:side" schemaLocation="side.xsd"/>'
            '  <xs:element name="root"><xs:complexType><xs:sequence>'
            '    <xs:element ref="s:e" maxOccurs="unbounded"/>'
            '    <xs:element ref="s:e"/>'
            "  </xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        schema = _compile(tmp_path, main=main, side=side)
        assert "upa" in _codes(schema)
