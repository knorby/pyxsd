"""Direct schema import visibility (the XSD 4.2.6 reference audit).

A schema document may reference components only in namespaces it
itself deals with: its own effective target namespace (including a
recorded chameleon adoption), the built-in XSD namespace, or a
namespace its own ``xs:import`` directives authorize. Composition
splices every composed document into one tree, which would otherwise
satisfy a reference with a component some *other* document imported;
the post-composition audit attributes each element to its authored
document and reports the missing direct import.
"""

import weakref
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from pyxsd.binding import ParseModes
from pyxsd.import_visibility import audit_direct_imports, record_authored_document
from pyxsd.namespaces import XSD_NS, NamespaceContext, clark
from pyxsd.schema import Schema
from pyxsd.validation import IssueSeverity, ValidationReport

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

#: A document for ``urn:c`` that declares one of each referable kind.
_C_DOC = f"""<xs:schema {XS} targetNamespace="urn:c" xmlns:c="urn:c">
  <xs:complexType name="ct">
    <xs:sequence><xs:element name="n" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:simpleType name="st"><xs:restriction base="xs:string"/></xs:simpleType>
  <xs:attributeGroup name="ag"><xs:attribute name="a" type="xs:string"/></xs:attributeGroup>
  <xs:element name="e">
    <xs:complexType>
      <xs:sequence><xs:element name="n" type="xs:string"/></xs:sequence>
      <xs:attribute name="a" type="xs:string"/>
    </xs:complexType>
    <xs:key name="k"><xs:selector xpath="."/><xs:field xpath="@a"/></xs:key>
  </xs:element>
</xs:schema>"""

#: A transitive supplier: it imports ``urn:c`` but references nothing.
_B_DOC = f"""<xs:schema {XS} targetNamespace="urn:b" xmlns:c="urn:c">
  <xs:import namespace="urn:c" schemaLocation="c.xsd"/>
</xs:schema>"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _errors(report):
    return [issue for issue in report.for_phase("schema") if issue.severity is IssueSeverity.ERROR]


def _codes(report):
    return {issue.code for issue in _errors(report)}


def _import_required_messages(report):
    return [issue.message for issue in _errors(report) if issue.code == "import-required"]


def _compile_transitive(tmp_path, body):
    """Compiles a main document that imports B, and B imports C.

    Every component of ``urn:c`` is therefore resolvable through the
    spliced tree even though the main document never imports ``urn:c``.
    """
    _write(tmp_path, "c.xsd", _C_DOC)
    _write(tmp_path, "b.xsd", _B_DOC)
    main = _write(
        tmp_path,
        "a.xsd",
        f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a"
  xmlns:c="urn:c" xmlns:d="urn:d">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  {body}
</xs:schema>""",
    )
    return Schema.compile(str(main), mode=ParseModes.NAMESPACED)


class TestDirectReferenceObligations:
    """Each audited QName attribute needs its own ``xs:import``."""

    def test_type_reference_requires_direct_import(self, tmp_path):
        result = _compile_transitive(tmp_path, '<xs:element name="x" type="c:ct"/>')
        messages = _import_required_messages(result.report)
        assert any("c:ct" in message for message in messages)

    def test_import_error_uses_the_declaration_origin(self, tmp_path):
        result = _compile_transitive(tmp_path, '<xs:element name="x" type="c:ct"/>')
        issues = [issue for issue in _errors(result.report) if issue.code == "import-required"]
        assert issues
        assert issues[0].location is not None
        assert issues[0].location.source.endswith("a.xsd")

    def test_ref_reference_requires_direct_import(self, tmp_path):
        result = _compile_transitive(
            tmp_path,
            '<xs:complexType name="t"><xs:sequence>'
            '<xs:element ref="c:e"/>'
            "</xs:sequence></xs:complexType>",
        )
        messages = _import_required_messages(result.report)
        assert any("c:e" in message for message in messages)

    def test_base_reference_requires_direct_import(self, tmp_path):
        result = _compile_transitive(
            tmp_path,
            '<xs:complexType name="d"><xs:complexContent>'
            '<xs:extension base="c:ct"/>'
            "</xs:complexContent></xs:complexType>",
        )
        messages = _import_required_messages(result.report)
        assert any("c:ct" in message for message in messages)

    def test_refer_attribute_requires_direct_import(self, tmp_path):
        result = _compile_transitive(
            tmp_path,
            '<xs:element name="root"><xs:complexType><xs:sequence>'
            '<xs:element name="child" type="xs:string"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:key name="own"><xs:selector xpath="."/><xs:field xpath="@a"/></xs:key>'
            '<xs:keyref name="kr" refer="c:k">'
            '<xs:selector xpath="."/><xs:field xpath="@a"/></xs:keyref>'
            "</xs:element>",
        )
        messages = _import_required_messages(result.report)
        assert any("c:k" in message for message in messages)

    def test_item_type_reference_requires_direct_import(self, tmp_path):
        result = _compile_transitive(
            tmp_path,
            '<xs:simpleType name="l"><xs:list itemType="c:st"/></xs:simpleType>',
        )
        messages = _import_required_messages(result.report)
        assert any("c:st" in message for message in messages)

    def test_default_attributes_reference_requires_direct_import(self, tmp_path):
        result = _compile_transitive(
            tmp_path,
            '<xs:complexType name="da" defaultAttributes="c:ag"/>',
        )
        messages = _import_required_messages(result.report)
        assert any("c:ag" in message for message in messages)

    def test_substitution_group_reference_requires_direct_import(self, tmp_path):
        result = _compile_transitive(
            tmp_path,
            '<xs:element name="m" substitutionGroup="c:e"/>',
        )
        messages = _import_required_messages(result.report)
        assert any("c:e" in message for message in messages)

    def test_every_member_types_name_is_audited(self, tmp_path):
        result = _compile_transitive(
            tmp_path,
            '<xs:simpleType name="u"><xs:union memberTypes="c:st d:st"/></xs:simpleType>',
        )
        messages = _import_required_messages(result.report)
        assert any("c:st" in message for message in messages)
        assert any("d:st" in message for message in messages)

    def test_caller_supplied_schema_does_not_satisfy_the_import(self, tmp_path):
        """A ``namespace_schemas`` supply is not an authored import.

        The caller makes ``urn:c`` available, so the reference resolves,
        but 4.2.6 obligations are authoring statements: only the
        document's own ``xs:import`` can satisfy them.
        """
        c_path = _write(tmp_path, "c.xsd", _C_DOC)
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:c="urn:c">
  <xs:element name="x" type="c:ct"/>
</xs:schema>""",
        )
        result = Schema.compile(
            str(main),
            mode=ParseModes.NAMESPACED,
            namespace_schemas={"urn:c": str(c_path)},
        )
        messages = _import_required_messages(result.report)
        assert any("c:ct" in message for message in messages)


class TestPerDocumentProvenance:
    """The audit judges each element by its own document's records."""

    def test_composed_document_reference_uses_its_own_record(self, tmp_path):
        """An imported document's reference needs *its* import.

        The main document imports ``urn:b`` and could resolve ``a:ctA``
        itself (its own namespace); the reference written in B cannot
        borrow that. B is mapped to its own root through
        ``composed_schema_roots``.
        """
        _write(tmp_path, "c.xsd", _C_DOC)
        _write(
            tmp_path,
            "b.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:b" xmlns:a="urn:a">
  <xs:import namespace="urn:c" schemaLocation="c.xsd"/>
  <xs:element name="b-e" type="a:ctA"/>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:complexType name="ctA">
    <xs:sequence><xs:element name="n" type="xs:string"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        messages = _import_required_messages(result.report)
        assert any("a:ctA" in message for message in messages)

    def test_import_authored_by_an_included_document_does_not_cover_main(self, tmp_path):
        """The schZ005 shape: a chameleon include authors the import.

        The included document imports ``urn:c`` and is spliced into the
        main tree, but the main document's own reference to ``urn:c``
        still needs the main document's own import.
        """
        _write(tmp_path, "c.xsd", _C_DOC)
        _write(
            tmp_path,
            "ch.xsd",
            f"""<xs:schema {XS} xmlns:c="urn:c">
  <xs:import namespace="urn:c" schemaLocation="c.xsd"/>
  <xs:element name="included-e" type="xs:string"/>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a" xmlns:c="urn:c">
  <xs:include schemaLocation="ch.xsd"/>
  <xs:element name="x" type="c:st"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        messages = _import_required_messages(result.report)
        assert any("c:st" in message for message in messages)

    def test_redefine_block_reference_uses_the_redefining_document(self, tmp_path):
        """A redefine block inside an included document is authored there.

        The main document imports only ``urn:b``. The redefinition is
        written by B and references B's import of ``urn:d``; judging it
        by the main document's imports would reject a valid schema.
        """
        _write(
            tmp_path,
            "d.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:d" xmlns:d="urn:d">
  <xs:attribute name="a" type="xs:string"/>
</xs:schema>""",
        )
        _write(
            tmp_path,
            "base.xsd",
            f"""<xs:schema {XS}>
  <xs:attributeGroup name="ag">
    <xs:attribute name="own" type="xs:string"/>
  </xs:attributeGroup>
</xs:schema>""",
        )
        _write(
            tmp_path,
            "b.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:b" xmlns:d="urn:d">
  <xs:import namespace="urn:d" schemaLocation="d.xsd"/>
  <xs:redefine schemaLocation="base.xsd">
    <xs:attributeGroup name="ag"><xs:attribute ref="d:a"/></xs:attributeGroup>
  </xs:redefine>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_redefine_block_needs_its_own_document_import(self, tmp_path):
        """The main document's import cannot authorize B's redefinition."""
        _write(
            tmp_path,
            "d.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:d" xmlns:d="urn:d">
  <xs:attribute name="a" type="xs:string"/>
</xs:schema>""",
        )
        _write(
            tmp_path,
            "base.xsd",
            f"""<xs:schema {XS}>
  <xs:attributeGroup name="ag">
    <xs:attribute name="own" type="xs:string"/>
  </xs:attributeGroup>
</xs:schema>""",
        )
        _write(
            tmp_path,
            "b.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:b" xmlns:d="urn:d">
  <xs:redefine schemaLocation="base.xsd">
    <xs:attributeGroup name="ag"><xs:attribute ref="d:a"/></xs:attributeGroup>
  </xs:redefine>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a"
  xmlns:d="urn:d">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:import namespace="urn:d" schemaLocation="d.xsd"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        messages = _import_required_messages(result.report)
        assert any("d:a" in message for message in messages)

    def test_override_block_inside_an_included_document_uses_that_document(self, tmp_path):
        """An override written by B is judged by B, not its base or host."""
        _write(
            tmp_path,
            "d.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:d" xmlns:d="urn:d">
  <xs:complexType name="dt">
    <xs:sequence><xs:element name="n" type="xs:string"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        _write(
            tmp_path,
            "base.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:b" xmlns:b="urn:b">
  <xs:complexType name="ProductType">
    <xs:sequence><xs:element name="number" type="xs:integer"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        _write(
            tmp_path,
            "b.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:b" xmlns:b="urn:b" xmlns:d="urn:d">
  <xs:import namespace="urn:d" schemaLocation="d.xsd"/>
  <xs:override schemaLocation="base.xsd">
    <xs:complexType name="ProductType">
      <xs:sequence><xs:element name="detail" type="d:dt"/></xs:sequence>
    </xs:complexType>
  </xs:override>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)


class TestPositiveControls:
    """Valid visibility shapes stay error-free after the audit."""

    def test_direct_import_with_schema_location(self, tmp_path):
        _write(tmp_path, "c.xsd", _C_DOC)
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:c="urn:c">
  <xs:import namespace="urn:c" schemaLocation="c.xsd"/>
  <xs:element name="x" type="c:ct"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert not _errors(result.report)

    def test_namespace_only_import_with_supplied_schema(self, tmp_path):
        c_path = _write(tmp_path, "c.xsd", _C_DOC)
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:c="urn:c">
  <xs:import namespace="urn:c"/>
  <xs:element name="x" type="c:ct"/>
</xs:schema>""",
        )
        result = Schema.compile(
            str(main),
            mode=ParseModes.NAMESPACED,
            namespace_schemas={"urn:c": str(c_path)},
        )
        assert "import-required" not in _codes(result.report)

    def test_same_namespace_include_references(self, tmp_path):
        _write(
            tmp_path,
            "a2.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:complexType name="t2">
    <xs:sequence><xs:element name="two" type="a:t1"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:include schemaLocation="a2.xsd"/>
  <xs:complexType name="t1">
    <xs:sequence><xs:element name="one" type="a:t2"/></xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="a:t1"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_absent_namespace_import_reference(self, tmp_path):
        _write(
            tmp_path,
            "noNs.xsd",
            f"""<xs:schema {XS}>
  <xs:simpleType name="bare"><xs:restriction base="xs:string"/></xs:simpleType>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a">
  <xs:import schemaLocation="noNs.xsd"/>
  <xs:element name="x" type="bare"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_circular_imports_references(self, tmp_path):
        _write(
            tmp_path,
            "b.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:b" xmlns:a="urn:a">
  <xs:import namespace="urn:a" schemaLocation="a.xsd"/>
  <xs:complexType name="bt">
    <xs:sequence><xs:element name="n" type="a:at"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a" xmlns:b="urn:b">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:complexType name="at">
    <xs:sequence><xs:element name="n" type="b:bt"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_diamond_imports_references(self, tmp_path):
        _write(
            tmp_path,
            "d.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:d" xmlns:d="urn:d">
  <xs:complexType name="dt">
    <xs:sequence><xs:element name="n" type="xs:string"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        shared = """<xs:schema {xs_ns} targetNamespace="urn:{name}" xmlns:d="urn:d">
  <xs:import namespace="urn:d" schemaLocation="d.xsd"/>
  <xs:complexType name="{name}t">
    <xs:sequence><xs:element name="n" type="d:dt"/></xs:sequence>
  </xs:complexType>
</xs:schema>"""
        _write(tmp_path, "b.xsd", shared.format(xs_ns=XS, name="b"))
        _write(
            tmp_path,
            "c.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:c" xmlns:d="urn:d">
  <xs:import namespace="urn:d" schemaLocation="d.xsd"/>
  <xs:complexType name="ct">
    <xs:sequence><xs:element name="n" type="d:dt"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:b="urn:b" xmlns:c="urn:c">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:import namespace="urn:c" schemaLocation="c.xsd"/>
  <xs:complexType name="at">
    <xs:sequence>
      <xs:element name="one" type="b:bt"/>
      <xs:element name="two" type="c:ct"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_builtin_xsd_types_need_no_import(self, tmp_path):
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:simpleType name="l"><xs:list itemType="xs:string"/></xs:simpleType>
  <xs:simpleType name="u"><xs:union memberTypes="xs:string a:l"/></xs:simpleType>
  <xs:complexType name="t">
    <xs:sequence><xs:element name="n" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="r">
    <xs:complexContent><xs:extension base="a:t"/></xs:complexContent>
  </xs:complexType>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert not _errors(result.report)

    def test_builtin_vocabulary_namespaces_need_no_import(self, tmp_path):
        """``xml``/``xsi``/``xlink`` references stay import-free.

        Their attribute declarations are built into the compiler (the
        strict-mode injections supply them), exactly as the XSD
        built-in types are; referencing them is not a 4.2.6 violation.
        """
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a"
  xmlns:xml="http://www.w3.org/XML/1998/namespace"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:xlink="http://www.w3.org/1999/xlink">
  <xs:complexType name="t">
    <xs:sequence><xs:element name="n" type="xs:string"/></xs:sequence>
    <xs:attribute ref="xml:space"/>
    <xs:attribute ref="xsi:type"/>
    <xs:attribute ref="xlink:href"/>
  </xs:complexType>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_unprefixed_reference_without_default_namespace_is_allowed(self, tmp_path):
        """A bare name denotes the document's own lenient lookup scope.

        pyxsd's component lookup matches bare local names without a
        default-namespace declaration, so the visibility audit treats
        a reference that names no namespace as the document's own.
        """
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a">
  <xs:complexType name="t">
    <xs:sequence><xs:element name="n" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="t"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_chameleon_include_references_and_own_import(self, tmp_path):
        """A chameleon include keeps its own imports and adoption."""
        _write(tmp_path, "c.xsd", _C_DOC)
        _write(
            tmp_path,
            "ch.xsd",
            f"""<xs:schema {XS} xmlns:c="urn:c">
  <xs:import namespace="urn:c" schemaLocation="c.xsd"/>
  <xs:simpleType name="chSt"><xs:restriction base="c:st"/></xs:simpleType>
  <xs:complexType name="chCt">
    <xs:sequence><xs:element name="n" type="chSt"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:include schemaLocation="ch.xsd"/>
  <xs:element name="x" type="a:chCt"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_adopted_redefine_namespace_references(self, tmp_path):
        """A redefined chameleon base is judged by its adopted namespace.

        The base document's root is not stamped with the adopting
        namespace (unlike a plain include), so the audit needs the
        recorded ``adoptedNS`` or its rewritten self-references would
        look like foreign-namespace references.
        """
        _write(
            tmp_path,
            "base.xsd",
            f"""<xs:schema {XS}>
  <xs:attribute name="base-a" type="xs:string"/>
  <xs:group name="g1">
    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>
  </xs:group>
  <xs:complexType name="baseType">
    <xs:sequence><xs:group ref="g1"/></xs:sequence>
    <xs:attribute ref="base-a"/>
  </xs:complexType>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:redef" xmlns="urn:redef">
  <xs:redefine schemaLocation="base.xsd">
    <xs:group name="g1"><xs:sequence>
      <xs:group ref="g1"/>
      <xs:element name="b" type="xs:string"/>
    </xs:sequence></xs:group>
  </xs:redefine>
  <xs:complexType name="mainType"><xs:group ref="g1"/></xs:complexType>
  <xs:element name="root" type="mainType"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_override_block_reference_uses_the_overriding_document(self, tmp_path):
        """An ``xs:override`` declaration is authored by its overrider.

        XSD 1.1 §4.2.5 (W3C bug 17574, Saxon over029): the override
        block's references are compiled in the *overriding* document's
        context, so the overrider's import satisfies them even though
        the overridden document never imports the namespace.
        """
        _write(
            tmp_path,
            "base.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:complexType name="ProductType">
    <xs:sequence><xs:element name="number" type="xs:integer"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        _write(
            tmp_path,
            "spc.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:spc" xmlns:spc="urn:spc">
  <xs:element name="giftWrap" type="xs:string"/>
  <xs:attribute name="points" type="xs:nonNegativeInteger"/>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a" xmlns:spc="urn:spc">
  <xs:import namespace="urn:spc" schemaLocation="spc.xsd"/>
  <xs:override schemaLocation="base.xsd">
    <xs:complexType name="ProductType">
      <xs:sequence>
        <xs:element name="number" type="xs:integer"/>
        <xs:element ref="spc:giftWrap" minOccurs="0"/>
      </xs:sequence>
      <xs:attribute ref="spc:points"/>
    </xs:complexType>
  </xs:override>
  <xs:element name="order" type="a:ProductType"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_override_block_still_needs_its_own_import(self, tmp_path):
        """The overrider's context does not remove the obligation.

        An override-block reference into a namespace neither document
        imports is still reported.
        """
        _write(
            tmp_path,
            "base.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:complexType name="ProductType">
    <xs:sequence><xs:element name="number" type="xs:integer"/></xs:sequence>
  </xs:complexType>
</xs:schema>""",
        )
        _write(
            tmp_path,
            "spc.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:spc" xmlns:spc="urn:spc">
  <xs:element name="giftWrap" type="xs:string"/>
</xs:schema>""",
        )
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a" xmlns:spc="urn:spc"
  xmlns:far="urn:far">
  <xs:import namespace="urn:spc" schemaLocation="spc.xsd"/>
  <xs:override schemaLocation="base.xsd">
    <xs:complexType name="ProductType">
      <xs:sequence>
        <xs:element name="number" type="xs:integer"/>
        <xs:element ref="far:giftWrap" minOccurs="0"/>
      </xs:sequence>
    </xs:complexType>
  </xs:override>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        messages = _import_required_messages(result.report)
        assert any("far:giftWrap" in message for message in messages)


class TestAuditBoundaries:
    """What the audit must not touch."""

    def test_foreign_annotation_payload_is_ignored(self, tmp_path):
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:c="urn:c" xmlns:ext="urn:ext">
  <xs:element name="x" type="xs:string">
    <xs:annotation>
      <xs:appinfo>
        <ext:node ref="c:e" type="c:ct" base="c:ct" memberTypes="c:st"/>
      </xs:appinfo>
      <xs:documentation>
        <ext:doc ref="c:e" type="c:ct"/>
      </xs:documentation>
    </xs:annotation>
  </xs:element>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_schema_element_inside_annotation_payload_is_ignored(self, tmp_path):
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:c="urn:c">
  <xs:element name="x" type="xs:string">
    <xs:annotation>
      <xs:appinfo><xs:element ref="c:e"/></xs:appinfo>
    </xs:annotation>
  </xs:element>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)

    def test_unbound_prefix_keeps_its_namespace_error_only(self, tmp_path):
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a">
  <xs:element name="x" type="nope:ct"/>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "unknown-namespace-prefix" in _codes(result.report)
        assert "import-required" not in _codes(result.report)

    def test_non_reference_qnames_are_not_audited(self, tmp_path):
        """``notQName``, QName defaults and XPath values stay out."""
        main = _write(
            tmp_path,
            "a.xsd",
            f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:c="urn:c">
  <xs:complexType name="t">
    <xs:sequence>
      <xs:any namespace="##any" processContents="lax" notQName="c:e"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="q" type="xs:QName" default="c:sym"/>
  <xs:element name="root" type="t">
    <xs:key name="k"><xs:selector xpath="c:sel"/><xs:field xpath="@a"/></xs:key>
  </xs:element>
</xs:schema>""",
        )
        result = Schema.compile(str(main), mode=ParseModes.NAMESPACED)
        assert "import-required" not in _codes(result.report)


class TestHelperGuards:
    """Direct calls for the branches a parsed document cannot produce.

    The stdlib parser drops comments, so the comment guards and the
    fabricated-tree paths are exercised through the helpers directly,
    as ``tests/test_schema_composition_coverage.py`` does for the
    composition helpers.
    """

    @staticmethod
    def _recorder():
        return SimpleNamespace(
            report=ValidationReport(),
            namespace_context=NamespaceContext(),
            composed_schema_roots={},
            authored_element_roots={},
            authored_import_namespaces={},
            authored_effective_namespaces={},
            declaration_origins=weakref.WeakKeyDictionary(),
        )

    def test_record_skips_comments_and_non_import_children(self):
        root = ET.Element(clark(XSD_NS, "schema"), {"targetNamespace": "urn:a"})
        root.append(ET.Comment("a note"))
        ET.SubElement(root, "{urn:ext}import", {"namespace": "urn:ext"})
        ET.SubElement(root, clark(XSD_NS, "import"), {"namespace": "urn:c"})
        ET.SubElement(root, clark(XSD_NS, "import"))
        ET.SubElement(root, clark(XSD_NS, "include"), {"schemaLocation": "b.xsd"})
        ctx = self._recorder()
        record_authored_document(ctx, root)
        assert ctx.authored_import_namespaces[id(root)] == {"urn:c", None}
        assert ctx.authored_effective_namespaces[id(root)] == "urn:a"

    def test_record_prefers_declared_namespace_over_adopted(self):
        root = ET.Element(clark(XSD_NS, "schema"), {"targetNamespace": "urn:own"})
        ctx = self._recorder()
        record_authored_document(ctx, root, adopted_namespace="urn:adopted")
        assert ctx.authored_effective_namespaces[id(root)] == "urn:own"

    def test_record_marks_unstamped_root_by_its_adopted_namespace(self):
        root = ET.Element(clark(XSD_NS, "schema"))
        ctx = self._recorder()
        record_authored_document(ctx, root, adopted_namespace="urn:adopted")
        assert ctx.authored_effective_namespaces[id(root)] == "urn:adopted"
        assert ctx.authored_import_namespaces[id(root)] == set()

    def test_audit_walk_skips_comments_and_foreign_elements(self):
        root = ET.Element(clark(XSD_NS, "schema"))
        root.append(ET.Comment("a note"))
        ET.SubElement(root, "{urn:ext}node", {"type": "c:ct"})
        ET.SubElement(root, clark(XSD_NS, "element"), {"name": "x", "type": "c:ct"})
        ctx = self._recorder()
        ctx.namespace_context.record(root, {"c": "urn:c"})
        record_authored_document(ctx, root)
        audit_direct_imports(ctx, root)
        assert len(_import_required_messages(ctx.report)) == 1
