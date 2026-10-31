"""Coverage-rounding tests for ``pyxsd.schema_composition``.

Composition is driven end to end through :meth:`Schema.compile` on
multi-document schemas written to ``tmp_path`` (as ``tests/test_composition.py``
does), with direct calls to the pure/defensive helpers for the branches a
parsed document cannot produce.
"""

import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from pyxsd.binding import ParseModes
from pyxsd.namespaces import XSD_NS, NamespaceContext, clark
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.schema import Schema
from pyxsd.schema_composition import (
    chameleon_qname,
    check_directive_annotation,
)
from pyxsd.validation import IssueSeverity, ValidationReport
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


def _compile(tmp_path, schema, files=None, **kwargs):
    (tmp_path / "schema.xsd").write_text(schema, encoding="utf-8")
    for name, content in (files or {}).items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    return Schema.compile(str(tmp_path / "schema.xsd"), **kwargs)


def _codes(schema):
    return {issue.code for issue in schema.report.errors}


def _warning_codes(schema):
    return {issue.code for issue in schema.report.issues if issue.severity is IssueSeverity.WARNING}


class TestAdditionalSchemaSources:
    """``collect_additional_schemas`` path handling."""

    def test_relative_namespace_schema_path_resolves(self, tmp_path):
        schema = (
            f'<xs:schema {XS} xmlns:x="urn:x">'
            '<xs:import namespace="urn:x"/>'
            '<xs:element name="root" type="x:extra"/>'
            "</xs:schema>"
        )
        dep = (
            f'<xs:schema {XS} targetNamespace="urn:x" xmlns:x="urn:x">'
            '<xs:complexType name="extra"><xs:sequence/></xs:complexType>'
            "</xs:schema>"
        )
        result = _compile(
            tmp_path, schema, {"dep.xsd": dep}, namespace_schemas={"urn:x": "dep.xsd"}
        )
        assert "extra" in result.classes
        assert _codes(result) == set()

    def test_namespace_schema_equal_to_main_is_skipped(self, tmp_path):
        schema = (
            f"<xs:schema {XS}>"
            '<xs:complexType name="commonType"><xs:sequence/></xs:complexType>'
            '<xs:element name="root" type="commonType"/>'
            "</xs:schema>"
        )
        result = _compile(
            tmp_path,
            schema,
            namespace_schemas={"urn:x": str(tmp_path / "schema.xsd")},
        )
        # The entry names the main document itself, so it is not loaded
        # twice (a second splice would duplicate the declaration).
        assert "commonType" in result.classes
        assert _codes(result) == set()

    def test_relative_schema_location_hint_path_resolves(self, tmp_path):
        schema = (
            f'<xs:schema {XS} xmlns:x="urn:x">'
            '<xs:import namespace="urn:x"/>'
            '<xs:element name="root" type="x:extra"/>'
            "</xs:schema>"
        )
        hint = (
            f'<xs:schema {XS} targetNamespace="urn:x" xmlns:x="urn:x">'
            '<xs:complexType name="extra"><xs:sequence/></xs:complexType>'
            "</xs:schema>"
        )
        result = _compile(
            tmp_path,
            schema,
            {"hint.xsd": hint},
            mode=ParseModes.NAMESPACED,
            schema_location_pairs=[("urn:x", "hint.xsd")],
        )
        assert "extra" in result.classes

    def test_remote_hint_is_a_policy_error_not_a_missing_hint_warning(self, tmp_path):
        schema = f'<xs:schema {XS}><xs:element name="root" type="xs:string"/></xs:schema>'
        with pytest.raises(ResourcePolicyError, match="remote-capable resource policy"):
            _compile(
                tmp_path,
                schema,
                mode=ParseModes.NAMESPACED,
                schema_location_pairs=[("urn:x", "https://schemas.example/x.xsd")],
            )


class TestDirectiveIds:
    """``note_directive_id`` duplicate reporting."""

    def test_duplicate_directive_id_is_reported(self, tmp_path):
        schema = (
            f"<xs:schema {XS}>"
            '<xs:import namespace="urn:b" id="dup"/>'
            '<xs:import namespace="urn:c" id="dup"/>'
            '<xs:element name="root"/>'
            "</xs:schema>"
        )
        result = _compile(tmp_path, schema)
        assert "declaration-duplicate" in _codes(result)


class TestImportNamespaceEdges:
    """``xs:import`` namespace-attribute edge cases."""

    def test_namespaceless_import_of_namespaced_document_is_error(self, tmp_path):
        schema = (
            f"<xs:schema {XS}>"
            '<xs:import schemaLocation="base.xsd"/>'
            '<xs:element name="root"/>'
            "</xs:schema>"
        )
        base = f'<xs:schema {XS} targetNamespace="urn:b"><xs:element name="e"/></xs:schema>'
        result = _compile(tmp_path, schema, {"base.xsd": base})
        # The import names the absent namespace but the document declares one.
        assert "compose-invalid" in _codes(result)


class TestXsd10VocabularyInInclude:
    """The 1.0 vocabulary gate runs on included documents too."""

    def test_include_with_11_vocabulary_is_reported_in_10_mode(self, tmp_path):
        schema = (
            f'<xs:schema {XS} xmlns:t="urn:t">'
            '<xs:include schemaLocation="base.xsd"/>'
            '<xs:element name="root" type="xs:string"/>'
            "</xs:schema>"
        )
        base = (
            f'<xs:schema {XS} xmlns:t="urn:t" targetNamespace="urn:t">'
            '<xs:element name="e" type="xs:dateTimeStamp"/>'
            "</xs:schema>"
        )
        result = _compile(tmp_path, schema, {"base.xsd": base}, xsd_version="1.0")
        vocabulary = [issue for issue in result.report.errors if issue.code == "xsd11-construct"]
        assert vocabulary and vocabulary[0].element == "dateTimeStamp"


class TestChameleonQnameHelper:
    """``chameleon_qname``'s early returns."""

    def _context(self):
        return SimpleNamespace(namespace_context=NamespaceContext())

    def test_expanded_and_keyword_tokens_are_returned_unchanged(self):
        ctx = self._context()
        element = ET.Element(clark("urn:x", "foo"))
        assert chameleon_qname(ctx, element, "{urn:x}t", "urn:a") == "{urn:x}t"
        assert chameleon_qname(ctx, element, "##any", "urn:a") == "##any"
        assert chameleon_qname(ctx, element, "", "urn:a") == ""

    def test_unbound_prefix_is_returned_unchanged(self):
        ctx = self._context()
        element = ET.Element(clark("urn:x", "foo"))
        assert chameleon_qname(ctx, element, "zzz:t", "urn:a") == "zzz:t"


class TestDirectiveAnnotationGuard:
    """``check_directive_annotation``'s comment/foreign-child guards."""

    def test_comment_and_foreign_child_are_ignored(self):
        report = ValidationReport()
        tag = ET.Element(clark(XSD_NS, "include"))
        tag.append(ET.Comment("a note"))
        ET.SubElement(tag, "{urn:foreign}other")
        ET.SubElement(tag, clark(XSD_NS, "annotation"))
        ET.SubElement(tag, clark(XSD_NS, "sequence"))
        check_directive_annotation(SimpleNamespace(report=report), tag, False)
        messages = [issue.message for issue in report.errors]
        assert len(messages) == 1
        assert "sequence" in messages[0]


class TestNamespaceReferences:
    """The namespace-reference scan behind an unresolved import."""

    def test_scan_reads_qname_attributes_and_skips_foreign_elements(self, tmp_path):
        schema = (
            f'<xs:schema {XS} xmlns:b="urn:b" xmlns:app="urn:app">'
            '<xs:import namespace="urn:b"/>'
            "<app:marker/>"
            '<xs:element name="root" type="b:t" substitutionGroup="zzz:head"/>'
            "</xs:schema>"
        )
        result = _compile(tmp_path, schema, mode=ParseModes.NAMESPACED)
        # urn:b is referenced by the ``type`` QName, so the namespace-only
        # import is fatal; the unbound substitutionGroup prefix is skipped.
        assert "import-unresolved" in _codes(result)


class TestRedefineCycles:
    """``splice_redefine`` cycle verdicts."""

    def test_namespaced_circular_redefine_is_an_error(self, tmp_path):
        a = (
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:redefine schemaLocation="b.xsd"/>'
            "</xs:schema>"
        )
        b = (
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:redefine schemaLocation="a.xsd"/>'
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:include schemaLocation="a.xsd"/>'
            "</xs:schema>"
        )
        result = _compile(tmp_path, main, {"a.xsd": a, "b.xsd": b}, mode=ParseModes.NAMESPACED)
        assert "compose-invalid" in _codes(result)


class TestOverrideEdges:
    """``splice_override`` direct edge cases."""

    def test_override_without_schema_location_is_error(self, tmp_path):
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override><xs:element name="doc" type="xs:string"/></xs:override>'
            '<xs:element name="root" type="xs:string"/>'
            "</xs:schema>"
        )
        result = _compile(tmp_path, schema)
        assert "schema-compose" in _codes(result)

    def test_override_annotation_child_is_ignored(self, tmp_path):
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd"><xs:annotation/></xs:override>'
            '<xs:element name="root" type="xs:string"/>'
            "</xs:schema>"
        )
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        result = _compile(tmp_path, schema, {"base.xsd": base})
        assert "override-invalid" not in _codes(result)

    def test_override_child_without_name_is_error(self, tmp_path):
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd"><xs:complexType/></xs:override>'
            '<xs:element name="root" type="xs:string"/>'
            "</xs:schema>"
        )
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        result = _compile(tmp_path, schema, {"base.xsd": base})
        assert "override-invalid" in _codes(result)

    def test_base_annotation_component_is_skipped(self, tmp_path):
        base = (
            f'<xs:schema {XS}><xs:annotation/><xs:element name="doc" type="xs:string"/></xs:schema>'
        )
        schema = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:date"/></xs:override>'
            '<xs:element name="root" type="xs:string"/>'
            "</xs:schema>"
        )
        result = _compile(tmp_path, schema, {"base.xsd": base})
        assert "override-invalid" not in _codes(result)

    def test_circular_override_is_warning(self, tmp_path):
        a = f'<xs:schema {XS}><xs:override schemaLocation="b.xsd"/></xs:schema>'
        b = f'<xs:schema {XS}><xs:override schemaLocation="a.xsd"/></xs:schema>'
        main = (
            f"<xs:schema {XS}>"
            '<xs:include schemaLocation="a.xsd"/>'
            '<xs:element name="root" type="xs:string"/>'
            "</xs:schema>"
        )
        result = _compile(tmp_path, main, {"a.xsd": a, "b.xsd": b})
        assert "compose-cycle" in _warning_codes(result)

    def test_nested_override_of_same_base_is_not_a_conflict(self, tmp_path):
        base = f'<xs:schema {XS}><xs:element name="doc" type="xs:string"/></xs:schema>'
        a = (
            f"<xs:schema {XS}>"
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:date"/></xs:override>'
            "</xs:schema>"
        )
        main = (
            f"<xs:schema {XS}>"
            '<xs:include schemaLocation="a.xsd"/>'
            '<xs:override schemaLocation="base.xsd">'
            '<xs:element name="doc" type="xs:time"/></xs:override>'
            "</xs:schema>"
        )
        result = _compile(tmp_path, main, {"a.xsd": a, "base.xsd": base})
        assert "override-invalid" not in _codes(result)


class TestRedefineRestrictions:
    """``check_redefine_restrictions`` edge branches."""

    def _redefine_attribute_group(self, tmp_path, base_body, derived_body):
        base = f"<xs:schema {XS}>{base_body}</xs:schema>"
        main = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            f'<xs:attributeGroup name="ag">{derived_body}</xs:attributeGroup>'
            '</xs:redefine><xs:element name="root" type="xs:string"/></xs:schema>'
        )
        return _compile(tmp_path, main, {"base.xsd": base})

    def test_redefine_missing_attribute_group_is_error(self, tmp_path):
        base = (
            f"<xs:schema {XS}>"
            '<xs:attributeGroup name="other"><xs:attribute name="a"/></xs:attributeGroup>'
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>'
            '</xs:redefine><xs:element name="root" type="xs:string"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"base.xsd": base})
        assert "compose-invalid" in _codes(result)

    def test_redefine_unnamed_attribute_group_is_skipped(self, tmp_path):
        base = (
            f"<xs:schema {XS}>"
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>'
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            "<xs:attributeGroup/>"
            '</xs:redefine><xs:element name="root" type="xs:string"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"base.xsd": base})
        # The nameless group is otherwise invalid, but the restriction check
        # itself reports nothing for it.
        assert "declaration-name" in _codes(result)
        assert "compose-invalid" not in _codes(result)

    def test_redefine_base_with_referenced_group_is_skipped(self, tmp_path):
        base = (
            f"<xs:schema {XS}>"
            '<xs:attributeGroup name="other"><xs:attribute name="a"/></xs:attributeGroup>'
            '<xs:attributeGroup name="ag"><xs:attributeGroup ref="other"/></xs:attributeGroup>'
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            '<xs:attributeGroup name="ag">'
            '<xs:attributeGroup ref="ag"/><xs:attribute name="a"/>'
            "</xs:attributeGroup>"
            '</xs:redefine><xs:element name="root" type="xs:string"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"base.xsd": base})
        assert "compose-invalid" not in _codes(result)

    def test_redefine_with_attribute_reference_is_skipped(self, tmp_path):
        base = (
            f"<xs:schema {XS}>"
            '<xs:attribute name="a" type="xs:string"/>'
            '<xs:attributeGroup name="ag"><xs:attribute name="a" type="xs:string"/></xs:attributeGroup>'
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            '<xs:attributeGroup name="ag"><xs:attribute ref="a"/></xs:attributeGroup>'
            '</xs:redefine><xs:element name="root" type="xs:string"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"base.xsd": base})
        assert "compose-invalid" not in _codes(result)

    def test_redefine_nameless_attribute_is_skipped(self, tmp_path):
        result = self._redefine_attribute_group(
            tmp_path,
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>',
            "<xs:attribute/>",
        )
        assert "compose-invalid" not in _codes(result)

    def test_redefine_annotation_child_is_ignored(self, tmp_path):
        result = self._redefine_attribute_group(
            tmp_path,
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>',
            '<xs:annotation/><xs:attribute name="a"/>',
        )
        assert "compose-invalid" not in _codes(result)

    def test_redefine_other_group_reference_is_skipped(self, tmp_path):
        base = (
            f"<xs:schema {XS}>"
            '<xs:attributeGroup name="other"><xs:attribute name="b"/></xs:attributeGroup>'
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>'
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            '<xs:attributeGroup name="ag"><xs:attributeGroup ref="other"/></xs:attributeGroup>'
            '</xs:redefine><xs:element name="root" type="xs:string"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"base.xsd": base})
        assert "compose-invalid" not in _codes(result)

    def test_redefine_double_self_reference_is_error(self, tmp_path):
        result = self._redefine_attribute_group(
            tmp_path,
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>',
            '<xs:attributeGroup ref="ag"/><xs:attributeGroup ref="ag"/>',
        )
        messages = [issue.message for issue in result.report.errors]
        assert any("references itself" in message for message in messages)

    def test_redefine_drops_required_attribute_is_error(self, tmp_path):
        result = self._redefine_attribute_group(
            tmp_path,
            '<xs:attributeGroup name="ag">'
            '<xs:attribute name="a" type="xs:string" use="required"/></xs:attributeGroup>',
            "",
        )
        messages = [issue.message for issue in result.report.errors]
        assert any("required attribute 'a'" in message for message in messages)

    def test_redefine_changes_use_is_error(self, tmp_path):
        result = self._redefine_attribute_group(
            tmp_path,
            '<xs:attributeGroup name="ag">'
            '<xs:attribute name="a" type="xs:string" use="required"/></xs:attributeGroup>',
            '<xs:attribute name="a" type="xs:string"/>',
        )
        messages = [issue.message for issue in result.report.errors]
        assert any("changes its use" in message for message in messages)

    def test_find_base_component_skips_earlier_children(self, tmp_path):
        base = (
            f"<xs:schema {XS}>"
            "<xs:annotation/>"
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>'
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>'
            '</xs:redefine><xs:element name="root" type="xs:string"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"base.xsd": base})
        assert "compose-invalid" not in _codes(result)

    def test_find_base_component_falls_back_to_nested_redefine(self, tmp_path):
        inner = (
            f"<xs:schema {XS}>"
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>'
            "</xs:schema>"
        )
        base = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="inner.xsd">'
            "<xs:annotation/>"
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>'
            "</xs:redefine></xs:schema>"
        )
        main = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>'
            '</xs:redefine><xs:element name="root" type="xs:string"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"base.xsd": base, "inner.xsd": inner})
        assert "compose-invalid" not in _codes(result)

    def test_redefine_reached_through_shared_include_is_not_a_conflict(self, tmp_path):
        base = (
            f'<xs:schema {XS}><xs:group name="g"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/></xs:sequence></xs:group>'
            "</xs:schema>"
        )
        a = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:sequence><xs:group ref="g"/>'
            '<xs:element name="b" type="xs:string"/></xs:sequence></xs:group>'
            "</xs:redefine></xs:schema>"
        )
        main = (
            f'<xs:schema {XS}><xs:include schemaLocation="a.xsd"/>'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:group name="g"><xs:sequence><xs:group ref="g"/>'
            '<xs:element name="c" type="xs:string"/></xs:sequence></xs:group>'
            "</xs:redefine>"
            '<xs:element name="root" type="xs:string"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"a.xsd": a, "base.xsd": base})
        messages = [issue.message for issue in result.report.errors]
        assert not any("redefined more than once" in message for message in messages)

    def test_attribute_uses_skips_annotation_and_nameless(self, tmp_path):
        base = (
            f"<xs:schema {XS}>"
            '<xs:attributeGroup name="ag">'
            "<xs:annotation/>"
            "<xs:attribute/>"
            '<xs:attribute name="a"/>'
            "</xs:attributeGroup>"
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS}><xs:redefine schemaLocation="base.xsd">'
            '<xs:attributeGroup name="ag"><xs:attribute name="a"/></xs:attributeGroup>'
            '</xs:redefine><xs:element name="root" type="xs:string"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"base.xsd": base})
        assert "compose-invalid" not in _codes(result)


class TestChameleonRedefineRebind:
    """``rebind_redefine_references`` on a chameleon redefine."""

    def test_chameleon_redefine_self_base_is_rebound(self, tmp_path):
        base = (
            f'<xs:schema {XS}><xs:complexType name="c"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/></xs:sequence></xs:complexType>'
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns="urn:a">'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:complexType name="c"><xs:complexContent>'
            '<xs:restriction base="c"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/></xs:sequence>'
            "</xs:restriction></xs:complexContent></xs:complexType>"
            '</xs:redefine><xs:element name="root" type="c"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"base.xsd": base}, mode=ParseModes.NAMESPACED)
        assert "c" in result.classes
        assert "compose-invalid" not in _codes(result)

    def test_chameleon_redefine_unbound_self_base_is_error(self, tmp_path):
        base = (
            f'<xs:schema {XS}><xs:complexType name="c"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/></xs:sequence></xs:complexType>'
            "</xs:schema>"
        )
        main = (
            f'<xs:schema {XS} targetNamespace="urn:a" xmlns="urn:a">'
            '<xs:redefine schemaLocation="base.xsd">'
            '<xs:complexType name="c"><xs:complexContent>'
            '<xs:restriction base="zzz:c"><xs:sequence>'
            '<xs:element name="a" type="xs:string"/></xs:sequence>'
            "</xs:restriction></xs:complexContent></xs:complexType>"
            '</xs:redefine><xs:element name="root" type="c"/></xs:schema>'
        )
        result = _compile(tmp_path, main, {"base.xsd": base}, mode=ParseModes.NAMESPACED)
        messages = [issue.message for issue in result.report.errors]
        assert any("must be qualified" in message for message in messages)


class TestLocalTargetNamespaces:
    """``apply_local_target_namespaces`` rule branches (XSD 1.1 §3.2.3)."""

    def test_form_and_target_namespace_together_is_error(self, tmp_path):
        schema = (
            f"<xs:schema {XS}>"
            '<xs:element name="root"><xs:complexType><xs:sequence>'
            '<xs:element name="c" form="unqualified" targetNamespace="urn:a"/>'
            "</xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        result = _compile(tmp_path, schema)
        assert "declaration-attribute" in _codes(result)

    def test_global_declaration_with_target_namespace_is_error(self, tmp_path):
        schema = f'<xs:schema {XS}><xs:element name="e" targetNamespace="urn:a"/></xs:schema>'
        result = _compile(tmp_path, schema)
        messages = [issue.message for issue in result.report.errors]
        assert any("global element declaration" in message for message in messages)

    def test_reference_with_target_namespace_is_error(self, tmp_path):
        schema = (
            f"<xs:schema {XS}>"
            '<xs:element name="g" type="xs:string"/>'
            '<xs:element name="root"><xs:complexType><xs:sequence>'
            '<xs:element ref="g" targetNamespace="urn:a"/>'
            "</xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        result = _compile(tmp_path, schema)
        messages = [issue.message for issue in result.report.errors]
        assert any("reference may not carry 'targetNamespace'" in message for message in messages)

    def test_differing_local_namespace_outside_restriction_is_error(self, tmp_path):
        schema = (
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:element name="root"><xs:complexType><xs:sequence>'
            '<xs:element name="c" targetNamespace="urn:a"/>'
            "</xs:sequence></xs:complexType></xs:element>"
            "</xs:schema>"
        )
        result = _compile(tmp_path, schema)
        assert "declaration-attribute" in _codes(result)

    def test_differing_namespace_inside_anytype_restriction_is_error(self, tmp_path):
        schema = (
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t">'
            '<xs:complexType name="r"><xs:complexContent>'
            '<xs:restriction base="xs:anyType"><xs:sequence>'
            '<xs:element name="c" targetNamespace="urn:a"/>'
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
            "</xs:schema>"
        )
        result = _compile(tmp_path, schema)
        messages = [issue.message for issue in result.report.errors]
        assert any("restriction of xs:anyType" in message for message in messages)

    def test_local_namespace_inside_restriction_is_allowed(self, tmp_path):
        schema = (
            f'<xs:schema {XS} targetNamespace="urn:t" xmlns="urn:t" '
            'elementFormDefault="qualified">'
            '<xs:complexType name="b"><xs:sequence>'
            '<xs:element name="c" type="xs:string"/></xs:sequence></xs:complexType>'
            '<xs:complexType name="r"><xs:complexContent><xs:restriction base="b">'
            '<xs:sequence><xs:element name="c" type="xs:string" targetNamespace="urn:other"/>'
            "</xs:sequence></xs:restriction></xs:complexContent></xs:complexType>"
            "</xs:schema>"
        )
        result = _compile(tmp_path, schema)
        assert "declaration-attribute" not in _codes(result)


class TestComposedParserPolicies:
    """Compile-time parser policy and limits reach dependency documents."""

    @pytest.mark.parametrize("directive", ["include", "import"])
    def test_composed_schema_parse_applies_compile_xml_limits(self, tmp_path, directive):
        imported = directive == "import"
        namespace = ' namespace="urn:part"' if imported else ""
        target = ' targetNamespace="urn:part"' if imported else ""
        main = (
            f'<xs:schema {XS}><xs:{directive}{namespace} schemaLocation="part.xsd"/>'
            '<xs:element name="root" type="xs:string"/></xs:schema>'
        )
        part = (
            f"<xs:schema {XS}{target}><!--{'padded' * 40}-->"
            '<xs:element name="part" type="xs:string"/></xs:schema>'
        )
        main_bytes = main.encode("utf-8")
        part_bytes = part.encode("utf-8")
        assert len(part_bytes) > len(main_bytes)
        (tmp_path / "main.xsd").write_bytes(main_bytes)
        (tmp_path / "part.xsd").write_bytes(part_bytes)
        with pytest.raises(ResourcePolicyError, match="max_bytes"):
            Schema.compile(
                str(tmp_path / "main.xsd"),
                xml_limits=XmlLimits(max_bytes=len(main_bytes)),
            )

    def test_composed_schema_protected_policy_rejects_dependency_entities(self, tmp_path):
        part = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE xs:schema [<!ENTITY tns "urn:part">]>'
            f'<xs:schema {XS} targetNamespace="&tns;">'
            '<xs:element name="part" type="xs:string"/></xs:schema>'
        )
        (tmp_path / "main.xsd").write_text(
            f'<xs:schema {XS}><xs:include schemaLocation="part.xsd"/></xs:schema>',
            encoding="utf-8",
        )
        (tmp_path / "part.xsd").write_text(part, encoding="utf-8")
        with pytest.raises(ResourcePolicyError):
            Schema.compile(
                str(tmp_path / "main.xsd"),
                xml_policy=XmlParserPolicy.protected(),
            )

    def test_composed_schema_default_policy_still_expands_dependency_entities(self, tmp_path):
        part = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE xs:schema [<!ENTITY base "xs:int">]>'
            f'<xs:schema {XS}><xs:simpleType name="Number">'
            '<xs:restriction base="&base;"/></xs:simpleType></xs:schema>'
        )
        result = _compile(
            tmp_path,
            f'<xs:schema {XS}><xs:include schemaLocation="part.xsd"/>'
            '<xs:element name="root" type="Number"/></xs:schema>',
            {"part.xsd": part},
        )
        assert not result.report.has_errors
