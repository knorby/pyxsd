"""Attribute wildcards inherited through ``xs:extension`` (schema phase).

``report_attribute_wildcard_restriction`` used to read the base type's
*direct* ``wildcardAttributeSpecs``.  A base that inherited its wildcard
through ``xs:extension`` therefore looked wildcard-less, so a restriction
of it was reported ``wildcard-invalid`` ("declares an attribute wildcard
but its base type has none") even when it merely declared the very
wildcard it inherited.  The packaged XSLT 2.0/3.0 schemas have that shape
(``generic-element-type`` -> ``versioned-element-type`` -> restriction),
and the false reports became ``XTSE0220`` for ``xsl:import-schema``.

The base side is now the type's *effective* wildcard: an extension unions
the base's effective wildcard with its own (XSD 1.1 §3.4.2.4,
``union_wildcard_specs``, stronger ``processContents``), while a
restriction keeps only its own declared wildcard and never inherits
(§3.4.6.3).  The derived side stays *declared* (own ``anyAttribute`` plus
its attribute groups, intersected per errata E1-10): using the derived
effective wildcard would intersect it with the base's and make every
restriction trivially valid.

The sibling ``attribute-restriction`` ("not admitted by the attribute
wildcard") defect on the same packaged schemas was a different cause —
attribute *uses* the base inherited through its restriction/extension
chain — and stays out.  The *group* half of that sibling is fixed here:
``effective_attribute_uses`` read only ``er.attributes``, so a base whose
use arrives through an ``xs:attributeGroup`` looked use-less and a
restriction that redeclared the very attribute the group contributes was
reported.  It now folds in the transitive attributeGroup contributions,
the same read path ``attribute_group_wildcard_specs`` uses for wildcards.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyxsd import schema_checks
from pyxsd.binding import ParseModes
from pyxsd.schema import Schema
from pyxsd.schema_checks import (
    _compute_effective_type_attribute_wildcard,
    _wildcard_cache,
    attribute_group_attribute_uses,
    attribute_group_wildcard_specs,
    effective_attribute_uses,
    effective_type_attribute_wildcard,
)
from pyxsd.validation import IssueSeverity
from pyxsd.wildcards import WildcardSpec

XS = "xmlns:xs='http://www.w3.org/2001/XMLSchema'"
XSD_HEAD = "<xs:schema " + XS + ">"
XSD_TAIL = "</xs:schema>"
NS_HEAD = (
    "<xs:schema " + XS + " targetNamespace='urn:t' xmlns:t='urn:t' elementFormDefault='qualified'>"
)


def wildcard_errors(report) -> list[tuple[str, str]]:
    return [
        (issue.code, issue.message)
        for issue in report.for_phase("schema")
        if issue.code == "wildcard-invalid" and issue.severity is IssueSeverity.ERROR
    ]


def particle_restriction_errors(report) -> list:
    return [
        issue
        for issue in report.for_phase("schema")
        if issue.code == "particle-restriction" and issue.severity is IssueSeverity.ERROR
    ]


def attribute_restriction_errors(report) -> list[tuple[str, str]]:
    return [
        (issue.code, issue.message)
        for issue in report.for_phase("schema")
        if issue.code == "attribute-restriction" and issue.severity is IssueSeverity.ERROR
    ]


@pytest.fixture
def parse(tmp_path):
    """Compile a schema fragment and return its schema-phase report."""
    schema_path = tmp_path / "schema.xsd"

    def _parse(schema_string: str, head: str = XSD_HEAD, xsd_version: str = "1.1"):
        schema_path.write_text(head + schema_string + XSD_TAIL, encoding="utf-8")
        return Schema.compile(
            str(schema_path), mode=ParseModes.NAMESPACED, xsd_version=xsd_version
        ).report

    return _parse


class TestAttributeWildcardInheritance:
    """The base side of the restriction check is the effective wildcard."""

    def test_restriction_of_an_extension_inherits_the_base_wildcard(self, parse):
        """A restriction may restate the wildcard its base inherited."""
        report = parse(
            """
<xs:complexType name="base">
  <xs:attribute name="a" type="xs:string"/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:complexType>
<xs:complexType name="mid">
  <xs:complexContent>
    <xs:extension base="t:base">
      <xs:attribute name="b" type="xs:string"/>
    </xs:extension>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:mid">
      <xs:attribute name="a" type="xs:string"/>
      <xs:attribute name="b" type="xs:string" use="required"/>
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        assert wildcard_errors(report) == []

    def test_extension_union_is_the_base_for_a_later_restriction(self, parse):
        """The extension's wildcard unions in and takes the stronger processContents."""
        report = parse(
            """
<xs:complexType name="base">
  <xs:attribute name="a" type="xs:string"/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:complexType>
<xs:complexType name="mid">
  <xs:complexContent>
    <xs:extension base="t:base">
      <xs:anyAttribute namespace="urn:extra" processContents="strict"/>
    </xs:extension>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:mid">
      <xs:attribute name="a" type="xs:string"/>
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        errors = wildcard_errors(report)
        assert len(errors) == 1
        # The union's namespace is the same set of admissible namespaces (both
        # sides are "everything but the target namespace"), but its
        # processContents is the stronger "strict" the extension contributed.
        assert "weakens the base attribute wildcard processContents" in errors[0][1]
        assert "'lax' is weaker than 'strict'" in errors[0][1]

    def test_restriction_of_a_widened_extension_reports_a_widening(self, parse):
        report = parse(
            """
<xs:complexType name="base">
  <xs:attribute name="a" type="xs:string"/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:complexType>
<xs:complexType name="mid">
  <xs:complexContent>
    <xs:extension base="t:base">
      <xs:attribute name="b" type="xs:string"/>
    </xs:extension>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:mid">
      <xs:attribute name="a" type="xs:string"/>
      <xs:anyAttribute namespace="##any" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        errors = wildcard_errors(report)
        assert len(errors) == 1
        assert "widens the base attribute wildcard namespace constraint" in errors[0][1]

    def test_a_restriction_that_drops_the_wildcard_breaks_the_chain(self, parse):
        """A restriction never passes a wildcard on (ctO005 shape, unchanged)."""
        report = parse(
            """
<xs:complexType name="base">
  <xs:sequence/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:complexType>
<xs:complexType name="restricted">
  <xs:complexContent>
    <xs:restriction base="t:base"><xs:sequence/></xs:restriction>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="ext">
  <xs:complexContent>
    <xs:extension base="t:restricted"><xs:sequence/></xs:extension>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="final">
  <xs:complexContent>
    <xs:restriction base="t:ext">
      <xs:sequence/>
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        errors = wildcard_errors(report)
        assert len(errors) == 1
        assert "declares an attribute wildcard but its base type has none" in errors[0][1]
        assert "'final'" in errors[0][1]

    def test_attribute_group_wildcard_is_the_derived_side(self, parse):
        """A group-contributed widening is reported (the old false negative)."""
        report = parse(
            """
<xs:attributeGroup name="widen">
  <xs:anyAttribute namespace="##any" processContents="lax"/>
</xs:attributeGroup>
<xs:complexType name="base">
  <xs:attribute name="a" type="xs:string"/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:base">
      <xs:attribute name="a" type="xs:string"/>
      <xs:attributeGroup ref="t:widen"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        errors = wildcard_errors(report)
        assert len(errors) == 1
        assert "widens the base attribute wildcard namespace constraint" in errors[0][1]

    def test_a_valid_attribute_group_wildcard_restriction_stays_silent(self, parse):
        """The negative control for the group-contributed derived side."""
        report = parse(
            """
<xs:attributeGroup name="keep">
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:attributeGroup>
<xs:complexType name="base">
  <xs:attribute name="a" type="xs:string"/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:base">
      <xs:attribute name="a" type="xs:string"/>
      <xs:attributeGroup ref="t:keep"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        assert wildcard_errors(report) == []

    def test_attribute_group_contribution_is_transitive(self, parse):
        """A group reached through another group contributes its wildcard."""
        report = parse(
            """
<xs:attributeGroup name="outer">
  <xs:attributeGroup ref="t:inner"/>
</xs:attributeGroup>
<xs:attributeGroup name="inner">
  <xs:anyAttribute namespace="##any" processContents="lax"/>
</xs:attributeGroup>
<xs:complexType name="base">
  <xs:attribute name="a" type="xs:string"/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:base">
      <xs:attribute name="a" type="xs:string"/>
      <xs:attributeGroup ref="t:outer"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        errors = wildcard_errors(report)
        assert len(errors) == 1
        assert "widens the base attribute wildcard namespace constraint" in errors[0][1]

    def test_a_wildcard_after_a_dropped_chain_stands_alone(self, parse):
        """Extension and restriction each stand alone over a wildcard-less base."""
        report = parse(
            """
<xs:complexType name="base">
  <xs:sequence/>
</xs:complexType>
<xs:complexType name="dropped">
  <xs:complexContent>
    <xs:restriction base="t:base"><xs:sequence/></xs:restriction>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="ext">
  <xs:complexContent>
    <xs:extension base="t:dropped">
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:extension>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="kept">
  <xs:complexContent>
    <xs:restriction base="t:dropped">
      <xs:sequence/>
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="final">
  <xs:complexContent>
    <xs:restriction base="t:ext">
      <xs:sequence/>
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="last">
  <xs:complexContent>
    <xs:restriction base="t:kept">
      <xs:sequence/>
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        # `dropped` inherits nothing and declares nothing, so `ext`'s wildcard
        # is its own (an extension may add one) while `kept`'s is its own too
        # but invalid, because a restriction may not introduce a wildcard over
        # a base that has none (ctO005). `final`/`last` narrow the wildcard
        # each chain carries.
        errors = wildcard_errors(report)
        assert len(errors) == 1
        assert "'kept'" in errors[0][1]
        assert "base type has none" in errors[0][1]

    def test_a_restriction_chain_intersects_its_steps(self, parse):
        """A restriction whose base is a restriction walks the chain step by step."""
        shape = """
<xs:complexType name="base">
  <xs:sequence/>
  <xs:anyAttribute namespace="##any" processContents="lax"/>
</xs:complexType>
<xs:complexType name="mid">
  <xs:complexContent>
    <xs:restriction base="t:base">
      <xs:sequence/>
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="last">
  <xs:complexContent>
    <xs:restriction base="t:mid">
      <xs:sequence/>
      <xs:anyAttribute namespace="{ns}" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
"""
        narrowed = parse(shape.format(ns="##other"), head=NS_HEAD)
        assert wildcard_errors(narrowed) == []
        # The effective base is the intersection of the whole chain ("##other"),
        # not the first step's "##any".
        widened = parse(shape.format(ns="##any"), head=NS_HEAD)
        errors = wildcard_errors(widened)
        assert len(errors) == 1
        assert "widens the base attribute wildcard namespace constraint" in errors[0][1]

    def test_element_wildcards_are_already_effective_through_extension(self, parse):
        """The element-wildcard check has no such blind spot (blast radius)."""
        shape = """
<xs:complexType name="computer">
  <xs:all>
    <xs:element name="name" type="xs:string"/>
    <xs:any namespace="##other" minOccurs="0" maxOccurs="2"
            processContents="lax"/>
  </xs:all>
</xs:complexType>
<xs:complexType name="computer-mid">
  <xs:complexContent><xs:extension base="t:computer"/></xs:complexContent>
</xs:complexType>
<xs:complexType name="{name}">
  <xs:complexContent>
    <xs:restriction base="t:computer-mid">
      <xs:sequence>
        <xs:element name="name" type="xs:string"/>
        <xs:any namespace="{ns}" minOccurs="1" maxOccurs="1"
                processContents="lax"/>
      </xs:sequence>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
"""
        narrowed = parse(shape.format(name="narrowed", ns="urn:x"), head=NS_HEAD)
        assert particle_restriction_errors(narrowed) == []
        widened = parse(shape.format(name="widened", ns="##any"), head=NS_HEAD)
        assert particle_restriction_errors(widened)

    def test_simple_content_extension_chain_is_walked(self, parse):
        """The simpleContent wrapper's derivation is part of the chain."""
        shape = """
<xs:complexType name="sbase">
  <xs:simpleContent>
    <xs:extension base="xs:string">
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:extension>
  </xs:simpleContent>
</xs:complexType>
<xs:complexType name="{name}">
  <xs:simpleContent>
    <xs:restriction base="t:sbase">
      <xs:simpleType><xs:restriction base="xs:string"/></xs:simpleType>
      <xs:anyAttribute namespace="{ns}" processContents="lax"/>
    </xs:restriction>
  </xs:simpleContent>
</xs:complexType>
"""
        narrowed = parse(shape.format(name="snarrow", ns="##other"), head=NS_HEAD)
        assert wildcard_errors(narrowed) == []
        widened = parse(shape.format(name="swiden", ns="##any"), head=NS_HEAD)
        errors = wildcard_errors(widened)
        assert len(errors) == 1
        assert "widens the base attribute wildcard namespace constraint" in errors[0][1]

    def test_unresolvable_base_chain_contributes_nothing(self, parse):
        """An unresolved base contributes no wildcard; the derived type keeps its own."""
        report = parse(
            """
<xs:complexType name="mid">
  <xs:complexContent>
    <xs:restriction base="t:missing">
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:mid">
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        assert wildcard_errors(report) == []
        assert "unknown-type" in {issue.code for issue in report.for_phase("schema")}

    def test_unresolvable_attribute_group_reference_is_skipped(self, parse):
        report = parse(
            """
<xs:complexType name="base">
  <xs:attributeGroup ref="t:missing"/>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:base">
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        errors = wildcard_errors(report)
        assert len(errors) == 1
        assert "base type has none" in errors[0][1]
        assert "unknown-attributeGroup" in {issue.code for issue in report.for_phase("schema")}

    def test_circular_attribute_group_terminates(self, parse):
        """XSD 1.1 permits circular attributeGroup references (bug 15795)."""
        report = parse(
            """
<xs:attributeGroup name="a">
  <xs:attributeGroup ref="t:b"/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:attributeGroup>
<xs:attributeGroup name="b">
  <xs:attributeGroup ref="t:a"/>
</xs:attributeGroup>
<xs:complexType name="base">
  <xs:attributeGroup ref="t:a"/>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:base">
      <xs:attributeGroup ref="t:a"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        assert wildcard_errors(report) == []


class TestAttributeGroupAttributeUses:
    """The restriction-use check reads group-contributed uses on the base."""

    GROUP_BASE = """
<xs:attributeGroup name="std">
  <xs:attribute name="a" type="xs:string"/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:attributeGroup>
<xs:complexType name="base">
  <xs:attributeGroup ref="t:std"/>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:base">
      %s
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
"""

    @pytest.mark.parametrize("xsd_version", ("1.0", "1.1"))
    def test_a_group_contributed_base_use_admits_a_redeclaration(self, parse, xsd_version):
        """A restriction may redeclare the use its base gets from a group.

        Regression: the base's wildcard is now effective, which runs the
        attribute-use check that ``effective_attribute_uses`` used to starve
        of the group's uses.  Read the two together and the redeclared use is
        a base use, not a use the wildcard must admit.
        """
        report = parse(
            self.GROUP_BASE % '<xs:attribute name="a" type="xs:string"/>',
            head=NS_HEAD,
            xsd_version=xsd_version,
        )
        assert attribute_restriction_errors(report) == []

    @pytest.mark.parametrize("xsd_version", ("1.0", "1.1"))
    def test_a_genuinely_new_attribute_is_still_not_admitted(self, parse, xsd_version):
        """The group admits only its own use; a new use is still reported."""
        report = parse(
            self.GROUP_BASE
            % (
                '<xs:attribute name="a" type="xs:string"/>\n'
                '      <xs:attribute name="b" type="xs:string"/>'
            ),
            head=NS_HEAD,
            xsd_version=xsd_version,
        )
        errors = attribute_restriction_errors(report)
        assert len(errors) == 1
        assert "'b'" in errors[0][1]
        assert "is not admitted by the attribute wildcard" in errors[0][1]

    @pytest.mark.parametrize("xsd_version", ("1.0", "1.1"))
    def test_an_extension_union_admits_a_restriction_to_the_added_namespace(
        self, parse, xsd_version
    ):
        """``##local`` base plus an ``urn:extra`` extension admits ``urn:extra``."""
        report = parse(
            """
<xs:complexType name="base">
  <xs:anyAttribute namespace="##local" processContents="lax"/>
</xs:complexType>
<xs:complexType name="mid">
  <xs:complexContent>
    <xs:extension base="t:base">
      <xs:anyAttribute namespace="urn:extra" processContents="lax"/>
    </xs:extension>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:mid">
      <xs:anyAttribute namespace="urn:extra" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
            xsd_version=xsd_version,
        )
        assert wildcard_errors(report) == []
        assert attribute_restriction_errors(report) == []

    @pytest.mark.parametrize("xsd_version", ("1.0", "1.1"))
    def test_a_nested_group_contributed_use_admits_a_redeclaration(self, parse, xsd_version):
        """The base's uses include the one a nested group contributes.

        The base reaches ``a`` directly through ``outer`` and ``b`` through
        ``outer``'s reference to ``inner``; the restriction redeclares both.
        Without the nested-group merge ``b`` looks like a new use the base's
        wildcard has to admit.
        """
        report = parse(
            """
<xs:attributeGroup name="outer">
  <xs:attribute name="a" type="xs:string"/>
  <xs:attributeGroup ref="t:inner"/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:attributeGroup>
<xs:attributeGroup name="inner">
  <xs:attribute name="b" type="xs:string"/>
</xs:attributeGroup>
<xs:complexType name="base">
  <xs:attributeGroup ref="t:outer"/>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:base">
      <xs:attribute name="a" type="xs:string"/>
      <xs:attribute name="b" type="xs:string"/>
      <xs:anyAttribute namespace="##other" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
            xsd_version=xsd_version,
        )
        assert attribute_restriction_errors(report) == []
        assert wildcard_errors(report) == []

    @pytest.mark.parametrize("xsd_version", ("1.0", "1.1"))
    def test_an_extension_union_admits_a_restriction_to_the_inherited_namespace(
        self, parse, xsd_version
    ):
        """``urn:extra`` from the base survives the ``urn:other`` extension.

        The mirror of the test above: here the extension contributes the
        *other* namespace and the restriction keeps the base's, so dropping
        the base arm of the union makes ``urn:extra`` look uninherited.
        """
        report = parse(
            """
<xs:complexType name="base">
  <xs:anyAttribute namespace="urn:extra" processContents="lax"/>
</xs:complexType>
<xs:complexType name="mid">
  <xs:complexContent>
    <xs:extension base="t:base">
      <xs:anyAttribute namespace="urn:other" processContents="lax"/>
    </xs:extension>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="derived">
  <xs:complexContent>
    <xs:restriction base="t:mid">
      <xs:anyAttribute namespace="urn:extra" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
            xsd_version=xsd_version,
        )
        assert wildcard_errors(report) == []
        assert attribute_restriction_errors(report) == []


class TestAttributeWildcardInheritanceAcrossImports:
    """Each spec keeps the target namespace of the document declaring it."""

    BASE = (
        "<xs:schema " + XS + " targetNamespace='urn:b' xmlns:b='urn:b' "
        "elementFormDefault='qualified'>"
        "<xs:complexType name='base'>"
        "<xs:attribute name='a' type='xs:string'/>"
        "<xs:anyAttribute namespace='{ns}' processContents='lax'/>"
        "</xs:complexType></xs:schema>"
    )
    MAIN = (
        "<xs:schema " + XS + " targetNamespace='urn:a' xmlns:a='urn:a' "
        "xmlns:b='urn:b' elementFormDefault='qualified'>"
        "<xs:import namespace='urn:b' schemaLocation='base.xsd'/>"
        "<xs:complexType name='derived'>"
        "<xs:complexContent><xs:restriction base='b:base'>"
        "<xs:attribute name='a' type='xs:string'/>"
        "<xs:anyAttribute namespace='##other' processContents='lax'/>"
        "</xs:restriction></xs:complexContent>"
        "</xs:complexType>"
        "<xs:element name='doc' type='a:derived'/>"
        "</xs:schema>"
    )

    def _compile(self, tmp_path, base_wildcard: str):
        (tmp_path / "base.xsd").write_text(self.BASE.format(ns=base_wildcard), encoding="utf-8")
        (tmp_path / "main.xsd").write_text(self.MAIN, encoding="utf-8")
        return Schema.compile(str(tmp_path / "main.xsd"), mode=ParseModes.NAMESPACED).report

    def test_imported_any_base_admits_the_derived_target_namespace(self, tmp_path):
        report = self._compile(tmp_path, "##any")
        assert wildcard_errors(report) == []

    def test_cross_namespace_other_is_not_a_subset_of_other(self, tmp_path):
        # urn:b's ##other admits urn:a; urn:a's ##other does not.
        report = self._compile(tmp_path, "##other")
        errors = wildcard_errors(report)
        assert len(errors) == 1
        assert "widens the base attribute wildcard namespace constraint" in errors[0][1]


class _StubType:
    """Minimal element-representative stand-in for the chain walk."""

    def __init__(
        self,
        name: str,
        derivation: str | None,
        base: _StubType | None = None,
        specs: tuple[WildcardSpec, ...] = (),
        schema: object | None = None,
    ) -> None:
        self.name = name
        self.derivation = derivation
        self.base = base
        self.wildcardAttributeSpecs = list(specs)
        self.schema = schema

    def getDerivation(self) -> str | None:
        return self.derivation

    def _firstProcessedChild(self, parent: object, name: str) -> None:
        return None

    def getNamespace(self) -> None:
        return None

    def getSchema(self):
        return self.schema


class _StubUse:
    """Minimal attribute-use stand-in for the attributeGroup walk."""

    def __init__(self, name: str, use: str = "optional", inheritable: bool = False) -> None:
        self.name = name
        self.use = use
        self.tagAttributes = {"inheritable": "true"} if inheritable else {}

    def getUse(self) -> str:
        return self.use


def _group_stub(name: str, uses=(), refs=()) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        attributes={use.name: use for use in uses},
        attributeGroupRefs=list(refs),
    )


def _ref_stub(group: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(_resolve_group=lambda **kwargs: group)


class TestEffectiveWildcardChain:
    """Unit-level coverage of the ER walk's memo and guards."""

    def test_derivation_cycle_does_not_recurse_without_bound(self, monkeypatch):
        """The ``seen`` guard cuts a chain whose base resolves to itself.

        No real schema reaches it: pyxsd reports the derivation cycle and the
        offending base stops resolving, so ``base_type_er`` returns ``None``
        first. The guard is still load-bearing for isolated ERs and for a
        monkeypatched/buggy resolver.
        """
        stub = _StubType("self-restricting", "restriction", schema=None)
        stub.base = stub
        monkeypatch.setattr(schema_checks, "base_type_er", lambda ctx, er, py_xsd: er.base)
        cache = _wildcard_cache(stub)
        result = _compute_effective_type_attribute_wildcard(None, stub, None, set(), cache)
        assert result is None
        # "No wildcard" is a cacheable answer (_CACHE_MISS is distinct from None).
        assert cache["effective"][id(stub)] is None

    def test_chain_is_memoized_per_step(self, monkeypatch):
        """Every step is cached, the entry point hits, and inner steps hit too."""
        schema = SimpleNamespace()
        spec = WildcardSpec(namespace="##other", process_contents="lax", is_attribute=True)
        base = _StubType("base", None, specs=(spec,), schema=schema)
        mid = _StubType("mid", "extension", base=base, schema=schema)
        other = _StubType("other", "extension", base=mid, schema=schema)
        monkeypatch.setattr(schema_checks, "base_type_er", lambda ctx, er, py_xsd: er.base)

        first = effective_type_attribute_wildcard(None, other, None)
        assert first is not None
        cache = schema._attribute_wildcard_cache
        assert set(cache["effective"]) == {id(base), id(mid), id(other)}
        # Entry-point hit: the memoized object is returned unchanged.
        assert effective_type_attribute_wildcard(None, other, None) is first
        # Inner-step hit: `sibling` reads `mid`'s cached step while walking.
        sibling = _StubType("sibling", "extension", base=mid, schema=schema)
        assert effective_type_attribute_wildcard(None, sibling, None) == first
        assert id(sibling) in cache["effective"]

    def test_wildcard_cache_degrades_without_a_schema(self):
        """No schema, no ``getSchema`` and a shared schema all behave."""
        shared = SimpleNamespace()
        first = _wildcard_cache(_StubType("a", None, schema=shared))
        second = _wildcard_cache(_StubType("b", None, schema=shared))
        assert first is second
        assert shared._attribute_wildcard_cache is first
        assert _wildcard_cache(_StubType("c", None, schema=None)) == {
            "declared": {},
            "effective": {},
        }
        # An ER without ``getSchema`` at all (an isolated declaration).
        assert _wildcard_cache(SimpleNamespace()) == {"declared": {}, "effective": {}}
        # A ``getSchema`` that exists but is not callable (annotation-only
        # attributes on stubs) degrades the same way.
        assert _wildcard_cache(SimpleNamespace(getSchema=None)) == {
            "declared": {},
            "effective": {},
        }

    def test_attribute_group_specs_drop_a_spec_already_copied_onto_the_type(self):
        """Class building copies group specs onto the type; the walk dedups them."""
        spec = WildcardSpec(namespace="##other", process_contents="lax", is_attribute=True)
        group = SimpleNamespace(wildcardAttributeSpecs=[spec], attributeGroupRefs=[])
        ref = SimpleNamespace(_resolve_group=lambda **kwargs: group)
        er = SimpleNamespace(wildcardAttributeSpecs=[spec], attributeGroupRefs=[ref])
        assert attribute_group_wildcard_specs(er, None) == [spec]


class TestAttributeGroupUseWalk:
    """Unit-level coverage of the attributeGroup use walk."""

    def test_a_groups_own_declaration_wins_over_a_nested_group(self):
        inner_use = _StubUse("a")
        outer_use = _StubUse("a", "required")
        inner = _group_stub("inner", uses=[inner_use])
        outer = _group_stub("outer", uses=[outer_use], refs=[_ref_stub(inner)])
        er = SimpleNamespace(attributeGroupRefs=[_ref_stub(outer)])
        assert attribute_group_attribute_uses(er, None) == {"a": outer_use}

    def test_a_nested_groups_distinct_use_reaches_the_caller(self):
        """A nested group's own attribute is a use of the referring type.

        The shadowing test above cannot see this: both levels declare ``a``,
        so a walk that never merged the nested group's declarations would
        still answer ``{"a"}``.
        """
        outer_use = _StubUse("a")
        inner_use = _StubUse("b")
        inner = _group_stub("inner", uses=[inner_use])
        outer = _group_stub("outer", uses=[outer_use], refs=[_ref_stub(inner)])
        er = SimpleNamespace(attributeGroupRefs=[_ref_stub(outer)])
        assert attribute_group_attribute_uses(er, None) == {"a": outer_use, "b": inner_use}

    def test_a_prohibited_group_contribution_is_not_a_use(self):
        group = _group_stub("g", uses=[_StubUse("a"), _StubUse("b", "prohibited")])
        er = SimpleNamespace(attributeGroupRefs=[_ref_stub(group)])
        assert list(attribute_group_attribute_uses(er, None)) == ["a"]

    def test_a_circular_group_terminates(self):
        use = _StubUse("a")
        first = _group_stub("a", uses=[use])
        second = _group_stub("b", refs=[_ref_stub(first)])
        first.attributeGroupRefs.append(_ref_stub(second))
        er = SimpleNamespace(attributeGroupRefs=[_ref_stub(first)])
        assert attribute_group_attribute_uses(er, None) == {"a": use}

    def test_a_shared_group_is_walked_once(self):
        use = _StubUse("a")
        group = _group_stub("g", uses=[use])
        er = SimpleNamespace(attributeGroupRefs=[_ref_stub(group), _ref_stub(group)])
        assert attribute_group_attribute_uses(er, None) == {"a": use}

    def test_a_direct_use_wins_over_a_group_contribution(self):
        direct = _StubUse("a", "required")
        group = _group_stub("g", uses=[_StubUse("a")])
        er = SimpleNamespace(
            attributes={"a": direct},
            attributeGroupRefs=[_ref_stub(group)],
            getDerivation=lambda: None,
            _firstProcessedChild=lambda parent, name: None,
        )
        uses, inheritable = effective_attribute_uses(None, er, None)
        assert uses == {"a": direct}
        assert inheritable is False

    def test_an_inheritable_group_use_reaches_the_caller(self):
        group = _group_stub("g", uses=[_StubUse("a", inheritable=True)])
        er = SimpleNamespace(
            attributes={},
            attributeGroupRefs=[_ref_stub(group)],
            getDerivation=lambda: None,
            _firstProcessedChild=lambda parent, name: None,
        )
        uses, inheritable = effective_attribute_uses(None, er, None)
        assert set(uses) == {"a"}
        assert inheritable is True


class TestSharedBaseDiagnostics:
    """A shared base produces the same diagnostic for each restriction."""

    def test_two_restrictions_of_one_base_report_the_same_widening(self, parse):
        report = parse(
            """
<xs:complexType name="base">
  <xs:attribute name="a" type="xs:string"/>
  <xs:anyAttribute namespace="##other" processContents="lax"/>
</xs:complexType>
<xs:complexType name="mid">
  <xs:complexContent>
    <xs:extension base="t:base">
      <xs:attribute name="b" type="xs:string"/>
    </xs:extension>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="first">
  <xs:complexContent>
    <xs:restriction base="t:mid">
      <xs:attribute name="a" type="xs:string"/>
      <xs:anyAttribute namespace="##any" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
<xs:complexType name="second">
  <xs:complexContent>
    <xs:restriction base="t:mid">
      <xs:attribute name="a" type="xs:string"/>
      <xs:anyAttribute namespace="##any" processContents="lax"/>
    </xs:restriction>
  </xs:complexContent>
</xs:complexType>
""",
            head=NS_HEAD,
        )
        errors = wildcard_errors(report)
        assert len(errors) == 2
        assert all("widens the base attribute wildcard" in message for _, message in errors)
        assert {message.split("'")[1] for _, message in errors} == {"first", "second"}
