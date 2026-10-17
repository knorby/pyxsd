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


#: A composed document's wildcard resolving ``##targetNamespace``: the
#: restriction's qualified local ``e`` lies in the declaring document's
#: own namespace, which the wildcard admits.
SIDE_WILDCARD = (
    f'<xs:schema {XS} targetNamespace="urn:side"'
    ' xmlns="urn:side" elementFormDefault="qualified">'
    '  <xs:complexType name="Base">'
    '    <xs:sequence><xs:any namespace="##targetNamespace"'
    ' processContents="lax" minOccurs="0" maxOccurs="unbounded"/></xs:sequence>'
    "  </xs:complexType>"
    '  <xs:complexType name="Derived">'
    '    <xs:complexContent><xs:restriction base="Base">'
    '      <xs:sequence><xs:element name="e" type="xs:string"/></xs:sequence>'
    "    </xs:restriction></xs:complexContent>"
    "  </xs:complexType>"
    "</xs:schema>"
)

#: The same shape with ``##other``: it excludes the declaring document's
#: namespace, so the base wildcard does *not* admit the qualified local
#: ``e`` — a genuine particle-restriction violation.
SIDE_WILDCARD_OTHER = SIDE_WILDCARD.replace('namespace="##targetNamespace"', 'namespace="##other"')

#: ``##other`` beside a local ``e`` whose alternative table differs from
#: the like-named global's.  The wildcard excludes the declaring
#: document's namespace, so the global declaration is not admitted and
#: the tighter-EDC check must not compare the two tables.
SIDE_OTHER_ALTERNATIVES = (
    f'<xs:schema {XS} targetNamespace="urn:side"'
    ' xmlns="urn:side" elementFormDefault="qualified">'
    '  <xs:element name="e" type="xs:string"/>'
    '  <xs:complexType name="Base">'
    "    <xs:sequence>"
    '      <xs:element name="e">'
    '        <xs:alternative test="0 = 1" type="xs:integer"/>'
    '        <xs:alternative type="xs:string"/>'
    "      </xs:element>"
    '      <xs:any namespace="##other" processContents="lax" minOccurs="0"/>'
    "    </xs:sequence>"
    "  </xs:complexType>"
    "</xs:schema>"
)

#: The admitting twin of the above: ``##any`` admits the global, so the
#: differing tables are a genuine element-consistent violation.
SIDE_ANY_ALTERNATIVES = SIDE_OTHER_ALTERNATIVES.replace('namespace="##other"', 'namespace="##any"')

MAIN_WILDCARD = (
    f'<xs:schema {XS} targetNamespace="urn:main"'
    ' xmlns="urn:main" xmlns:s="urn:side" elementFormDefault="qualified">'
    '  <xs:import namespace="urn:side" schemaLocation="side.xsd"/>'
    '  <xs:element name="root" type="s:Derived"/>'
    "</xs:schema>"
)

#: Open content in a composed document: the base's particle wildcard and
#: the restricting type's open-content wildcard are both declared in the
#: imported document, so ``##targetNamespace`` denotes ``urn:side`` on
#: both sides and the base particle covers the derived open content
#: (Saxon ``open022``'s reading, whose shape this is with a keyword).
SIDE_OPEN_CONTENT = (
    f'<xs:schema {XS} targetNamespace="urn:side"'
    ' xmlns="urn:side" elementFormDefault="qualified">'
    '  <xs:complexType name="Base">'
    '    <xs:sequence><xs:any namespace="##targetNamespace"'
    ' processContents="lax" minOccurs="0" maxOccurs="unbounded"/></xs:sequence>'
    "  </xs:complexType>"
    '  <xs:complexType name="Derived">'
    '    <xs:complexContent><xs:restriction base="Base">'
    '      <xs:openContent mode="interleave">'
    '        <xs:any namespace="##targetNamespace" processContents="lax"/>'
    "      </xs:openContent>"
    '      <xs:sequence><xs:element name="e" type="xs:string"/></xs:sequence>'
    "    </xs:restriction></xs:complexContent>"
    "  </xs:complexType>"
    "</xs:schema>"
)

#: ``open022``'s own shape (an empty derived particle) with the keyword
#: on both sides: the oracle judges this one.
SIDE_OPEN_CONTENT_OPEN022 = SIDE_OPEN_CONTENT.replace(
    '<xs:sequence><xs:element name="e" type="xs:string"/></xs:sequence>', "<xs:sequence/>"
)

#: The ``##other`` variant: both wildcards exclude the declaring
#: document's namespace, so the base particle still covers the derived
#: open content.  The derived particle is empty: a qualified ``e`` is
#: genuinely not admitted by a ``##other`` base wildcard.
SIDE_OPEN_CONTENT_OTHER = SIDE_OPEN_CONTENT.replace(
    'namespace="##targetNamespace"', 'namespace="##other"'
).replace('<xs:sequence><xs:element name="e" type="xs:string"/></xs:sequence>', "<xs:sequence/>")

#: Guard: the base particle admits only namespaces the derived open
#: content does not (and vice versa), so nothing covers it — the
#: restriction accepts documents the base rejects.
SIDE_OPEN_CONTENT_UNCOVERED = SIDE_OPEN_CONTENT.replace(
    'namespace="##targetNamespace"', 'namespace="##other"', 1
).replace('<xs:sequence><xs:element name="e" type="xs:string"/></xs:sequence>', "<xs:sequence/>")

#: Guard: the base declares no wildcard particle at all.
SIDE_OPEN_CONTENT_NO_PARTICLE = (
    f'<xs:schema {XS} targetNamespace="urn:side"'
    ' xmlns="urn:side" elementFormDefault="qualified">'
    '  <xs:complexType name="Base">'
    '    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
    "  </xs:complexType>"
    '  <xs:complexType name="Derived">'
    '    <xs:complexContent><xs:restriction base="Base">'
    '      <xs:openContent mode="interleave">'
    '        <xs:any namespace="##targetNamespace" processContents="lax"/>'
    "      </xs:openContent>"
    '      <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
    "    </xs:restriction></xs:complexContent>"
    "  </xs:complexType>"
    "</xs:schema>"
)

#: A no-namespace document whose default open content is keyword-spelled,
#: redefined by a namespaced schema: the adopted namespace lives in the
#: composition's record, not on the (unstamped) root.
SIDE_DEFAULT_OPEN_CONTENT = (
    f'<xs:schema {XS} elementFormDefault="qualified">'
    '  <xs:defaultOpenContent mode="interleave">'
    '    <xs:any namespace="##targetNamespace" processContents="lax"/>'
    "  </xs:defaultOpenContent>"
    '  <xs:complexType name="Base">'
    '    <xs:sequence><xs:any namespace="##targetNamespace"'
    ' processContents="lax" minOccurs="0" maxOccurs="unbounded"/></xs:sequence>'
    "  </xs:complexType>"
    '  <xs:complexType name="Derived">'
    '    <xs:complexContent><xs:restriction base="Base">'
    '      <xs:openContent mode="interleave">'
    '        <xs:any namespace="##targetNamespace" processContents="lax"/>'
    "      </xs:openContent>"
    "      <xs:sequence/>"
    "    </xs:restriction></xs:complexContent>"
    "  </xs:complexType>"
    "</xs:schema>"
)

#: The mismatch twin: the restricting type's open content excludes the
#: adopted namespace the default admits, so it is not a subset.
SIDE_DEFAULT_OPEN_CONTENT_MISMATCH = SIDE_DEFAULT_OPEN_CONTENT.replace(
    '<xs:openContent mode="interleave">        <xs:any namespace="##targetNamespace"',
    '<xs:openContent mode="interleave">        <xs:any namespace="##other"',
)

#: The nested twin: the no-namespace document ``main`` redefines is
#: itself a redefinition of another no-namespace document carrying the
#: keyword default open content. The middle document's root is left
#: unstamped by the outer redefinition, so the inner document's
#: adoption hangs on the adopted namespace being threaded through the
#: nested redefine dispatch too.
INNER_DEFAULT_OPEN_CONTENT = SIDE_DEFAULT_OPEN_CONTENT

BASE_REDEFINE_INNER = f'<xs:schema {XS}><xs:redefine schemaLocation="inner.xsd"/></xs:schema>'

MAIN_REDEFINE = (
    f'<xs:schema {XS} targetNamespace="urn:main"'
    ' xmlns="urn:main" elementFormDefault="qualified">'
    '  <xs:redefine schemaLocation="base.xsd"/>'
    '  <xs:element name="root" type="Derived"/>'
    "</xs:schema>"
)


class TestOpenContentNamespaceAcrossDocuments:
    """An open-content wildcard's keywords resolve against its own document.

    A restriction may add open content where the base type has none when
    a base particle's wildcard already admits the same names (Saxon
    ``open022``).  Both wildcards then live in the *same* document, so a
    keyword constraint — ``##targetNamespace`` or ``##other`` — denotes
    the same namespace on both sides.  Reading the derived open
    content's keyword against the merged namespace while the base
    particle's came from its declaring document made the two sides
    denote different namespaces and rejected the legal restriction.
    """

    def test_target_namespace_open_content_covered_by_particle(self, tmp_path):
        """The re-reviewer's shape: both keywords mean ``urn:side``."""
        schema = _compile(tmp_path, main=MAIN_WILDCARD, side=SIDE_OPEN_CONTENT)
        assert _codes(schema) == []

    def test_other_open_content_covered_by_particle(self, tmp_path):
        """The ``##other`` variant: both wildcards exclude ``urn:side``."""
        schema = _compile(tmp_path, main=MAIN_WILDCARD, side=SIDE_OPEN_CONTENT_OTHER)
        assert _codes(schema) == []

    def test_open022_shape_with_target_namespace_keyword(self, tmp_path):
        """``open022``'s own (empty-particle) shape, keyword-spelled.

        xmlschema accepts the single-document equivalent, so the
        composed form must compile clean as well.
        """
        schema = _compile(tmp_path, main=MAIN_WILDCARD, side=SIDE_OPEN_CONTENT_OPEN022)
        assert _codes(schema) == []

    def test_uncovered_open_content_still_reports(self, tmp_path):
        """Guard: a base particle that does not admit the open content."""
        schema = _compile(tmp_path, main=MAIN_WILDCARD, side=SIDE_OPEN_CONTENT_UNCOVERED)
        assert "particle-restriction" in _codes(schema)

    def test_base_without_wildcard_particle_still_reports(self, tmp_path):
        """Guard: nothing in the base covers the added open content."""
        schema = _compile(tmp_path, main=MAIN_WILDCARD, side=SIDE_OPEN_CONTENT_NO_PARTICLE)
        assert "particle-restriction" in _codes(schema)

    def test_redefined_default_open_content_uses_adopted_namespace(self, tmp_path):
        """A redefined no-namespace document's default open content.

        The document is adopted into the redefining schema's namespace
        without stamping its root, so its ``defaultOpenContent``
        keywords resolve against the composition's record of the
        namespace its components were spliced under.
        """
        (tmp_path / "base.xsd").write_text(SIDE_DEFAULT_OPEN_CONTENT)
        (tmp_path / "main.xsd").write_text(MAIN_REDEFINE)
        schema = Schema.compile(str(tmp_path / "main.xsd"))
        assert _codes(schema) == []

    def test_redefined_default_open_content_mismatch_still_reports(self, tmp_path):
        """Guard: the derived open content is not a subset of the default."""
        (tmp_path / "base.xsd").write_text(SIDE_DEFAULT_OPEN_CONTENT_MISMATCH)
        (tmp_path / "main.xsd").write_text(MAIN_REDEFINE)
        schema = Schema.compile(str(tmp_path / "main.xsd"))
        assert "particle-restriction" in _codes(schema)

    def test_nested_redefined_default_open_content_uses_adopted_namespace(self, tmp_path):
        """A redefined base that itself redefines its inner document.

        The middle document's root stays unstamped by the outer
        chameleon redefinition, so the inner document's keyword
        default open content resolves against the adopted namespace
        only when the redefine dispatch threads it down like the
        include dispatch does.
        """
        (tmp_path / "inner.xsd").write_text(INNER_DEFAULT_OPEN_CONTENT)
        (tmp_path / "base.xsd").write_text(BASE_REDEFINE_INNER)
        (tmp_path / "main.xsd").write_text(MAIN_REDEFINE)
        schema = Schema.compile(str(tmp_path / "main.xsd"))
        assert _codes(schema) == []

    def test_nested_redefined_default_open_content_mismatch_still_reports(self, tmp_path):
        """Guard: the nested twin of the mismatch still reports."""
        (tmp_path / "inner.xsd").write_text(SIDE_DEFAULT_OPEN_CONTENT_MISMATCH)
        (tmp_path / "base.xsd").write_text(BASE_REDEFINE_INNER)
        (tmp_path / "main.xsd").write_text(MAIN_REDEFINE)
        schema = Schema.compile(str(tmp_path / "main.xsd"))
        assert "particle-restriction" in _codes(schema)


class TestWildcardNamespaceAcrossDocuments:
    """A wildcard's ``##targetNamespace``/``##other`` resolve against its
    own document, not against the schema that composes it.

    Legacy namespace mode reports every composed document in the main
    schema's target namespace, and a wildcard's recorded target used to
    be read back that way while the element declarations it is compared
    against now carry their authored namespaces.  ``##targetNamespace``
    then excluded the very document that declared the wildcard and
    ``##other`` admitted it, in both directions: false
    particle-restriction reports for legal restrictions, missed ones for
    illegal restrictions, and wildcard/alternative-table comparisons the
    spec does not require (or the skipping of ones it does).
    """

    def test_target_namespace_wildcard_admits_its_own_document(self, tmp_path):
        """The wildcard admits the declaring document's own elements."""
        schema = _compile(tmp_path, main=MAIN_WILDCARD, side=SIDE_WILDCARD)
        assert _codes(schema) == []

    def test_other_wildcard_excludes_its_own_document(self, tmp_path):
        """``##other`` does not admit a global of the declaring document."""
        main = MAIN_WILDCARD.replace("s:Derived", "s:Base")
        schema = _compile(tmp_path, main=main, side=SIDE_OTHER_ALTERNATIVES)
        assert _codes(schema) == []

    def test_wildcard_that_excludes_the_element_still_reports(self, tmp_path):
        """Guard: ``##other`` does not admit the qualified local ``e``."""
        schema = _compile(tmp_path, main=MAIN_WILDCARD, side=SIDE_WILDCARD_OTHER)
        assert "particle-restriction" in _codes(schema)

    def test_wildcard_that_admits_the_name_still_compares(self, tmp_path):
        """Guard: ``##any`` admits the global, so the tables must match."""
        main = MAIN_WILDCARD.replace("s:Derived", "s:Base")
        schema = _compile(tmp_path, main=main, side=SIDE_ANY_ALTERNATIVES)
        assert "element-consistent" in _codes(schema)
