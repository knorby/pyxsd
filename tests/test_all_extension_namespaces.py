"""An all-extends-all extension compares particles by expanded name.

The composed ``all`` of an all-over-all extension holds the base's
particles followed by the extension's, and the overlap check used to
key them by bare local name while resolving namespaces through the
merged ``getNamespace()``: a legacy compile reports every composed
document in the main schema's target namespace, so a legal extension
pulling same-local-name elements from two different imported namespaces
was spuriously rejected as non-deterministic.  The two ``shared``
particles have distinct expanded names (``{urn:one}shared`` from the
base's qualified local, ``{urn:two}shared`` from the referenced global
or ``{urn:main}shared`` from the extension's own local), so the
composed ``all`` is deterministic.  xmlschema (the oracle) compiles both
shapes with no errors.

The guards pin the other direction: same-expanded-name declarations —
a base local and a same-document global of one namespace — still
collide (xmlschema rejects the shape with a UPA violation), and the
single-document Saxon all302 shape keeps reporting.
"""

from __future__ import annotations

from pyxsd import Schema

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

#: The base document: a qualified local ``shared`` inside an ``all``.
ONE = (
    f'<xs:schema {XS} targetNamespace="urn:one"'
    ' xmlns="urn:one" elementFormDefault="qualified">'
    '  <xs:complexType name="Base">'
    '    <xs:all><xs:element name="shared" type="xs:string"/></xs:all>'
    "  </xs:complexType>"
    "</xs:schema>"
)

#: The conflict twin: the same document also declares a *global*
#: ``shared``, so the base's local and the extension's reference name
#: one expanded name, ``{urn:one}shared``.
ONE_GLOBAL = ONE.replace(
    "</xs:schema>",
    '  <xs:element name="shared" type="xs:string"/></xs:schema>',
)

#: A second imported namespace with its own global ``shared``.
TWO = (
    f'<xs:schema {XS} targetNamespace="urn:two"'
    ' xmlns="urn:two" elementFormDefault="qualified">'
    '  <xs:element name="shared" type="xs:string"/>'
    "</xs:schema>"
)

MAIN_REF = (
    f'<xs:schema {XS} targetNamespace="urn:main"'
    ' xmlns="urn:main" xmlns:o="urn:one" xmlns:t="urn:two"'
    ' elementFormDefault="qualified">'
    '  <xs:import namespace="urn:one" schemaLocation="one.xsd"/>'
    '  <xs:import namespace="urn:two" schemaLocation="two.xsd"/>'
    '  <xs:complexType name="Ext">'
    '    <xs:complexContent><xs:extension base="o:Base">'
    '      <xs:all><xs:element ref="t:shared"/></xs:all>'
    "    </xs:extension></xs:complexContent>"
    "  </xs:complexType>"
    '  <xs:element name="root" type="Ext"/>'
    "</xs:schema>"
)

#: The same extension with a local declaration of its own document's
#: namespace instead of a reference.
MAIN_LOCAL = (
    f'<xs:schema {XS} targetNamespace="urn:main"'
    ' xmlns="urn:main" xmlns:o="urn:one" elementFormDefault="qualified">'
    '  <xs:import namespace="urn:one" schemaLocation="one.xsd"/>'
    '  <xs:complexType name="Ext">'
    '    <xs:complexContent><xs:extension base="o:Base">'
    '      <xs:all><xs:element name="shared" type="xs:string"/></xs:all>'
    "    </xs:extension></xs:complexContent>"
    "  </xs:complexType>"
    '  <xs:element name="root" type="Ext"/>'
    "</xs:schema>"
)

#: The guard: the extension references the base document's own global,
#: which shares the base local's expanded name.
MAIN_CONFLICT = MAIN_REF.replace('<xs:element ref="t:shared"/>', '<xs:element ref="o:shared"/>')

#: The single-document Saxon all302 shape (no composition at all).
SINGLE_DOCUMENT = (
    f"<xs:schema {XS}>"
    "  <xs:complexType name='b'><xs:all>"
    "<xs:element name='a'/><xs:element name='c'/>"
    "  </xs:all></xs:complexType>"
    "  <xs:complexType name='t'><xs:complexContent>"
    "<xs:extension base='b'><xs:all>"
    "<xs:element name='e'/><xs:element name='c'/>"
    "  </xs:all></xs:extension></xs:complexContent></xs:complexType>"
    "</xs:schema>"
)


def _compile(tmp_path, main, one=ONE, two=TWO):
    (tmp_path / "one.xsd").write_text(one)
    if two is not None:
        (tmp_path / "two.xsd").write_text(two)
    (tmp_path / "main.xsd").write_text(main)
    return Schema.compile(str(tmp_path / "main.xsd"))


def _codes(schema):
    return [error.code for error in schema.report.errors]


class TestAllExtensionExpandedNames:
    def test_cross_namespace_reference_extension_is_clean(self, tmp_path):
        """``{urn:one}shared`` and ``{urn:two}shared`` are distinct names."""
        schema = _compile(tmp_path, MAIN_REF)
        assert _codes(schema) == []

    def test_cross_namespace_local_extension_is_clean(self, tmp_path):
        """``{urn:one}shared`` and ``{urn:main}shared`` are distinct names."""
        schema = _compile(tmp_path, MAIN_LOCAL, two=None)
        assert _codes(schema) == []

    def test_same_expansion_across_sides_still_reports(self, tmp_path):
        """The base local and the same document's global share a name."""
        schema = _compile(tmp_path, MAIN_CONFLICT, one=ONE_GLOBAL)
        assert "particle-restriction" in _codes(schema)

    def test_single_document_repeat_still_reports(self, tmp_path):
        """The Saxon all302 shape (one document) keeps reporting."""
        (tmp_path / "main.xsd").write_text(SINGLE_DOCUMENT)
        schema = Schema.compile(str(tmp_path / "main.xsd"))
        assert "particle-restriction" in _codes(schema)
