"""Tests for schema composition (Phase 9).

Covers ``xs:include`` (including the chameleon case where the
included schema declares no target namespace), ``xs:import``
(namespace-light merging), ``xs:redefine`` (derived redefinition of
included components) and the error paths (missing files, cycles,
namespace mismatches, malformed included schemas).
"""

from io import StringIO

import pytest

from conftest import run_parser
from pyxsd.binding import ParseModes
from pyxsd.element_representatives.element_representative import (
    ElementRepresentative,
    registry,
)
from pyxsd.schema import Schema
from pyxsd.validation import IssueSeverity

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


def _parse_text(schema, instance="<r/>"):
    """Compiles an inline schema and binds an inline instance from strings."""
    return Schema.compile(StringIO(schema)).parse(StringIO(instance))


def _schema_errors(report):
    return [issue for issue in report.for_phase("schema") if issue.severity is IssueSeverity.ERROR]


def _schema_warning_codes(report):
    return {
        issue.code
        for issue in report.for_phase("schema")
        if issue.severity is IssueSeverity.WARNING
    }


class TestParserLocalRegistry:
    """Declarations from one parse must not leak into another (R14)."""

    def test_second_parser_does_not_reuse_first_parsers_types(self):
        first = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="B"><xs:sequence>'
            '<xs:element name="old" type="xs:string"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:element name="r1" type="B"/>'
            "</xs:schema>",
            "<r1><old/></r1>",
        )
        assert not first.report.has_errors

        second = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="B"><xs:sequence>'
            '<xs:element name="new" type="xs:string"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:complexType name="D"><xs:complexContent>'
            '<xs:extension base="B"/>'
            "</xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="D"/>'
            "</xs:schema>",
            "<r><new/></r>",
        )
        # The second schema's D extends the second schema's B, not the
        # first schema's B (which expected 'old').
        assert not second.report.has_errors

    def test_element_and_type_may_share_a_name(self):
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:element name="T" type="xs:string"/>'
            '<xs:complexType name="T"><xs:sequence>'
            '<xs:element name="v" type="xs:string"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:complexType name="D"><xs:complexContent>'
            '<xs:extension base="T"/>'
            "</xs:complexContent></xs:complexType>"
            '<xs:element name="r" type="D"/>'
            "</xs:schema>",
            "<r><v/></r>",
        )
        assert not doc.report.has_errors
        assert "D" in doc.schema.classes
        base_names = [base.__name__ for base in doc.schema.classes["D"].__mro__]
        assert "T" in base_names


class TestComposeFixture:
    """The compose fixture: include + redefine + import in one schema."""

    @pytest.fixture()
    def doc(self):
        return run_parser("compose")

    def test_included_types_become_classes(self, doc):
        assert "commonType" in doc.schema.classes
        assert "unitType" in doc.schema.classes

    def test_redefine_creates_derived_class(self, doc):
        assert "widgetType" in doc.schema.classes
        widget_cls = doc.schema.classes["widgetType"]
        element_names = [descriptor.name for descriptor in widget_cls()._getElements()]
        assert element_names == ["name", "count"]
        assert list(widget_cls._elementNames_) == ["count"]

    def test_redefine_keeps_original_under_base_name(self, doc):
        original = ElementRepresentative.getFromName("widgetType|base")
        assert original is not None
        assert original.name == "widgetType|base"

    def test_redefined_class_derives_from_original(self, doc):
        widget_cls = doc.schema.classes["widgetType"]
        base_names = [base.__name__ for base in widget_cls.__mro__]
        assert "widgetType|base" in base_names

    def test_imported_types_available(self, doc):
        assert "extraType" in doc.schema.classes

    def test_valid_parse_has_clean_report(self, doc):
        assert not doc.report.has_errors

    def test_chameleon_include_components_are_used(self, doc):
        """The chameleon include's simpleType works as an attribute type."""
        assert registry.get("unitType") is not None

    def test_composition_tags_are_removed_from_schema_tree(self, doc):
        schema_er = registry["schema"][0]
        root_tags = {child.tag.split("}")[-1] for child in schema_er.xsdElement}
        assert "include" not in root_tags
        assert "redefine" not in root_tags
        assert "import" not in root_tags


class TestCompositionErrors:
    """Error paths of the composition machinery."""

    def test_missing_include_file_is_warning(self, tmp_path):
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="no-such-file.xsd"/>\n'
            '  <xs:element name="root"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text("<root/>")
        doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))
        # XSD treats an unresolvable schemaLocation as a hint, not an
        # error: the schema is still valid and only a warning is raised.
        assert not _schema_errors(doc.report)
        assert "schema-compose" in _schema_warning_codes(doc.report)

    def test_malformed_include_is_reported(self, tmp_path):
        (tmp_path / "bad.xsd").write_text("<xs:schema><not closed")
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="bad.xsd"/>\n'
            '  <xs:element name="root"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text("<root/>")
        doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))
        codes = [issue.code for issue in doc.report.errors]
        assert "schema-compose" in codes

    def test_include_cycle_is_deduplicated(self, tmp_path):
        (tmp_path / "a.xsd").write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="b.xsd"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "b.xsd").write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="a.xsd"/>\n'
            "</xs:schema>\n"
        )
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="a.xsd"/>\n'
            '  <xs:element name="root"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text("<root/>")
        doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))
        # A legal include cycle is deduplicated rather than rejected:
        # the schema composes cleanly.
        assert not doc.report.has_errors

    def test_include_namespace_mismatch_is_reported(self, tmp_path):
        (tmp_path / "other.xsd").write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'targetNamespace="urn:other">\n'
            "</xs:schema>\n"
        )
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:include schemaLocation="other.xsd"/>\n'
            '  <xs:element name="root"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text("<root/>")
        doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))
        codes = [issue.code for issue in doc.report.errors]
        assert "compose-namespace" in codes

    def test_import_of_schema_namespace_is_skipped(self, tmp_path):
        schema = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:import namespace="http://www.w3.org/2001/XMLSchema"/>\n'
            '  <xs:element name="root"/>\n'
            "</xs:schema>\n"
        )
        (tmp_path / "schema.xsd").write_text(schema)
        (tmp_path / "instance.xml").write_text("<root/>")
        doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))
        assert len(doc.report) == 0


# ---------------------------------------------------------------------------
# Standalone include/import against hand-written schemas


class TestCompositionCorrectness:
    """R14 composition defects fixed alongside the per-parse component table."""

    def test_element_ref_inside_a_group_resolves(self):
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:element name="a" type="xs:string"/>'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element ref="a"/>'
            "</xs:sequence></xs:group>"
            '<xs:element name="r"><xs:complexType>'
            '<xs:group ref="g"/>'
            "</xs:complexType></xs:element>"
            "</xs:schema>",
            "<r><a>x</a></r>",
        )
        assert not doc.report.has_errors
        root = doc.root
        # The ref site adopts the global declaration's name, so the
        # child binds to the ``a`` descriptor.
        assert [descriptor.name for descriptor in root._getElements()] == ["a"]
        assert root.a == "x"

    def test_nested_attribute_group_references_merge(self):
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:attributeGroup name="g1">'
            '<xs:attribute name="a" type="xs:int"/>'
            "</xs:attributeGroup>"
            '<xs:attributeGroup name="g2">'
            '<xs:attributeGroup ref="g1"/>'
            "</xs:attributeGroup>"
            '<xs:element name="r"><xs:complexType>'
            '<xs:attributeGroup ref="g2"/>'
            "</xs:complexType></xs:element>"
            "</xs:schema>",
            '<r a="5"/>',
        )
        assert not doc.report.has_errors
        assert doc.root._attribs_["a"] == "5"

    def test_circular_attribute_group_is_accepted(self):
        # attgC010: XSD 1.1 allows circular attribute group definitions.
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="test"><xs:attributeGroup ref="test"/></xs:complexType>'
            '<xs:attributeGroup name="test">'
            '<xs:attributeGroup ref="test"/>'
            '<xs:attribute name="foo" type="xs:int"/>'
            "</xs:attributeGroup>"
            '<xs:element name="T" type="test"/>'
            "</xs:schema>",
            '<T foo="3"/>',
        )
        assert not doc.report.has_errors
        assert doc.root._attribs_["foo"] == "3"

    def test_import_without_schema_location_is_allowed(self):
        doc = _parse_text(
            f"<xs:schema {XS}>"
            '<xs:import namespace="urn:unused"/>'
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>",
            "<r>hi</r>",
        )
        assert not doc.report.has_errors

    def test_single_level_group_redefine(self, tmp_path):
        (tmp_path / "base.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/>'
            "</xs:sequence></xs:group>"
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:group ref="g"/>'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:sequence></xs:group>"
            "</xs:redefine>"
            '<xs:element name="r"><xs:complexType>'
            '<xs:group ref="g"/>'
            "</xs:complexType></xs:element>"
            "</xs:schema>"
        )
        (tmp_path / "instance.xml").write_text("<r><a/><b/></r>")
        doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))
        assert not doc.report.has_errors

    def test_single_level_attribute_group_redefine(self, tmp_path):
        (tmp_path / "base.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:attributeGroup name="ag">'
            '<xs:attribute name="a" type="xs:string"/>'
            "</xs:attributeGroup>"
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:attributeGroup name="ag">'
            '<xs:attributeGroup ref="ag"/>'
            '<xs:attribute name="b" type="xs:string"/>'
            "</xs:attributeGroup>"
            "</xs:redefine>"
            '<xs:element name="r"><xs:complexType>'
            '<xs:attributeGroup ref="ag"/>'
            "</xs:complexType></xs:element>"
            "</xs:schema>"
        )
        (tmp_path / "instance.xml").write_text('<r a="1" b="2"/>')
        doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))
        assert not doc.report.has_errors
        assert doc.root._attribs_ == {"a": "1", "b": "2"}

    def test_chameleon_include_base_reference_resolves(self, tmp_path):
        """A chameleon document's own type reference resolves after adoption.

        The included document declares no targetNamespace, so it is
        absorbed into the including schema's namespace and its unprefixed
        references are rewritten into that namespace (Appendix F.1). The
        rewritten (Clark-spelled) base reference must still resolve
        against the composed declarations.
        """
        (tmp_path / "child.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="baseType">'
            '<xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
            "</xs:complexType>"
            '<xs:complexType name="derivedType"><xs:complexContent>'
            '<xs:extension base="baseType">'
            '<xs:sequence><xs:element name="b" type="xs:string"/></xs:sequence>'
            "</xs:extension></xs:complexContent></xs:complexType>"
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:cyc" xmlns:c="urn:cyc">'
            '  <xs:include schemaLocation="child.xsd"/>'
            '  <xs:element name="r" type="derivedType"/>'
            "</xs:schema>"
        )
        (tmp_path / "instance.xml").write_text("<r><a>x</a><b>y</b></r>")
        doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))
        assert not doc.report.has_errors
        assert "derivedType" in doc.schema.classes
        base_names = [base.__name__ for base in doc.schema.classes["derivedType"].__mro__]
        assert "baseType" in base_names

    def test_legacy_clark_group_reference_uses_authored_namespace(self, tmp_path):
        """A legacy import's adopted Clark group reference finds its group.

        ``imp.xsd`` chameleon-includes ``child.xsd``, so the group
        reference written in ``child.xsd`` is rewritten to
        ``{urn:imp}g``. Legacy composition reports the imported group in
        the main schema's namespace (``urn:main``), so selecting
        candidates by the reported namespace rejects the reference even
        though the group's recorded authored namespace is its target.
        The included document declares ``elementFormDefault`` so the
        adopted group's local element is qualified in the instance.
        """
        (tmp_path / "child.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/>'
            "</xs:sequence></xs:group>"
            '<xs:complexType name="t"><xs:sequence>'
            '<xs:group ref="g"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:element name="root" type="t"/>'
            "</xs:schema>"
        )
        (tmp_path / "imp.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:imp" xmlns="urn:imp">'
            '  <xs:include schemaLocation="child.xsd"/>'
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:main" xmlns:i="urn:imp">'
            '  <xs:import namespace="urn:imp" schemaLocation="imp.xsd"/>'
            "</xs:schema>"
        )
        (tmp_path / "instance.xml").write_text('<root xmlns="urn:imp"><a>x</a></root>')
        for mode in (None, ParseModes.NAMESPACED):
            kwargs = {} if mode is None else {"mode": mode}
            schema = Schema.compile(str(tmp_path / "schema.xsd"), **kwargs)
            assert not schema.report.has_errors
            assert not schema.parse(str(tmp_path / "instance.xml")).report.has_errors

    def test_chameleon_redefine_base_references_resolve(self, tmp_path):
        """A chameleon redefine's base document resolves its own references.

        The redefined document declares no targetNamespace, so it is
        absorbed into the redefining schema's namespace exactly as a
        chameleon include is (XSD 1.1 §4.2.4 with Appendix F.1): its
        unprefixed QName references are rewritten into that namespace.
        Both the group reference and the attribute reference written in
        the *base* document must resolve against the adopted components.
        """
        (tmp_path / "base.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '<xs:attribute name="base-a" type="xs:string"/>'
            '<xs:group name="g1">'
            '<xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
            "</xs:group>"
            '<xs:complexType name="baseType">'
            '<xs:sequence><xs:group ref="g1"/></xs:sequence>'
            '<xs:attribute ref="base-a"/>'
            "</xs:complexType>"
            '<xs:element name="base-e" type="baseType"/>'
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:redef" xmlns="urn:redef"'
            ' elementFormDefault="qualified">'
            '  <xs:redefine schemaLocation="base.xsd">'
            '    <xs:group name="g1"><xs:sequence>'
            '      <xs:group ref="g1"/>'
            '      <xs:element name="b" type="xs:string"/>'
            "    </xs:sequence></xs:group>"
            "  </xs:redefine>"
            '  <xs:complexType name="mainType"><xs:group ref="g1"/></xs:complexType>'
            '  <xs:element name="root" type="mainType"/>'
            "</xs:schema>"
        )
        # Legacy mode matches by local name.
        (tmp_path / "legacy.xml").write_text('<base-e base-a="1"><a>x</a><b>y</b></base-e>')
        schema = Schema.compile(str(tmp_path / "schema.xsd"))
        assert [issue.code for issue in schema.report.errors] == []
        doc = schema.parse(str(tmp_path / "legacy.xml"))
        assert not doc.report.has_errors

        # Strict mode addresses the adopted components by expanded name.
        (tmp_path / "strict.xml").write_text(
            '<base-e xmlns="urn:redef" xmlns:r="urn:redef" r:base-a="1"><a>x</a><b>y</b></base-e>'
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert [issue.code for issue in schema.report.errors] == []
        doc = schema.parse(str(tmp_path / "strict.xml"))
        assert not doc.report.has_errors
        assert doc.root._attribs_["{urn:redef}base-a"] == "1"

    def test_chameleon_redefine_nested_include_references_resolve(self, tmp_path):
        """A redefined document's own chameleon include adopts as well.

        ``base.xsd`` (no targetNamespace) is redefined by a namespaced
        schema and *includes* ``inner.xsd`` (also no targetNamespace),
        which declares the group ``base.xsd``'s type references. The
        whole include chain is absorbed into the redefining schema's
        namespace (Appendix F.1 applies through it), so the reference
        rewritten in ``base.xsd`` must reach the included declaration --
        which only holds when the nested include adopts the namespace
        too.
        """
        (tmp_path / "inner.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '<xs:group name="g3">'
            '<xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>'
            "</xs:group>"
            "</xs:schema>"
        )
        (tmp_path / "base.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '  <xs:include schemaLocation="inner.xsd"/>'
            '<xs:group name="g1">'
            '<xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
            "</xs:group>"
            '<xs:complexType name="baseType"><xs:group ref="g3"/></xs:complexType>'
            '<xs:element name="base-e" type="baseType"/>'
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:redef" xmlns="urn:redef"'
            ' elementFormDefault="qualified">'
            '  <xs:redefine schemaLocation="base.xsd">'
            '    <xs:group name="g1"><xs:sequence>'
            '      <xs:group ref="g1"/>'
            '      <xs:element name="b" type="xs:string"/>'
            "    </xs:sequence></xs:group>"
            "  </xs:redefine>"
            '  <xs:element name="root" type="baseType"/>'
            "</xs:schema>"
        )
        (tmp_path / "instance.xml").write_text('<base-e xmlns="urn:redef"><c>x</c></base-e>')
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert [issue.code for issue in schema.report.errors] == []
        doc = schema.parse(str(tmp_path / "instance.xml"))
        assert not doc.report.has_errors

        # Legacy mode matches by local name; the adopted chain is not
        # otherwise disturbed.
        (tmp_path / "legacy.xml").write_text("<base-e><c>x</c></base-e>")
        schema = Schema.compile(str(tmp_path / "schema.xsd"))
        assert [issue.code for issue in schema.report.errors] == []
        doc = schema.parse(str(tmp_path / "legacy.xml"))
        assert not doc.report.has_errors

    def test_chameleon_rewrite_produces_clark_literals(self, tmp_path):
        """The adoption rewrite itself, pinned on the composed tree.

        The outcome tests above would keep passing if ``base.xsd``'s
        namespace-less references stopped being rewritten (a lexical
        lookup would find ``inner.xsd``'s declarations anyway), so this
        test asserts the mechanism: every namespace-less reference in
        the adopted no-namespace documents is rewritten to the adopting
        namespace's Clark form, while prefixed references into the XML
        Schema namespace stay untouched.
        """
        (tmp_path / "inner.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '<xs:group name="g3">'
            '<xs:sequence><xs:element name="c" type="xs:string"/></xs:sequence>'
            "</xs:group>"
            "</xs:schema>"
        )
        (tmp_path / "base.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '  <xs:include schemaLocation="inner.xsd"/>'
            '<xs:complexType name="baseType"><xs:group ref="g3"/></xs:complexType>'
            '<xs:element name="base-e" type="baseType"/>'
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:redef" xmlns="urn:redef"'
            ' elementFormDefault="qualified">'
            '  <xs:redefine schemaLocation="base.xsd"/>'
            '  <xs:element name="root" type="baseType"/>'
            "</xs:schema>"
        )
        Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        composed = registry["schema"][0].xsdElement
        group_refs = {child.get("ref") for child in composed.iter() if child.tag.endswith("}group")}
        # The adopted reference is the Clark literal; the bare lexical
        # form is gone.
        assert "{urn:redef}g3" in group_refs
        assert "g3" not in group_refs
        type_refs = {
            child.get("type")
            for child in composed.iter()
            if child.tag.endswith("}element") and child.get("type") is not None
        }
        assert "{urn:redef}baseType" in type_refs
        # Prefixed references into the XML Schema namespace are NOT
        # rewritten by adoption.
        assert "xs:string" in type_refs

    def test_adopted_nested_circular_redefine_stays_a_warning(self, tmp_path):
        """An adopted base's nested circular redefine keeps schU1's lenient verdict.

        The corpus schU1 case (a same-namespace circular redefine pair)
        is a warning, not an error: the re-entered document's
        declarations are already in the composition tree, so the cycle
        is skipped leniently. The adopted variant here -- the circular
        pair lives in no-namespace documents that a namespaced schema
        chameleon-adopts through its own ``xs:redefine`` -- must keep
        that verdict: the leniency keys on the document's *own* root
        namespace, not the adopting one, so adoption must not turn the
        cycle into a hard error.
        """
        (tmp_path / "b.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '  <xs:redefine schemaLocation="a.xsd">'
            '    <xs:attributeGroup name="a-g1">'
            '      <xs:attribute name="a1" type="xs:int"/>'
            '      <xs:attribute name="a2" type="xs:int"/>'
            "    </xs:attributeGroup>"
            "  </xs:redefine>"
            '  <xs:attributeGroup name="b-g1">'
            '    <xs:attribute name="b1"/>'
            '    <xs:attribute name="b2"/>'
            "  </xs:attributeGroup>"
            "</xs:schema>"
        )
        (tmp_path / "a.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '  <xs:redefine schemaLocation="b.xsd">'
            '    <xs:attributeGroup name="b-g1">'
            '      <xs:attribute name="b1" type="xs:int"/>'
            '      <xs:attribute name="b2" type="xs:int"/>'
            "    </xs:attributeGroup>"
            "  </xs:redefine>"
            '  <xs:attributeGroup name="a-g1">'
            '    <xs:attribute name="a1"/>'
            '    <xs:attribute name="a2"/>'
            "  </xs:attributeGroup>"
            '  <xs:complexType name="a-ct">'
            '    <xs:attributeGroup ref="b-g1"/>'
            "  </xs:complexType>"
            '  <xs:element name="e1" type="a-ct"/>'
            "</xs:schema>"
        )
        (tmp_path / "main.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:main" xmlns="urn:main"'
            ' elementFormDefault="qualified">'
            '  <xs:redefine schemaLocation="a.xsd"/>'
            '  <xs:element name="root" type="a-ct"/>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "main.xsd"), mode=ParseModes.NAMESPACED)
        assert [issue.code for issue in schema.report.errors] == []
        warnings = [
            issue.code for issue in schema.report.issues if issue.severity is IssueSeverity.WARNING
        ]
        assert warnings == ["compose-cycle"]
        assert "a-ct" in schema.classes

    def test_chameleon_redefine_nested_redefine_adopts_too(self, tmp_path):
        """A redefined document's own *redefine* adopts as well.

        ``base.xsd`` (no targetNamespace) is redefined by a namespaced
        schema and itself redefines ``inner.xsd`` (also no
        targetNamespace), restricting one of its groups. The whole
        redefine chain is absorbed into the redefining schema's
        namespace, so the inner document's declarations land there and
        the restriction is judged against the rewritten original --
        which only holds when the nested redefine adopts the namespace
        the outer redefinition threaded through the include dispatch.
        """
        (tmp_path / "inner.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '<xs:group name="g3">'
            '<xs:sequence><xs:element name="a" type="xs:string"/>'
            '<xs:element name="b" type="xs:string" minOccurs="0"/></xs:sequence>'
            "</xs:group>"
            '<xs:complexType name="innerType"><xs:sequence>'
            '<xs:group ref="g3"/></xs:sequence></xs:complexType>'
            "</xs:schema>"
        )
        (tmp_path / "base.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '  <xs:redefine schemaLocation="inner.xsd">'
            '<xs:group name="g3">'
            '<xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
            "    </xs:group>"
            "  </xs:redefine>"
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:redef" xmlns="urn:redef"'
            ' elementFormDefault="qualified">'
            '  <xs:redefine schemaLocation="base.xsd"/>'
            '  <xs:element name="root" type="innerType"/>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert [issue.code for issue in schema.report.errors] == []
        (tmp_path / "instance.xml").write_text('<root xmlns="urn:redef"><a>x</a></root>')
        doc = schema.parse(str(tmp_path / "instance.xml"))
        assert not doc.report.has_errors
        # The restriction dropped the optional b: it is no longer admissible.
        (tmp_path / "invalid.xml").write_text('<root xmlns="urn:redef"><a>x</a><b>y</b></root>')
        doc = schema.parse(str(tmp_path / "invalid.xml"))
        assert doc.report.has_errors

    def test_nested_redefine_of_third_namespace_reports(self, tmp_path):
        """A nested redefine may not target a third namespace.

        ``base.xsd`` (no targetNamespace, adopted by the outer
        redefinition into ``urn:redef``) redefines a document that
        declares ``urn:other``: the rename would introduce the
        components into the adopted namespace, which the composition
        must reject (XSD 1.1 §4.2.4).
        """
        (tmp_path / "inner.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:other">'
            '<xs:complexType name="x"><xs:sequence/></xs:complexType>'
            "</xs:schema>"
        )
        (tmp_path / "base.xsd").write_text(
            f'<xs:schema {XS}><xs:redefine schemaLocation="inner.xsd"/></xs:schema>'
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:redef">'
            '  <xs:redefine schemaLocation="base.xsd"/>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        codes = [issue.code for issue in schema.report.errors]
        assert "compose-invalid" in codes

    def test_nested_override_of_third_namespace_reports(self, tmp_path):
        """A nested override may not target a third namespace.

        ``base.xsd`` (no targetNamespace, adopted by the outer
        redefinition into ``urn:redef``) overrides a document that
        declares ``urn:other``: the override would splice the
        components into the adopted namespace, which the composition
        must reject (XSD 1.1 §4.2.5 clause 2 with the effective,
        adopted namespace judging the match).
        """
        (tmp_path / "inner.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:other">'
            '<xs:complexType name="x"><xs:sequence/></xs:complexType>'
            "</xs:schema>"
        )
        (tmp_path / "base.xsd").write_text(
            f'<xs:schema {XS}><xs:override schemaLocation="inner.xsd"/></xs:schema>'
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:redef">'
            '  <xs:redefine schemaLocation="base.xsd"/>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        codes = [issue.code for issue in schema.report.errors]
        assert "compose-invalid" in codes

    def test_nested_override_of_adopted_namespace_resolves(self, tmp_path):
        """An override nested in an adopted base may target the adopted ns.

        ``base.xsd`` (no targetNamespace) is redefined by a namespaced
        schema and overrides a component of a document already
        declaring that namespace: judged by the effective (adopted)
        namespace the override is a same-namespace override and must
        compose cleanly -- the pre-adoption reading saw the unstamped
        base and rejected it as a namespace mismatch.
        """
        (tmp_path / "inner.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:redef"'
            ' elementFormDefault="qualified">'
            '<xs:element name="e" type="xs:string"/>'
            "</xs:schema>"
        )
        (tmp_path / "base.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '  <xs:override schemaLocation="inner.xsd">'
            '    <xs:element name="e" type="xs:string"/>'
            "  </xs:override>"
            '<xs:complexType name="baseType"><xs:sequence>'
            '<xs:element ref="e"/></xs:sequence></xs:complexType>'
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:redef" xmlns="urn:redef"'
            ' elementFormDefault="qualified">'
            '  <xs:redefine schemaLocation="base.xsd"/>'
            '  <xs:element name="root" type="baseType"/>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert [issue.code for issue in schema.report.errors] == []

    def test_chameleon_redefine_nested_override_adopts_too(self, tmp_path):
        """A redefined document's own *override* adopts as well.

        ``base.xsd`` (no targetNamespace) is redefined by a namespaced
        schema and *overrides* a declaration from ``inner.xsd`` (also
        no targetNamespace). The whole override chain is absorbed into
        the redefining schema's namespace (Appendix F.1 applies through
        it), so ``base.xsd``'s reference to the overridden element must
        reach the overriding declaration -- which only holds when the
        nested override adopts the namespace the outer redefinition
        threaded through the include dispatch.
        """
        (tmp_path / "inner.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '<xs:element name="helper" type="xs:string"/>'
            '<xs:complexType name="innerType"><xs:sequence>'
            '<xs:element ref="helper"/></xs:sequence></xs:complexType>'
            '<xs:element name="inner-e" type="innerType"/>'
            "</xs:schema>"
        )
        (tmp_path / "base.xsd").write_text(
            f'<xs:schema {XS} elementFormDefault="qualified">'
            '  <xs:override schemaLocation="inner.xsd">'
            '    <xs:element name="inner-e" type="innerType"/>'
            "  </xs:override>"
            '<xs:element name="base-e" type="innerType"/>'
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:redef" xmlns="urn:redef"'
            ' elementFormDefault="qualified">'
            '  <xs:redefine schemaLocation="base.xsd"/>'
            '  <xs:element name="root" type="innerType"/>'
            "</xs:schema>"
        )
        (tmp_path / "instance.xml").write_text('<root xmlns="urn:redef"><helper>x</helper></root>')
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert [issue.code for issue in schema.report.errors] == []
        doc = schema.parse(str(tmp_path / "instance.xml"))
        assert not doc.report.has_errors

        # Legacy mode matches by local name; the adopted chain is not
        # otherwise disturbed.
        (tmp_path / "legacy.xml").write_text("<root><helper>x</helper></root>")
        schema = Schema.compile(str(tmp_path / "schema.xsd"))
        assert [issue.code for issue in schema.report.errors] == []
        doc = schema.parse(str(tmp_path / "legacy.xml"))
        assert not doc.report.has_errors

    def test_circular_include_chameleon_base_reference_resolves(self, tmp_path):
        """Mutually including chameleon documents resolve their base types.

        The two documents include each other (a legal cycle) and are
        absorbed into the main schema's namespace, so each document's
        rewritten base reference names the other document's declaration.
        """
        (tmp_path / "cyc_a.xsd").write_text(
            f"<xs:schema {XS}>"
            '  <xs:include schemaLocation="cyc_b.xsd"/>'
            '  <xs:complexType name="aType"><xs:complexContent>'
            '<xs:extension base="bType">'
            '<xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>'
            "</xs:extension></xs:complexContent></xs:complexType>"
            "</xs:schema>"
        )
        (tmp_path / "cyc_b.xsd").write_text(
            f"<xs:schema {XS}>"
            '  <xs:include schemaLocation="cyc_a.xsd"/>'
            '  <xs:complexType name="bType">'
            '<xs:sequence><xs:element name="b" type="xs:string"/></xs:sequence>'
            "</xs:complexType>"
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:cyc" xmlns:c="urn:cyc">'
            '  <xs:include schemaLocation="cyc_a.xsd"/>'
            '  <xs:element name="r" type="aType"/>'
            "</xs:schema>"
        )
        (tmp_path / "instance.xml").write_text("<r><b>y</b><a>x</a></r>")
        doc = Schema.compile(str(tmp_path / "schema.xsd")).parse(str(tmp_path / "instance.xml"))
        assert not [issue for issue in doc.report.errors if issue.code == "unknown-type"]
        assert not doc.report.has_errors
        assert "aType" in doc.schema.classes
        base_names = [base.__name__ for base in doc.schema.classes["aType"].__mro__]
        assert "bType" in base_names

    def test_chameleon_attribute_ref_fixed_override_is_checked(self, tmp_path):
        """An adopted attribute reference's fixed override is compared.

        Chameleon adoption rewrites the namespace-less ``ref`` in the
        included document into a Clark name whose URI contains colons
        (``{urn:cycle:test}a``). The override check must read the
        reference's local part through those braces: a check that
        takes the text after the first colon (``split(":", 1)[-1]``)
        looks up ``cycle:test}a`` and silently skips the mismatch,
        accepting an invalid schema.
        """
        (tmp_path / "child.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:attribute name="a" type="xs:string" fixed="x"/>'
            '<xs:complexType name="t">'
            '<xs:attribute ref="a" fixed="y"/>'
            "</xs:complexType>"
            '<xs:element name="root" type="t"/>'
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:cycle:test" xmlns="urn:cycle:test">'
            '<xs:include schemaLocation="child.xsd"/>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert [(issue.code, issue.message) for issue in _schema_errors(schema.report)] == [
            (
                "declaration-attribute",
                "attribute reference '{urn:cycle:test}a' fixed value 'y' does not "
                "match the referenced declaration's 'x'",
            )
        ]

    def test_chameleon_attribute_ref_fixed_override_ignores_other_namespace(self, tmp_path):
        """The override is compared with the declaration in its own namespace.

        An imported namespace declares a same-local global attribute
        ``a`` with a different ``fixed``. A namespace-blind lookup takes
        the decoy and reports a false mismatch for the adopted
        reference, which names the real declaration in the including
        namespace. The valid schema must stay clean.
        """
        (tmp_path / "decoy.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:decoy" xmlns="urn:decoy">'
            '<xs:attribute name="a" type="xs:string" fixed="decoy"/>'
            "</xs:schema>"
        )
        (tmp_path / "child.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:attribute name="a" type="xs:string" fixed="real"/>'
            '<xs:complexType name="t">'
            '<xs:attribute ref="a" fixed="real"/>'
            "</xs:complexType>"
            '<xs:element name="root" type="t"/>'
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:cycle:test" xmlns="urn:cycle:test"'
            ' xmlns:d="urn:decoy">'
            '<xs:import namespace="urn:decoy" schemaLocation="decoy.xsd"/>'
            '<xs:include schemaLocation="child.xsd"/>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert not schema.report.has_errors

    def test_chameleon_attribute_type_naming_a_complex_type_is_rejected(self, tmp_path):
        """An adopted attribute declaration's ``type`` is still checked.

        Adoption rewrites the namespace-less ``type`` in the included
        document into ``{urn:cycle:test}ct``. Splitting that spelling at
        its first colon reads ``{urn`` as a prefix and looks up
        ``cycle:test}ct``, which names no type, so the schema is
        accepted even though an attribute type must be simple.
        """
        (tmp_path / "child.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:complexType name="ct"><xs:sequence/></xs:complexType>'
            '<xs:attribute name="a" type="ct"/>'
            '<xs:element name="root">'
            '<xs:complexType><xs:attribute ref="a"/></xs:complexType>'
            "</xs:element>"
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:cycle:test" xmlns="urn:cycle:test">'
            '<xs:include schemaLocation="child.xsd"/>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert [(issue.code, issue.message) for issue in _schema_errors(schema.report)] == [
            (
                "declaration-attribute",
                "attribute 'a' type '{urn:cycle:test}ct' is a complex type; "
                "attribute types must be simple",
            )
        ]

    def test_chameleon_attribute_type_ignores_other_namespace(self, tmp_path):
        """The attribute's type is resolved in its own namespace.

        An imported namespace declares a decoy complex type ``ct``; the
        adopted declaration's ``type="ct"`` names the simple type in the
        including namespace. A namespace-blind lookup finds the decoy
        and wrongly rejects the valid schema.
        """
        (tmp_path / "decoy.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:decoy" xmlns="urn:decoy">'
            '<xs:complexType name="ct"><xs:sequence/></xs:complexType>'
            "</xs:schema>"
        )
        (tmp_path / "child.xsd").write_text(
            f"<xs:schema {XS}>"
            '<xs:simpleType name="ct"><xs:restriction base="xs:string"/></xs:simpleType>'
            '<xs:attribute name="a" type="ct"/>'
            '<xs:element name="root">'
            '<xs:complexType><xs:attribute ref="a"/></xs:complexType>'
            "</xs:element>"
            "</xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:cycle:test" xmlns="urn:cycle:test"'
            ' xmlns:d="urn:decoy">'
            '<xs:import namespace="urn:decoy" schemaLocation="decoy.xsd"/>'
            '<xs:include schemaLocation="child.xsd"/>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert not schema.report.has_errors


# ---------------------------------------------------------------------------
# Unresolved-resource severity (Task 11)


class TestUnresolvedResourceSeverity:
    """A ``schemaLocation`` that cannot be retrieved is a hint, not an error.

    XSD treats an unresolvable schema reference as non-fatal: the schema
    remains valid and the run reports a schema-phase warning. Rule
    violations in the composing document must still poison it.
    """

    def _parser(self, tmp_path, schema, files=None, mode=None):
        (tmp_path / "schema.xsd").write_text(schema, encoding="utf-8")
        for name, content in (files or {}).items():
            (tmp_path / name).write_text(content, encoding="utf-8")
        (tmp_path / "instance.xml").write_text("<root/>", encoding="utf-8")
        schema_obj = Schema.compile(
            str(tmp_path / "schema.xsd"), mode=mode or ParseModes.NAMESPACED
        )
        return schema_obj.parse(str(tmp_path / "instance.xml"))

    def test_missing_include_is_warning_not_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:include schemaLocation="0"/>'
            '<xs:element name="root"/></xs:schema>',
        )
        assert not _schema_errors(doc.report)
        assert "schema-compose" in _schema_warning_codes(doc.report)

    def test_missing_import_warns(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:import namespace="urn:x" schemaLocation="0"/>'
            '<xs:element name="root"/></xs:schema>',
        )
        assert not _schema_errors(doc.report)
        assert "import-unresolved" in _schema_warning_codes(doc.report)

    def test_missing_redefine_is_warning_not_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="0"/>'
            '<xs:element name="root"/></xs:schema>',
        )
        assert not _schema_errors(doc.report)
        assert "schema-compose" in _schema_warning_codes(doc.report)

    def test_namespace_only_import_is_warning(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:import namespace="urn:unused"/>'
            '<xs:element name="root"/></xs:schema>',
        )
        assert not _schema_errors(doc.report)
        assert "import-unresolved" in _schema_warning_codes(doc.report)

    def test_malformed_include_errors(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:include schemaLocation="bad.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"bad.xsd": "<xs:schema><not closed"},
        )
        assert "schema-compose" in {i.code for i in _schema_errors(doc.report)}

    def test_include_without_schema_location_is_still_an_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:include/><xs:element name="root"/></xs:schema>',
        )
        assert "schema-compose" in {i.code for i in _schema_errors(doc.report)}

    def test_redefine_without_schema_location_is_still_an_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine/><xs:element name="root"/></xs:schema>',
        )
        assert "schema-compose" in {i.code for i in _schema_errors(doc.report)}

    def test_include_namespace_mismatch_is_still_an_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">'
            '<xs:include schemaLocation="other.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"other.xsd": f'<xs:schema {XS} targetNamespace="urn:b"/>'},
        )
        assert "compose-namespace" in {i.code for i in _schema_errors(doc.report)}

    def test_redefine_with_content_and_missing_base_is_an_error(self, tmp_path):
        # Redefining a component requires the base document: a missing
        # base cannot be verified, so the schema is not valid.
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="0">'
            '<xs:group name="g"><xs:sequence><xs:element name="e"/>'
            "</xs:sequence></xs:group></xs:redefine>"
            '<xs:element name="root"/></xs:schema>',
        )
        assert "schema-compose" in {i.code for i in _schema_errors(doc.report)}

    def test_redefine_annotations_are_allowed_with_missing_base(self, tmp_path):
        # W3C annotB025: duplicate annotation on xs:redefine is valid, so
        # only the missing-resource warning remains.
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="0">'
            "<xs:annotation/><xs:annotation/></xs:redefine>"
            '<xs:element name="root"/></xs:schema>',
        )
        assert not _schema_errors(doc.report)
        assert "schema-compose" in _schema_warning_codes(doc.report)

    def test_include_duplicate_annotation_is_an_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:include schemaLocation="base.xsd">'
            "<xs:annotation/><xs:annotation/></xs:include>"
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": f"<xs:schema {XS}/>"},
        )
        assert "schema-compose" in {i.code for i in _schema_errors(doc.report)}

    def test_import_duplicate_annotation_is_an_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:import namespace="urn:b" schemaLocation="base.xsd">'
            "<xs:annotation/><xs:annotation/></xs:import>"
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": f'<xs:schema {XS} targetNamespace="urn:b"/>'},
        )
        assert "schema-compose" in {i.code for i in _schema_errors(doc.report)}

    def test_self_import_is_an_error(self, tmp_path):
        # Importing a schema's own target namespace is a rule violation;
        # with a reference into that namespace the unresolved import is
        # fatal rather than a hint.
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">'
            '<xs:import namespace="urn:a"/>'
            '<xs:complexType name="ct"><xs:sequence>'
            '<xs:element ref="a:e"/></xs:sequence></xs:complexType>'
            '<xs:element name="e" type="xs:string"/>'
            '<xs:element name="root" type="ct"/></xs:schema>',
        )
        assert "import-unresolved" in {i.code for i in _schema_errors(doc.report)}
        assert "import-unresolved" not in _schema_warning_codes(doc.report)

    def test_reference_into_unresolved_import_is_an_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS} xmlns:b="urn:b">'
            '<xs:import namespace="urn:b"/>'
            '<xs:element name="root">'
            "<xs:complexType><xs:sequence>"
            '<xs:element ref="b:x"/>'
            "</xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>",
        )
        assert "import-unresolved" in {i.code for i in _schema_errors(doc.report)}
        assert "import-unresolved" not in _schema_warning_codes(doc.report)

    def test_duplicate_id_on_import_and_declaration_is_an_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f"<xs:schema {XS}>"
            '<xs:import namespace="urn:b" id="a"/>'
            '<xs:group name="grp" id="a"><xs:sequence>'
            '<xs:element name="e"/></xs:sequence></xs:group>'
            '<xs:element name="root"/></xs:schema>',
        )
        assert "declaration-duplicate" in {i.code for i in _schema_errors(doc.report)}


# ---------------------------------------------------------------------------
# Composition and redefine legality (Task 12)


class TestComposeInvalid:
    """Structural ``xs:redefine`` / ``xs:import`` rule violations.

    These are the composition rules that hold regardless of whether the
    referenced resource resolves: a redefine must name a component that
    exists in the base, the same base component must not be redefined
    twice, an import's ``namespace`` must match the imported document,
    a redefine's base must share the redefining schema's namespace (or
    be a chameleon), and a chameleon self-reference must be qualified
    into the redefining namespace.
    """

    def _parser(self, tmp_path, schema, files=None):
        (tmp_path / "schema.xsd").write_text(schema, encoding="utf-8")
        for name, content in (files or {}).items():
            (tmp_path / name).write_text(content, encoding="utf-8")
        (tmp_path / "instance.xml").write_text("<root/>", encoding="utf-8")
        schema_obj = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        return schema_obj.parse(str(tmp_path / "instance.xml"))

    def _error_codes(self, doc):
        return {i.code for i in _schema_errors(doc.report)}

    def test_redefine_of_missing_base_component_is_error(self, tmp_path):
        # The base document defines group ``g``; redefining group ``h``
        # would *add* a new component, which a redefine must not do.
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="h"><xs:sequence>'
            '<xs:element name="e"/></xs:sequence></xs:group>'
            '</xs:redefine><xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS}><xs:group name="g">'
                '<xs:sequence><xs:element name="e"/></xs:sequence>'
                "</xs:group></xs:schema>"
            },
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_redefine_duplicate_same_base_component_is_error(self, tmp_path):
        base = (
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="a"/></xs:sequence></xs:group>'
            "</xs:schema>"
        )
        redefiner = (
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:group ref="g"/><xs:element name="b"/>'
            "</xs:sequence></xs:group></xs:redefine></xs:schema>"
        )
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:include schemaLocation="a.xsd"/>'
            '<xs:include schemaLocation="b.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": base, "a.xsd": redefiner, "b.xsd": redefiner},
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_redefine_chain_is_not_duplicate(self, tmp_path):
        # A redefine may legitimately redefine a component that was itself
        # redefined by its own base document (a chain); only a second
        # redefine of the *same* base document is a conflict.
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="a.xsd">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:group ref="g"/><xs:element name="c"/>'
            "</xs:sequence></xs:group></xs:redefine>"
            '<xs:element name="root"/></xs:schema>',
            files={
                "a.xsd": f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
                '<xs:group name="g"><xs:sequence>'
                '<xs:group ref="g"/><xs:element name="b"/>'
                "</xs:sequence></xs:group></xs:redefine></xs:schema>",
                "base.xsd": f'<xs:schema {XS}><xs:group name="g">'
                '<xs:sequence><xs:element name="a"/></xs:sequence>'
                "</xs:group></xs:schema>",
            },
        )
        assert "compose-invalid" not in self._error_codes(doc)

    def test_import_namespace_mismatch_is_error(self, tmp_path):
        # An import with a namespace attribute cannot absorb a document
        # that declares no target namespace (that is an include).
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS} xmlns:b="urn:b">'
            '<xs:import namespace="urn:b" schemaLocation="base.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": f'<xs:schema {XS}><xs:element name="e"/></xs:schema>'},
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_import_namespace_match_is_valid(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS} xmlns:b="urn:b">'
            '<xs:import namespace="urn:b" schemaLocation="base.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS} targetNamespace="urn:b">'
                '<xs:element name="e"/></xs:schema>'
            },
        )
        assert "compose-invalid" not in self._error_codes(doc)

    def test_import_illegal_child_is_error(self, tmp_path):
        # notatF033: the only legal child of ``xs:import`` (and
        # ``xs:include``) is an annotation; a nested declaration such as
        # a notation is not a legal directive child.
        doc = self._parser(
            tmp_path,
            f"<xs:schema {XS}>"
            "<xs:import>"
            '<xs:notation name="jpeg" public="image/jpeg"/>'
            "</xs:import>"
            '<xs:element name="root"/></xs:schema>',
        )
        assert "declaration-child" in self._error_codes(doc)

    def test_import_annotation_child_is_valid(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f"<xs:schema {XS}>"
            '<xs:import schemaLocation="base.xsd">'
            "<xs:annotation/>"
            "</xs:import>"
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": f'<xs:schema {XS}><xs:element name="e"/></xs:schema>'},
        )
        assert "declaration-child" not in self._error_codes(doc)

    def test_import_without_namespace_into_no_namespace_schema_is_error(self, tmp_path):
        # schF3/addB008/addB035: an import with no namespace attribute
        # imports the absent target namespace, which cannot differ from a
        # no-namespace importing schema's own.
        doc = self._parser(
            tmp_path,
            f"<xs:schema {XS}>"
            '<xs:import schemaLocation="base.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"base.xsd": f'<xs:schema {XS}><xs:element name="e"/></xs:schema>'},
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_empty_import_namespace_is_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:import namespace=""/><xs:element name="root"/></xs:schema>',
        )
        assert "declaration-attribute" in self._error_codes(doc)

    def test_empty_target_namespace_is_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace=""><xs:element name="root"/></xs:schema>',
        )
        assert "declaration-attribute" in self._error_codes(doc)

    def test_redefine_base_namespace_mismatch_is_error(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="e"/></xs:sequence></xs:group>'
            '</xs:redefine><xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS} targetNamespace="urn:b">'
                '<xs:group name="g"><xs:sequence>'
                '<xs:element name="e"/></xs:sequence></xs:group>'
                "</xs:schema>"
            },
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_chameleon_redefine_unqualified_self_reference_is_error(self, tmp_path):
        # A no-namespace base redefined by a namespaced schema has its
        # component ported into the redefining namespace, so the self
        # reference must be qualified into it; an unqualified ``ref``
        # names no component there.
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:choice>'
            '<xs:group ref="g"/><xs:element name="b"/>'
            "</xs:choice></xs:group></xs:redefine>"
            '<xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS}><xs:group name="g">'
                '<xs:sequence><xs:element name="a"/></xs:sequence>'
                "</xs:group></xs:schema>"
            },
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_chameleon_redefine_qualified_self_reference_is_valid(self, tmp_path):
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:choice>'
            '<xs:group ref="a:g"/><xs:element name="b"/>'
            "</xs:choice></xs:group></xs:redefine>"
            '<xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS}><xs:group name="g">'
                '<xs:sequence><xs:element name="a"/></xs:sequence>'
                "</xs:group></xs:schema>"
            },
        )
        assert "compose-invalid" not in self._error_codes(doc)

    def _attribute_group_redefine(self, tmp_path, base_body, derived_body):
        return self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            f'<xs:attributeGroup name="ag">{derived_body}</xs:attributeGroup>'
            '</xs:redefine><xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS}><xs:attributeGroup name="ag">'
                f"{base_body}</xs:attributeGroup></xs:schema>"
            },
        )

    def test_attribute_group_redefine_adds_attribute_is_error(self, tmp_path):
        doc = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a" type="xs:string"/>',
            '<xs:attribute name="a" type="xs:string"/><xs:attribute name="b" type="xs:string"/>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_attribute_group_redefine_reorders_is_error(self, tmp_path):
        doc = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a"/><xs:attribute name="b"/>',
            '<xs:attribute name="b"/><xs:attribute name="a"/>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_attribute_group_redefine_drops_fixed_is_error(self, tmp_path):
        doc = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a" type="xs:string" fixed="x"/>',
            '<xs:attribute name="a" type="xs:string" default="x"/>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_attribute_group_redefine_self_reference_duplicate_is_error(self, tmp_path):
        doc = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a" type="xs:string"/>',
            '<xs:attributeGroup ref="ag"/><xs:attribute name="a" type="xs:string"/>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_attribute_group_redefine_valid_restriction_is_accepted(self, tmp_path):
        doc = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a"/><xs:attribute name="b"/>',
            '<xs:attribute name="a"/><xs:attribute name="b"/>',
        )
        assert "compose-invalid" not in self._error_codes(doc)

    def test_attribute_group_redefine_valid_extension_is_accepted(self, tmp_path):
        doc = self._attribute_group_redefine(
            tmp_path,
            '<xs:attribute name="a" type="xs:string"/>',
            '<xs:attributeGroup ref="ag"/><xs:attribute name="b" type="xs:string"/>',
        )
        assert "compose-invalid" not in self._error_codes(doc)

    def _redefine(self, tmp_path, derived_body, base_body):
        return self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            f"{derived_body}"
            '</xs:redefine><xs:element name="root"/></xs:schema>',
            files={"base.xsd": f"<xs:schema {XS}>{base_body}</xs:schema>"},
        )

    def test_include_of_well_formed_non_schema_is_error(self, tmp_path):
        # schB5: a schemaLocation that resolves to well-formed XML that is
        # not an xs:schema is a composition error, not a silent skip.
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:include schemaLocation="notaschema.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"notaschema.xsd": "<not-a-schema/>"},
        )
        assert "schema-compose" in self._error_codes(doc)

    def test_import_of_well_formed_non_schema_is_error(self, tmp_path):
        # schE6/schE10.
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:import namespace="urn:x" '
            'schemaLocation="notaschema.xsd"/>'
            '<xs:element name="root"/></xs:schema>',
            files={"notaschema.xsd": "<notAnXsd/>"},
        )
        assert "schema-compose" in self._error_codes(doc)

    def test_redefine_namespace_attribute_is_error(self, tmp_path):
        # schH4: ``xs:redefine`` carries a schemaLocation, never a
        # namespace attribute.
        doc = self._parser(
            tmp_path,
            f'<xs:schema {XS}><xs:redefine namespace="foo" schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:sequence><xs:element name="a"/></xs:sequence></xs:group>'
            '</xs:redefine><xs:element name="root"/></xs:schema>',
            files={
                "base.xsd": f'<xs:schema {XS}><xs:group name="g"><xs:sequence>'
                '<xs:element name="a"/></xs:sequence></xs:group></xs:schema>'
            },
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_redefine_of_element_component_is_error(self, tmp_path):
        # SUN xsd003-1.e: an element declaration cannot be redefined;
        # redefine covers only types, groups and attribute groups.
        doc = self._redefine(
            tmp_path,
            '<xs:element name="root"/>',
            '<xs:element name="root"/>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_redefine_of_attribute_component_is_error(self, tmp_path):
        # SUN xsd003-2.e.
        doc = self._redefine(
            tmp_path,
            '<xs:attribute name="gAtt" type="xs:string"/>',
            '<xs:attribute name="gAtt" type="xs:string"/>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_redefine_simple_type_without_self_base_is_error(self, tmp_path):
        # schJ2: a redefined simple type must restrict the original.
        doc = self._redefine(
            tmp_path,
            '<xs:simpleType name="t"><xs:restriction base="xs:string">'
            '<xs:minLength value="2"/></xs:restriction></xs:simpleType>',
            '<xs:simpleType name="t"><xs:restriction base="xs:string"/></xs:simpleType>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_redefine_simple_type_with_self_base_is_valid(self, tmp_path):
        doc = self._redefine(
            tmp_path,
            '<xs:simpleType name="t"><xs:restriction base="t">'
            '<xs:minLength value="2"/></xs:restriction></xs:simpleType>',
            '<xs:simpleType name="t"><xs:restriction base="xs:string"/></xs:simpleType>',
        )
        assert "compose-invalid" not in self._error_codes(doc)

    def test_redefine_complex_type_without_self_base_is_error(self, tmp_path):
        # schK2: the restriction must name the original type.
        doc = self._redefine(
            tmp_path,
            '<xs:complexType name="t"><xs:complexContent>'
            '<xs:restriction base="u"><xs:sequence><xs:element name="a"/></xs:sequence>'
            "</xs:restriction></xs:complexContent></xs:complexType>",
            '<xs:complexType name="t"><xs:sequence><xs:element name="a"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:complexType name="u"><xs:sequence><xs:element name="a"/>'
            "</xs:sequence></xs:complexType>",
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_redefine_complex_type_without_derivation_is_error(self, tmp_path):
        # schK3: a redefined complex type with no complexContent at all
        # cannot derive from the original.
        doc = self._redefine(
            tmp_path,
            '<xs:complexType name="t"><xs:sequence><xs:element name="a"/></xs:sequence>'
            "</xs:complexType>",
            '<xs:complexType name="t"><xs:sequence><xs:element name="a"/>'
            "</xs:sequence></xs:complexType>",
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_redefine_complex_type_with_self_base_is_valid(self, tmp_path):
        doc = self._redefine(
            tmp_path,
            '<xs:complexType name="t"><xs:complexContent>'
            '<xs:restriction base="t"><xs:sequence><xs:element name="a"/></xs:sequence>'
            "</xs:restriction></xs:complexContent></xs:complexType>",
            '<xs:complexType name="t"><xs:sequence><xs:element name="a"/>'
            '<xs:element name="b"/></xs:sequence></xs:complexType>',
        )
        assert "compose-invalid" not in self._error_codes(doc)

    def test_group_redefine_self_reference_min_occurs_zero_is_error(self, tmp_path):
        # schR3: a redefine's self reference must be exactly 1/1.
        doc = self._redefine(
            tmp_path,
            '<xs:group name="g"><xs:choice><xs:element name="c23" type="xs:int"/>'
            '<xs:group ref="g" minOccurs="0"/><xs:element name="c24" type="xs:int"/>'
            "</xs:choice></xs:group>",
            '<xs:group name="g"><xs:choice><xs:element name="c21" type="xs:int"/>'
            '<xs:element name="c22" type="xs:int"/></xs:choice></xs:group>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_group_redefine_self_reference_max_occurs_two_is_error(self, tmp_path):
        # schR4.
        doc = self._redefine(
            tmp_path,
            '<xs:group name="g"><xs:choice><xs:element name="c23" type="xs:int"/>'
            '<xs:group ref="g" maxOccurs="2"/><xs:element name="c24" type="xs:int"/>'
            "</xs:choice></xs:group>",
            '<xs:group name="g"><xs:choice><xs:element name="c21" type="xs:int"/>'
            '<xs:element name="c22" type="xs:int"/></xs:choice></xs:group>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_nested_redefine_self_reference_colon_adopted_uri_is_error(self, tmp_path):
        # schR3, reached through a chameleon adoption whose namespace
        # URI contains colons: the outer redefinition rewrites the
        # nested redefine body's self reference into a Clark name, so
        # the occurrence check must read its local part through the
        # braces, not through the last colon of the URI.
        (tmp_path / "inner.xsd").write_text(
            f'<xs:schema {XS}><xs:group name="g"><xs:choice>'
            '<xs:element name="c21" type="xs:int"/>'
            '<xs:element name="c22" type="xs:int"/>'
            "</xs:choice></xs:group></xs:schema>"
        )
        (tmp_path / "base.xsd").write_text(
            f'<xs:schema {XS}><xs:redefine schemaLocation="inner.xsd">'
            '<xs:group name="g"><xs:choice>'
            '<xs:element name="c23" type="xs:int"/>'
            '<xs:group ref="g" minOccurs="0"/>'
            '<xs:element name="c24" type="xs:int"/>'
            "</xs:choice></xs:group></xs:redefine></xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:cycle:test" xmlns="urn:cycle:test">'
            '<xs:redefine schemaLocation="base.xsd"/>'
            '<xs:element name="root"><xs:complexType><xs:sequence>'
            '<xs:group ref="g"/></xs:sequence></xs:complexType></xs:element>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert "compose-invalid" in [issue.code for issue in schema.report.errors]

    def test_nested_redefine_self_reference_colon_adopted_uri_valid(self, tmp_path):
        # The same adopted chain with a conforming self reference
        # (exactly 1/1) stays clean.
        (tmp_path / "inner.xsd").write_text(
            f'<xs:schema {XS}><xs:group name="g"><xs:sequence>'
            '<xs:element name="c21" type="xs:int"/>'
            "</xs:sequence></xs:group></xs:schema>"
        )
        (tmp_path / "base.xsd").write_text(
            f'<xs:schema {XS}><xs:redefine schemaLocation="inner.xsd">'
            '<xs:group name="g"><xs:sequence>'
            '<xs:group ref="g"/>'
            '<xs:element name="c22" type="xs:int"/>'
            "</xs:sequence></xs:group></xs:redefine></xs:schema>"
        )
        (tmp_path / "schema.xsd").write_text(
            f'<xs:schema {XS} targetNamespace="urn:cycle:test" xmlns="urn:cycle:test">'
            '<xs:redefine schemaLocation="base.xsd"/>'
            '<xs:element name="root"><xs:complexType><xs:sequence>'
            '<xs:group ref="g"/></xs:sequence></xs:complexType></xs:element>'
            "</xs:schema>"
        )
        schema = Schema.compile(str(tmp_path / "schema.xsd"), mode=ParseModes.NAMESPACED)
        assert [issue.code for issue in schema.report.errors] == []

    def test_group_redefine_self_reference_one_one_is_valid(self, tmp_path):
        doc = self._redefine(
            tmp_path,
            '<xs:group name="g"><xs:sequence><xs:group ref="g"/>'
            '<xs:element name="b" type="xs:string"/></xs:sequence></xs:group>',
            '<xs:group name="g"><xs:sequence><xs:element name="a" type="xs:string"/>'
            "</xs:sequence></xs:group>",
        )
        assert "compose-invalid" not in self._error_codes(doc)

    def test_group_redefine_superset_without_self_reference_is_error(self, tmp_path):
        # schL8: without a self reference the new model must restrict the
        # original, so adding an element is a violation.
        doc = self._redefine(
            tmp_path,
            '<xs:group name="g"><xs:sequence><xs:element name="c31" type="xs:int"/>'
            '<xs:element name="c32" type="xs:int"/><xs:element name="c33" type="xs:int"/>'
            "</xs:sequence></xs:group>",
            '<xs:group name="g"><xs:sequence><xs:element name="c31" type="xs:int"/>'
            '<xs:element name="c32" type="xs:int"/></xs:sequence></xs:group>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_group_redefine_reorder_without_self_reference_is_error(self, tmp_path):
        # schL6.
        doc = self._redefine(
            tmp_path,
            '<xs:group name="g"><xs:sequence><xs:element name="c32" type="xs:int"/>'
            '<xs:element name="c31" type="xs:int"/></xs:sequence></xs:group>',
            '<xs:group name="g"><xs:sequence><xs:element name="c31" type="xs:int"/>'
            '<xs:element name="c32" type="xs:int"/></xs:sequence></xs:group>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_group_redefine_drop_required_all_member_is_error(self, tmp_path):
        # schL1.
        doc = self._redefine(
            tmp_path,
            '<xs:group name="g"><xs:all><xs:element name="c11" type="xs:string"/>'
            '<xs:element name="c13" type="xs:string"/></xs:all></xs:group>',
            '<xs:group name="g"><xs:all><xs:element name="c11" type="xs:string"/>'
            '<xs:element name="c12" type="xs:string"/>'
            '<xs:element name="c13" type="xs:string"/></xs:all></xs:group>',
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_group_redefine_type_change_without_self_reference_is_error(self, tmp_path):
        # schO2: narrowing maxOccurs is fine but swapping an element's
        # declared type is not a restriction.
        doc = self._redefine(
            tmp_path,
            '<xs:group name="g"><xs:choice><xs:element name="c21" type="xs:string" '
            'maxOccurs="2"/><xs:element name="c22" type="xs:int" maxOccurs="2"/>'
            "</xs:choice></xs:group>",
            '<xs:group name="g"><xs:choice><xs:element name="c21" type="xs:int" '
            'maxOccurs="3"/><xs:element name="c22" type="xs:int" maxOccurs="3"/>'
            "</xs:choice></xs:group>",
        )
        assert "compose-invalid" in self._error_codes(doc)

    def test_group_redefine_narrowing_occurrence_is_valid(self, tmp_path):
        doc = self._redefine(
            tmp_path,
            '<xs:group name="g"><xs:choice><xs:element name="c21" type="xs:int" '
            'maxOccurs="2"/><xs:element name="c22" type="xs:int" maxOccurs="2"/>'
            "</xs:choice></xs:group>",
            '<xs:group name="g"><xs:choice><xs:element name="c21" type="xs:int" '
            'maxOccurs="3"/><xs:element name="c22" type="xs:int" maxOccurs="3"/>'
            "</xs:choice></xs:group>",
        )
        assert "compose-invalid" not in self._error_codes(doc)


# ---------------------------------------------------------------------------
# xs:override component replacement (Task 10)


class TestOverrideComposition:
    """XSD 1.1 ``xs:override`` replaces components wholesale.

    Unlike ``xs:redefine`` an override need not modify an existing
    component (it may match nothing, in which case it is ignored) and it
    may replace a component with one of a different kind in the same
    symbol space (a simple type for a complex type). References from
    inside the overriding component resolve to the override, not to the
    replaced base definition.
    """

    def _parser(self, tmp_path, schema, instance, files=None):
        (tmp_path / "schema.xsd").write_text(schema, encoding="utf-8")
        for name, content in (files or {}).items():
            (tmp_path / name).write_text(content, encoding="utf-8")
        (tmp_path / "instance.xml").write_text(instance, encoding="utf-8")
        schema_obj = Schema.compile(str(tmp_path / "schema.xsd"))
        return schema_obj.parse(str(tmp_path / "instance.xml"))

    def _error_codes(self, doc):
        return {i.code for i in doc.report.errors}

    def test_override_replaces_type_and_group(self, tmp_path):
        base = (
            f"<xs:schema {XS}>"
            '<xs:complexType name="T"><xs:sequence>'
            '<xs:element name="old" type="xs:string"/></xs:sequence></xs:complexType>'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/></xs:sequence></xs:group>'
            "</xs:schema>"
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:complexType name="T"><xs:sequence>'
            '<xs:group ref="g"/></xs:sequence></xs:complexType>'
            '<xs:group name="g"><xs:sequence>'
            '<xs:element name="b" type="xs:string"/></xs:sequence></xs:group>'
            "</xs:override>"
            '<xs:element name="r" type="T"/>'
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<r><b/></r>", {"base.xsd": base})
        assert not doc.report.has_errors
        # The overriding type wholly replaces the base, which is dropped.
        assert "T" in doc.schema.classes
        assert "T|base" not in doc.schema.classes
        assert [d.name for d in doc.schema.classes["T"]()._getElements()] == ["b"]

    def test_override_replaces_complex_type_with_simple_type(self, tmp_path):
        # over013: the override matches by symbol space, so a simpleType
        # may replace a complexType of the same name.
        base = (
            f"<xs:schema {XS}>"
            '<xs:complexType name="structuredDate"><xs:sequence>'
            '<xs:element name="year"/></xs:sequence></xs:complexType>'
            '<xs:element name="doc" type="structuredDate"/>'
            "</xs:schema>"
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:simpleType name="structuredDate">'
            '<xs:restriction base="xs:date"/></xs:simpleType>'
            "</xs:override>"
            '<xs:element name="r" type="structuredDate"/>'
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<r>2001-01-01</r>", {"base.xsd": base})
        assert not doc.report.has_errors

    def test_override_uses_component_added_by_the_overriding_document(self, tmp_path):
        # A brand-new component declared by the overriding schema (here a
        # simple type used by the overriding element) is available, as in
        # over028/over019.
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="smallInt"/>'
            "</xs:override>"
            '<xs:simpleType name="smallInt">'
            '<xs:restriction base="xs:int"><xs:maxInclusive value="16"/></xs:restriction>'
            "</xs:simpleType>"
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<doc>16</doc>", {"base.xsd": base})
        assert not doc.report.has_errors

    def test_override_matching_nothing_is_ignored_not_added(self, tmp_path):
        # over026: a declaration matching nothing in the target set is
        # silently ignored (XSD 1.1 §4.2.5), so it does not satisfy a
        # reference into it.
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:simpleType name="ghost">'
            '<xs:restriction base="xs:string"/></xs:simpleType>'
            "</xs:override>"
            '<xs:element name="r" type="ghost"/>'
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<r>x</r>", {"base.xsd": base})
        assert "unknown-type" in self._error_codes(doc)
        assert "override-invalid" not in self._error_codes(doc)

    def test_override_of_a_missing_component_is_legal(self, tmp_path):
        # The same as above but no reference to the ignored component:
        # overriding a component the target set lacks is not an error.
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:simpleType name="ghost">'
            '<xs:restriction base="xs:string"/></xs:simpleType>'
            "</xs:override>"
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<r>x</r>", {"base.xsd": base})
        assert "override-invalid" not in self._error_codes(doc)
        assert "compose-invalid" not in self._error_codes(doc)
        assert not doc.report.has_errors

    def test_override_self_reference_resolves_to_the_override(self, tmp_path):
        # over006: ``ref="section"`` inside the overriding declaration
        # names the overriding (recursive) element, not the base copy.
        base = (
            f"<xs:schema {XS}>"
            '<xs:element name="section">'
            "<xs:complexType><xs:sequence>"
            '<xs:element name="head" type="xs:string"/>'
            '<xs:element ref="section" minOccurs="1" maxOccurs="unbounded"/>'
            "</xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="section">'
            "<xs:complexType><xs:sequence>"
            '<xs:element name="head" type="xs:string"/>'
            '<xs:element ref="section" minOccurs="0" maxOccurs="unbounded"/>'
            "</xs:sequence>"
            '<xs:attribute name="nr" type="xs:decimal" use="required"/>'
            "</xs:complexType></xs:element>"
            "</xs:override>"
            "</xs:schema>"
        )
        doc = self._parser(
            tmp_path,
            schema,
            '<section nr="1"><head>a</head><section nr="2"><head>b</head></section></section>',
            {"base.xsd": base},
        )
        assert not doc.report.has_errors

    def test_illegal_override_child_is_override_invalid(self, tmp_path):
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:sequence><xs:element name="e"/></xs:sequence>'
            "</xs:override>"
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<r>x</r>", {"base.xsd": base})
        assert "override-invalid" in self._error_codes(doc)

    def test_duplicate_override_target_in_one_block_is_override_invalid(self, tmp_path):
        # over021: the same component named twice in one xs:override.
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:date"/>'
            '<xs:element name="doc" type="xs:time"/>'
            "</xs:override>"
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<doc>2001-01-01</doc>", {"base.xsd": base})
        assert "override-invalid" in self._error_codes(doc)

    def test_duplicate_override_across_blocks_is_override_invalid(self, tmp_path):
        # over022: the same base component overridden by two blocks.
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:date"/>'
            "</xs:override>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:time"/>'
            "</xs:override>"
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<doc>2001-01-01</doc>", {"base.xsd": base})
        assert "override-invalid" in self._error_codes(doc)

    def test_override_namespace_mismatch_is_error(self, tmp_path):
        # over016: a no-namespace overriding document may not override a
        # namespaced base document.
        base = f'<xs:schema {XS} targetNamespace="urn:b"><xs:element name="doc"/></xs:schema>'
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:string"/>'
            "</xs:override>"
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<doc>x</doc>", {"base.xsd": base})
        assert "compose-invalid" in self._error_codes(doc)

    def test_override_missing_base_with_content_is_error(self, tmp_path):
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="absent.xsd">'
            '<xs:element name="doc" type="xs:string"/>'
            "</xs:override>"
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<r>x</r>")
        assert "schema-compose" in self._error_codes(doc)

    def test_override_type_ignores_host_default_open_content(self, tmp_path):
        # open043: a type defined within xs:override takes its default open
        # content from the overridden document, not the overriding host.
        base = (
            f"<xs:schema {XS}>"
            '<xs:complexType name="beta"><xs:sequence/></xs:complexType>'
            '<xs:element name="doc" type="beta"/>'
            "</xs:schema>"
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:complexType name="beta"><xs:sequence/></xs:complexType>'
            "</xs:override>"
            '<xs:defaultOpenContent mode="suffix" appliesToEmpty="true">'
            '<xs:any namespace="urn:open" processContents="lax"/>'
            "</xs:defaultOpenContent>"
            '<xs:element name="r" type="xs:string"/>'
            "</xs:schema>"
        )
        doc = self._parser(
            tmp_path, schema, '<doc><extra xmlns="urn:open"/></doc>', {"base.xsd": base}
        )
        # The override's beta does not inherit the host's default open
        # content, so the wildcard-admitted extra element is rejected.
        assert "unexpected-element" in {i.code for i in doc.report.errors}

    def test_nested_override(self, tmp_path):
        # top overrides mid, which overrides base; the innermost
        # declaration (top) wins.
        base = (
            f"<xs:schema {XS}>"
            '<xs:simpleType name="T"><xs:restriction base="xs:string"/></xs:simpleType>'
            "</xs:schema>"
        )
        mid = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:simpleType name="T"><xs:restriction base="xs:string">'
            '<xs:maxLength value="5"/></xs:restriction></xs:simpleType>'
            "</xs:override>"
            "</xs:schema>"
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="mid.xsd">'
            '<xs:simpleType name="T"><xs:restriction base="xs:string">'
            '<xs:maxLength value="3"/></xs:restriction></xs:simpleType>'
            "</xs:override>"
            '<xs:element name="r" type="T"/>'
            "</xs:schema>"
        )
        doc = self._parser(tmp_path, schema, "<r>abc</r>", {"base.xsd": base, "mid.xsd": mid})
        assert not doc.report.has_errors


def test_redefine_base_reference_keeps_its_namespace_prefix(tmp_path):
    """A redefine self-reference must stay in the redefining namespace.

    Dropping the prefix made ``base="a:c"`` rewrite to an unprefixed
    ``c|base``, which resolved through the default XML Schema namespace
    instead of the target namespace, so the renamed original could not be
    found (XSTS defaultAttributesApply s3_4_2_4ii03-ii07).
    """
    (tmp_path / "base.xsd").write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:a" xmlns:a="urn:a">'
        '<xs:complexType name="c"><xs:sequence/></xs:complexType>'
        '<xs:element name="root" type="a:c"/></xs:schema>'
    )
    (tmp_path / "main.xsd").write_text(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'targetNamespace="urn:a" xmlns:a="urn:a" defaultAttributes="a:da">'
        '<xs:redefine schemaLocation="base.xsd">'
        '<xs:complexType name="c"><xs:complexContent>'
        '<xs:extension base="a:c"><xs:sequence/></xs:extension>'
        "</xs:complexContent></xs:complexType></xs:redefine>"
        '<xs:attributeGroup name="da">'
        '<xs:attribute name="extra" type="xs:boolean" use="required"/></xs:attributeGroup>'
        "</xs:schema>"
    )
    doc = Schema.compile(str(tmp_path / "main.xsd"), mode=ParseModes.NAMESPACED).parse(
        StringIO('<a:root xmlns:a="urn:a"/>')
    )
    codes = [issue.code for issue in doc.report.errors]
    assert "unknown-type" not in codes
    # The redefined type is declared in the host document, so the host's
    # default attribute group applies and the attribute is required.
    assert "missing-attribute" in codes
