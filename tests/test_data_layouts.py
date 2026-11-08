"""Layout-planner tests for the schema-driven ``pyxsd-json`` profile.

Every test compiles the schema shape under test and asks the planner for the
effective type's layout. The expected child names, minima and maxima are
hand-derived for each fixture; no assertion counts children observed in a
document.
"""

from __future__ import annotations

from io import StringIO

import pytest

import pyxsd
from pyxsd.data_conversion.layouts import (
    ChildPlan,
    Layout,
    LayoutPlan,
    plan_layout,
)
from pyxsd.element_representatives.element_representative import componentKind
from pyxsd.exceptions import PyXSDError
from pyxsd.schema_base import SchemaBase
from pyxsd.xsd_data_types import AnyType, Integer


def _root_declaration(schema: pyxsd.Schema, name: str):
    for entries in schema.components.values():
        for entry in entries:
            if (
                componentKind(entry) == "element"
                and entry.isGlobalDeclaration()
                and entry.name == name
            ):
                return entry
    raise AssertionError(f"no global element declaration named {name!r}")


def _compiled_type(source: str, *, root: str = "root", allow_errors: bool = False):
    schema = pyxsd.Schema.compile(StringIO(source))
    if not allow_errors:
        assert not schema.report.errors, [str(error) for error in schema.report.errors]
    return _root_declaration(schema, root).getType()


def _children(plan: LayoutPlan) -> list[tuple[str, int, int | None]]:
    return [(child.name, child.min_occurs, child.max_occurs) for child in plan.children]


def _model_elements(particle) -> list[tuple[str, int, int | None]]:
    """Flatten one compiled particle tree into its element occurrence list."""
    elements: list[tuple[str, int, int | None]] = []
    pending = [particle]
    while pending:
        current = pending.pop()
        if current.kind == "element":
            elements.append((current.name, current.min_occurs, current.max_occurs))
        else:
            pending.extend(reversed(current.children))
    return elements


EMPTY_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType/>
  </xs:element>
</xs:schema>"""

EMPTY_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:attribute name="id" type="xs:string"/>
      <xs:attribute name="gone" type="xs:string" use="prohibited"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

EMPTY_PROHIBITED_ONLY_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:attribute name="gone" type="xs:string" use="prohibited"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

EMPTY_ATTRIBUTE_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:anyAttribute processContents="lax"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SIMPLE_CONTENT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:attribute name="id" type="xs:string"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

PLAIN_SIMPLE_CONTENT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string"/>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SIMPLE_ATTRIBUTE_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:anyAttribute processContents="lax"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

DECLARED_SIMPLE_TYPE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="Code">
    <xs:restriction base="xs:string">
      <xs:maxLength value="4"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:element name="root" type="Code"/>
</xs:schema>"""

COMPLEX_DERIVATION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="BaseType">
    <xs:sequence>
      <xs:element name="base" type="xs:string" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="ExtensionType">
    <xs:complexContent>
      <xs:extension base="BaseType">
        <xs:sequence>
          <xs:element name="own" type="xs:string" minOccurs="0" maxOccurs="2"/>
        </xs:sequence>
      </xs:extension>
    </xs:complexContent>
  </xs:complexType>
  <xs:complexType name="RestrictionType">
    <xs:complexContent>
      <xs:restriction base="BaseType">
        <xs:sequence>
          <xs:element name="base" type="xs:string" maxOccurs="2"/>
        </xs:sequence>
      </xs:restriction>
    </xs:complexContent>
  </xs:complexType>
  <xs:element name="extensionRoot" type="ExtensionType"/>
  <xs:element name="restrictionRoot" type="RestrictionType"/>
</xs:schema>"""

ALL_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:all>
        <xs:element name="a" type="xs:string"/>
        <xs:element name="b" type="xs:string" minOccurs="0"/>
      </xs:all>
      <xs:attribute name="id" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ALL_ATTRIBUTE_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:all>
        <xs:element name="a" type="xs:string"/>
      </xs:all>
      <xs:anyAttribute processContents="lax"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ORDERED_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="head" type="xs:string"/>
        <xs:element name="p" type="xs:string" minOccurs="0" maxOccurs="2"/>
        <xs:element name="note" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

LIST_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

LIST_ATTRIBUTE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="source" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

LIST_ATTRIBUTE_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:anyAttribute processContents="lax"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

LIST_BOUNDED_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" minOccurs="0" maxOccurs="2"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

TWO_CHILD_LIST_CANDIDATE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
        <xs:element name="note" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

REPEATED_CHOICE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:choice maxOccurs="unbounded">
        <xs:element name="a" type="xs:string"/>
        <xs:element name="b" type="xs:string"/>
      </xs:choice>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ONE_SHOT_CHOICE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:choice>
        <xs:element name="a" type="xs:string"/>
        <xs:element name="b" type="xs:string"/>
      </xs:choice>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ORDERED_CHOICE_BRANCH_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:choice>
        <xs:sequence>
          <xs:element name="a" type="xs:string"/>
          <xs:element name="b" type="xs:string"/>
        </xs:sequence>
        <xs:element name="c" type="xs:string"/>
      </xs:choice>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

UNBOUNDED_CHOICE_BRANCH_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:choice>
        <xs:element name="a" type="xs:string" maxOccurs="unbounded"/>
        <xs:element name="c" type="xs:string"/>
      </xs:choice>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

REPEATED_SEQUENCE_CHOICE_BRANCH_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:choice>
        <xs:sequence maxOccurs="unbounded">
          <xs:element name="a" type="xs:string" maxOccurs="unbounded"/>
        </xs:sequence>
        <xs:element name="c" type="xs:string"/>
      </xs:choice>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ZERO_CHOICE_BRANCH_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:choice>
        <xs:element name="a" type="xs:string" minOccurs="0" maxOccurs="0"/>
        <xs:element name="b" type="xs:string"/>
      </xs:choice>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

GROUPED_ALL_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:group name="Members">
    <xs:all>
      <xs:element name="a" type="xs:string"/>
    </xs:all>
  </xs:group>
  <xs:element name="root">
    <xs:complexType>
      <xs:group ref="Members"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

DUPLICATE_NAME_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string"/>
        <xs:element name="a" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

UNBOUNDED_DUPLICATE_NAME_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="a" type="xs:string" maxOccurs="unbounded"/>
        <xs:element name="a" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

NESTED_OCCURRENCE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:sequence minOccurs="0" maxOccurs="2">
          <xs:element name="a" type="xs:string" maxOccurs="unbounded"/>
          <xs:element name="b" type="xs:string"/>
        </xs:sequence>
        <xs:element name="c" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

MIXED_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType mixed="true">
      <xs:sequence>
        <xs:element name="i" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="id" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

MIXED_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType mixed="true">
      <xs:sequence>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

OPEN_CONTENT_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:openContent mode="interleave">
        <xs:any processContents="lax"/>
      </xs:openContent>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

OPEN_CONTENT_EMPTY_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:openContent mode="suffix">
        <xs:any processContents="lax"/>
      </xs:openContent>
      <xs:sequence/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ELEMENT_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
        <xs:any minOccurs="0" maxOccurs="unbounded" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ZERO_WILDCARD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
        <xs:any minOccurs="0" maxOccurs="0" processContents="lax"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ZERO_OCCURRENCE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="gone" type="xs:string" minOccurs="0" maxOccurs="0"/>
        <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ZERO_GROUP_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence minOccurs="0" maxOccurs="0">
        <xs:element name="a" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

SUBSTITUTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="item" type="xs:string"/>
  <xs:element name="special" type="xs:string" substitutionGroup="item"/>
  <xs:element name="extra" type="xs:string" substitutionGroup="special"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="item" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ABSTRACT_SUBSTITUTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="item" abstract="true" type="xs:string"/>
  <xs:element name="special" type="xs:string" substitutionGroup="item"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="item" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

ABSTRACT_HEAD_ONLY_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="item" abstract="true" type="xs:string"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="item"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

BLOCKED_SUBSTITUTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="item" type="xs:string" block="substitution"/>
  <xs:element name="special" type="xs:string" substitutionGroup="item"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="item" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

INTERMEDIATE_BLOCKED_SUBSTITUTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="item" type="xs:string"/>
  <xs:element name="special" type="xs:string" substitutionGroup="item"
              block="substitution"/>
  <xs:element name="extra" type="xs:string" substitutionGroup="special"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="item" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

CYCLIC_SUBSTITUTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="a" type="xs:string" substitutionGroup="b"/>
  <xs:element name="b" type="xs:string" substitutionGroup="a"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="a" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

NAMESPACED_SUBSTITUTION_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example"
           targetNamespace="urn:example"
           elementFormDefault="qualified">
  <xs:element name="item" type="xs:string"/>
  <xs:element name="special" type="xs:string" substitutionGroup="t:item"/>
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="t:item" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

RECURSIVE_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="node">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="node" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:attribute name="name" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

RECURSIVE_RECORD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="node">
    <xs:complexType>
      <xs:all>
        <xs:element ref="node" minOccurs="0"/>
      </xs:all>
    </xs:complexType>
  </xs:element>
</xs:schema>"""

CONDITIONAL_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="ListType">
    <xs:sequence>
      <xs:element name="date" type="xs:date" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="RecordType">
    <xs:all>
      <xs:element name="a" type="xs:string"/>
      <xs:element name="b" type="xs:string" minOccurs="0"/>
    </xs:all>
  </xs:complexType>
  <xs:element name="root" type="xs:anyType">
    <xs:alternative test="@kind='list'" type="ListType"/>
    <xs:alternative test="@kind='record'" type="RecordType"/>
  </xs:element>
</xs:schema>"""


# ---------------------------------------------------------------------------
# Enum and record contract
# ---------------------------------------------------------------------------


def test_layout_enum_matches_the_profile_names():
    assert [(layout.name, layout.value) for layout in Layout] == [
        ("EMPTY", "empty"),
        ("EMPTY_PLUS", "empty-plus"),
        ("SIMPLE", "simple"),
        ("SIMPLE_PLUS", "simple-plus"),
        ("LIST", "list"),
        ("LIST_PLUS", "list-plus"),
        ("RECORD", "record"),
        ("SEQUENCE", "sequence"),
        ("MIXED", "mixed"),
    ]


def test_child_plans_keep_the_compiled_declaration():
    root_type = _compiled_type(LIST_SCHEMA)
    plan = plan_layout(root_type)

    assert isinstance(plan, LayoutPlan)
    assert isinstance(plan.children[0], ChildPlan)
    assert plan.children[0].declaration is root_type.__dict__["date"]


# ---------------------------------------------------------------------------
# Empty and simple content
# ---------------------------------------------------------------------------


def test_empty_content_without_attributes_is_empty():
    plan = plan_layout(_compiled_type(EMPTY_SCHEMA))

    assert plan.layout is Layout.EMPTY
    assert plan.children == ()
    assert plan.has_attributes is False
    assert plan.has_wildcards is False


def test_empty_content_with_an_explicit_type_uses_empty_plus():
    plan = plan_layout(_compiled_type(EMPTY_SCHEMA), explicit_type=True)

    assert plan.layout is Layout.EMPTY_PLUS
    assert plan.children == ()
    assert plan.has_attributes is False
    assert plan.has_wildcards is False


def test_empty_content_with_ordinary_attributes_uses_empty_plus():
    plan = plan_layout(_compiled_type(EMPTY_ATTRIBUTE_SCHEMA))

    assert plan.layout is Layout.EMPTY_PLUS
    assert plan.children == ()
    assert plan.has_attributes is True
    assert plan.has_wildcards is False


def test_empty_content_with_only_a_prohibited_attribute_is_empty():
    plan = plan_layout(_compiled_type(EMPTY_PROHIBITED_ONLY_SCHEMA))

    assert plan.layout is Layout.EMPTY
    assert plan.has_attributes is False
    assert plan.has_wildcards is False


def test_empty_content_with_an_attribute_wildcard_uses_empty_plus():
    plan = plan_layout(_compiled_type(EMPTY_ATTRIBUTE_WILDCARD_SCHEMA))

    assert plan.layout is Layout.EMPTY_PLUS
    assert plan.has_attributes is False
    assert plan.has_wildcards is True


def test_simple_values_use_simple_layouts():
    assert plan_layout(Integer).layout is Layout.SIMPLE
    assert plan_layout(Integer, explicit_type=True).layout is Layout.SIMPLE_PLUS

    plan = plan_layout(Integer)
    assert plan.children == ()
    assert plan.has_attributes is False
    assert plan.has_wildcards is False


def test_simple_content_without_attributes_is_simple():
    plan = plan_layout(_compiled_type(PLAIN_SIMPLE_CONTENT_SCHEMA))

    assert plan.layout is Layout.SIMPLE
    assert plan.children == ()
    assert plan.has_attributes is False
    assert plan.has_wildcards is False


def test_simple_content_with_attributes_or_wildcards_is_simple_plus():
    attributed = plan_layout(_compiled_type(SIMPLE_CONTENT_SCHEMA))
    assert attributed.layout is Layout.SIMPLE_PLUS
    assert attributed.has_attributes is True
    assert attributed.has_wildcards is False

    open_attributes = plan_layout(_compiled_type(SIMPLE_ATTRIBUTE_WILDCARD_SCHEMA))
    assert open_attributes.layout is Layout.SIMPLE_PLUS
    assert open_attributes.has_attributes is False
    assert open_attributes.has_wildcards is True


def test_simple_content_with_an_explicit_type_uses_simple_plus():
    plan = plan_layout(_compiled_type(PLAIN_SIMPLE_CONTENT_SCHEMA), explicit_type=True)

    assert plan.layout is Layout.SIMPLE_PLUS
    assert plan.has_attributes is False


def test_a_declared_simple_type_uses_simple_layouts():
    simple_type = _compiled_type(DECLARED_SIMPLE_TYPE_SCHEMA)

    plan = plan_layout(simple_type)
    assert plan.layout is Layout.SIMPLE
    assert plan.children == ()
    assert plan.has_attributes is False
    assert plan.has_wildcards is False

    assert plan_layout(simple_type, explicit_type=True).layout is Layout.SIMPLE_PLUS


def test_complex_content_extension_plans_its_flattened_effective_model():
    schema = pyxsd.Schema.compile(StringIO(COMPLEX_DERIVATION_SCHEMA))
    assert not schema.report.errors, [str(error) for error in schema.report.errors]
    base_type = schema.classes["BaseType"]
    extension_type = _root_declaration(schema, "extensionRoot").getType()

    # The planner reads only the class's own stamp, so a derived class must
    # carry its effective composed model rather than inherit the base's.
    assert "_contentModel_" in extension_type.__dict__
    assert extension_type.__dict__["_contentModel_"] is not base_type.__dict__["_contentModel_"]
    assert _model_elements(extension_type.__dict__["_contentModel_"]) == [
        ("base", 1, None),
        ("own", 0, 2),
    ]

    plan = plan_layout(extension_type)
    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("base", 1, None), ("own", 0, 2)]
    assert plan.has_attributes is False
    assert plan.has_wildcards is False


def test_complex_content_restriction_plans_its_restricted_model():
    schema = pyxsd.Schema.compile(StringIO(COMPLEX_DERIVATION_SCHEMA))
    assert not schema.report.errors, [str(error) for error in schema.report.errors]
    base_type = schema.classes["BaseType"]
    restriction_type = _root_declaration(schema, "restrictionRoot").getType()

    assert "_contentModel_" in restriction_type.__dict__
    assert restriction_type.__dict__["_contentModel_"] is not base_type.__dict__["_contentModel_"]
    assert _model_elements(base_type.__dict__["_contentModel_"]) == [("base", 1, None)]
    assert _model_elements(restriction_type.__dict__["_contentModel_"]) == [("base", 1, 2)]

    plan = plan_layout(restriction_type)
    assert plan.layout is Layout.LIST
    assert _children(plan) == [("base", 1, 2)]
    assert plan.has_attributes is False
    assert plan.has_wildcards is False


# ---------------------------------------------------------------------------
# Record and sequence selection
# ---------------------------------------------------------------------------


def test_all_content_is_a_record_with_occurrence_metadata():
    plan = plan_layout(_compiled_type(ALL_SCHEMA))

    assert plan.layout is Layout.RECORD
    assert _children(plan) == [("a", 1, 1), ("b", 0, 1)]
    assert plan.has_attributes is True
    assert plan.has_wildcards is False
    assert plan_layout(_compiled_type(ALL_SCHEMA), explicit_type=True).layout is Layout.RECORD


def test_attribute_wildcards_make_a_record_a_sequence():
    plan = plan_layout(_compiled_type(ALL_ATTRIBUTE_WILDCARD_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("a", 1, 1)]
    assert plan.has_attributes is False
    assert plan.has_wildcards is True


def test_ordered_unique_children_stay_a_sequence():
    plan = plan_layout(_compiled_type(ORDERED_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("head", 1, 1), ("p", 0, 2), ("note", 1, 1)]
    assert plan.has_attributes is False
    assert plan.has_wildcards is False


def test_a_one_shot_choice_is_a_record():
    plan = plan_layout(_compiled_type(ONE_SHOT_CHOICE_SCHEMA))

    assert plan.layout is Layout.RECORD
    assert _children(plan) == [("a", 0, 1), ("b", 0, 1)]
    assert plan.has_attributes is False
    assert plan.has_wildcards is False


def test_a_repeated_choice_is_a_sequence():
    plan = plan_layout(_compiled_type(REPEATED_CHOICE_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("a", 0, None), ("b", 0, None)]


def test_a_choice_branch_with_ordered_children_stays_a_sequence():
    plan = plan_layout(_compiled_type(ORDERED_CHOICE_BRANCH_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("a", 0, 1), ("b", 0, 1), ("c", 0, 1)]


def test_a_choice_branch_with_unbounded_content_stays_a_sequence():
    plan = plan_layout(_compiled_type(UNBOUNDED_CHOICE_BRANCH_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("a", 0, None), ("c", 0, 1)]


def test_a_repeated_sequence_branch_stays_a_sequence():
    plan = plan_layout(_compiled_type(REPEATED_SEQUENCE_CHOICE_BRANCH_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("a", 0, None), ("c", 0, 1)]


def test_a_zero_occurrence_choice_branch_is_absent():
    plan = plan_layout(_compiled_type(ZERO_CHOICE_BRANCH_SCHEMA))

    assert plan.layout is Layout.RECORD
    assert _children(plan) == [("b", 0, 1)]


def test_a_group_reference_to_an_all_is_still_a_record():
    plan = plan_layout(_compiled_type(GROUPED_ALL_SCHEMA))

    assert plan.layout is Layout.RECORD
    assert _children(plan) == [("a", 1, 1)]


def test_repeated_declarations_of_one_name_aggregate_their_occurrences():
    bounded = plan_layout(_compiled_type(DUPLICATE_NAME_SCHEMA, allow_errors=True))
    assert bounded.layout is Layout.LIST
    assert _children(bounded) == [("a", 2, 2)]

    unbounded = plan_layout(_compiled_type(UNBOUNDED_DUPLICATE_NAME_SCHEMA, allow_errors=True))
    assert unbounded.layout is Layout.LIST
    assert _children(unbounded) == [("a", 2, None)]


def test_nested_occurrences_multiply_child_minima_and_maxima():
    plan = plan_layout(_compiled_type(NESTED_OCCURRENCE_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("a", 0, None), ("b", 0, 2), ("c", 1, 1)]


# ---------------------------------------------------------------------------
# List selection
# ---------------------------------------------------------------------------


def test_one_unbounded_child_name_is_a_list():
    plan = plan_layout(_compiled_type(LIST_SCHEMA))

    assert plan.layout is Layout.LIST
    assert _children(plan) == [("date", 1, None)]
    assert plan.has_attributes is False
    assert plan.has_wildcards is False


def test_attributes_select_list_plus():
    for source in (LIST_ATTRIBUTE_SCHEMA, LIST_ATTRIBUTE_WILDCARD_SCHEMA):
        plan = plan_layout(_compiled_type(source))
        assert plan.layout is Layout.LIST_PLUS
        assert _children(plan) == [("date", 1, None)]


def test_list_plus_distinguishes_attributes_from_attribute_wildcards():
    attributed = plan_layout(_compiled_type(LIST_ATTRIBUTE_SCHEMA))
    assert attributed.has_attributes is True
    assert attributed.has_wildcards is False

    open_attributes = plan_layout(_compiled_type(LIST_ATTRIBUTE_WILDCARD_SCHEMA))
    assert open_attributes.has_attributes is False
    assert open_attributes.has_wildcards is True


def test_an_explicit_type_selects_list_plus():
    plan = plan_layout(_compiled_type(LIST_SCHEMA), explicit_type=True)

    assert plan.layout is Layout.LIST_PLUS
    assert _children(plan) == [("date", 1, None)]


def test_list_metadata_keeps_finite_occurrence_bounds():
    plan = plan_layout(_compiled_type(LIST_BOUNDED_SCHEMA))

    assert plan.layout is Layout.LIST
    assert _children(plan) == [("date", 0, 2)]


def test_two_child_names_prevent_list_layout():
    plan = plan_layout(_compiled_type(TWO_CHILD_LIST_CANDIDATE_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("date", 1, None), ("note", 1, 1)]


# ---------------------------------------------------------------------------
# Wildcards and open content
# ---------------------------------------------------------------------------


def test_element_wildcards_select_sequence():
    plan = plan_layout(_compiled_type(ELEMENT_WILDCARD_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("date", 1, None)]
    assert plan.has_wildcards is True


def test_zero_occurrence_wildcards_are_not_possible_content():
    plan = plan_layout(_compiled_type(ZERO_WILDCARD_SCHEMA))

    assert plan.layout is Layout.LIST
    assert _children(plan) == [("date", 1, None)]
    assert plan.has_wildcards is False


def test_zero_occurrence_particles_are_absent_from_the_plan():
    plan = plan_layout(_compiled_type(ZERO_OCCURRENCE_SCHEMA))

    assert plan.layout is Layout.LIST
    assert _children(plan) == [("date", 1, None)]


def test_a_zero_occurrence_model_is_empty():
    plan = plan_layout(_compiled_type(ZERO_GROUP_SCHEMA))

    assert plan.layout is Layout.EMPTY
    assert plan.children == ()
    assert plan.has_wildcards is False


def test_open_content_selects_sequence():
    plan = plan_layout(_compiled_type(OPEN_CONTENT_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("date", 1, None)]
    assert plan.has_attributes is False
    assert plan.has_wildcards is True


def test_open_content_without_declared_children_is_still_sequence():
    plan = plan_layout(_compiled_type(OPEN_CONTENT_EMPTY_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert plan.children == ()
    assert plan.has_wildcards is True


# ---------------------------------------------------------------------------
# Mixed content
# ---------------------------------------------------------------------------


def test_mixed_content_selects_mixed():
    plan = plan_layout(_compiled_type(MIXED_SCHEMA))

    assert plan.layout is Layout.MIXED
    assert _children(plan) == [("i", 0, None)]
    assert plan.has_attributes is True
    assert plan.has_wildcards is False
    assert plan_layout(_compiled_type(MIXED_SCHEMA), explicit_type=True).layout is Layout.MIXED


def test_mixed_content_with_a_wildcard_reports_wildcards():
    plan = plan_layout(_compiled_type(MIXED_WILDCARD_SCHEMA))

    assert plan.layout is Layout.MIXED
    assert plan.children == ()
    assert plan.has_wildcards is True


def test_any_type_is_mixed_and_open():
    for any_type in (SchemaBase, AnyType):
        plan = plan_layout(any_type)
        assert plan.layout is Layout.MIXED
        assert plan.children == ()
        assert plan.has_attributes is False
        assert plan.has_wildcards is True

    implicit = plan_layout(
        _compiled_type(
            """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root"/>
</xs:schema>"""
        )
    )
    assert implicit.layout is Layout.MIXED
    assert implicit.has_wildcards is True


# ---------------------------------------------------------------------------
# Substitution groups
# ---------------------------------------------------------------------------


def test_substitution_members_add_possible_child_names():
    plan = plan_layout(_compiled_type(SUBSTITUTION_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [
        ("item", 0, None),
        ("special", 0, None),
        ("extra", 0, None),
    ]


def test_an_abstract_head_is_not_a_possible_child_name():
    plan = plan_layout(_compiled_type(ABSTRACT_SUBSTITUTION_SCHEMA))

    assert plan.layout is Layout.LIST
    assert _children(plan) == [("special", 1, None)]


def test_an_abstract_head_with_no_members_contributes_no_child():
    plan = plan_layout(_compiled_type(ABSTRACT_HEAD_ONLY_SCHEMA))

    assert plan.layout is Layout.EMPTY
    assert plan.children == ()


def test_blocked_substitution_members_are_not_possible_names():
    plan = plan_layout(_compiled_type(BLOCKED_SUBSTITUTION_SCHEMA))

    assert plan.layout is Layout.LIST
    assert _children(plan) == [("item", 1, None)]


def test_an_intermediate_member_block_does_not_stop_the_closure():
    plan = plan_layout(_compiled_type(INTERMEDIATE_BLOCKED_SUBSTITUTION_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [
        ("item", 0, None),
        ("special", 0, None),
        ("extra", 0, None),
    ]


def test_cyclic_substitution_groups_terminate():
    plan = plan_layout(_compiled_type(CYCLIC_SUBSTITUTION_SCHEMA, allow_errors=True))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [("a", 0, None), ("b", 0, None)]


def test_substitution_names_are_expanded_names():
    plan = plan_layout(_compiled_type(NAMESPACED_SUBSTITUTION_SCHEMA))

    assert plan.layout is Layout.SEQUENCE
    assert _children(plan) == [
        ("{urn:example}item", 0, None),
        ("{urn:example}special", 0, None),
    ]


# ---------------------------------------------------------------------------
# Recursion and conditional types
# ---------------------------------------------------------------------------


def test_a_recursive_list_type_terminates_with_occurrence_metadata():
    root_type = _compiled_type(RECURSIVE_SCHEMA, root="node")

    first = plan_layout(root_type)
    second = plan_layout(root_type)

    assert first == second
    assert first.layout is Layout.LIST_PLUS
    assert _children(first) == [("node", 0, None)]
    assert first.has_attributes is True


def test_a_recursive_record_type_terminates():
    plan = plan_layout(_compiled_type(RECURSIVE_RECORD_SCHEMA, root="node"))

    assert plan.layout is Layout.RECORD
    assert _children(plan) == [("node", 0, 1)]


def test_conditional_types_use_the_selected_effective_type():
    schema = pyxsd.Schema.compile(StringIO(CONDITIONAL_SCHEMA))
    assert not schema.report.errors

    list_document = schema.parse(StringIO('<root kind="list"><date>2026-01-01</date></root>'))
    record_document = schema.parse(StringIO('<root kind="record"><a>x</a></root>'))

    list_plan = plan_layout(type(list_document.root))
    record_plan = plan_layout(type(record_document.root))

    assert list_plan.layout is Layout.LIST
    assert _children(list_plan) == [("date", 1, None)]
    assert record_plan.layout is Layout.RECORD
    assert _children(record_plan) == [("a", 1, 1), ("b", 0, 1)]


# ---------------------------------------------------------------------------
# Argument and unsupported-shape errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [42, "root", object(), Integer("1")])
def test_plan_layout_rejects_values_that_are_not_classes(value):
    with pytest.raises(ValueError, match="compiled XSD type class"):
        plan_layout(value)


def test_plan_layout_rejects_unrelated_classes():
    class NotAnXsdType:
        pass

    with pytest.raises(ValueError, match="compiled XSD type class"):
        plan_layout(NotAnXsdType)


def test_plan_layout_rejects_an_element_only_type_without_a_content_model():
    class MissingModel(SchemaBase):
        _contentKind_ = "complex"
        _elementOnly_ = True

    with pytest.raises(PyXSDError, match="schema-shape-unsupported"):
        plan_layout(MissingModel)
