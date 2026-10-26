"""Native descriptions preserve schema semantics without projection adapters."""

from dataclasses import FrozenInstanceError
from io import StringIO

import pytest

import pyxsd

SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:d="urn:demo" targetNamespace="urn:demo" elementFormDefault="qualified">
  <xs:simpleType name="Small"><xs:restriction base="xs:integer">
    <xs:minInclusive value="0"/><xs:maxInclusive value="10"/>
    <xs:pattern value="[0-9]+"/>
  </xs:restriction></xs:simpleType>
  <xs:group name="Content"><xs:choice>
    <xs:element name="child" type="d:Node"/>
    <xs:element name="value" type="d:Small" nillable="true" default="1"/>
  </xs:choice></xs:group>
  <xs:complexType name="Node" mixed="true">
    <xs:annotation><xs:documentation>A recursive node.</xs:documentation></xs:annotation>
    <xs:sequence><xs:group ref="d:Content" minOccurs="0" maxOccurs="4"/>
      <xs:element name="code" type="xs:string" minOccurs="0"/>
    </xs:sequence>
    <xs:attribute name="code" type="xs:string" fixed="X"/>
    <xs:assert test="true()"/>
  </xs:complexType>
  <xs:complexType name="Derived" mixed="true"><xs:complexContent mixed="true">
    <xs:extension base="d:Node"><xs:sequence>
      <xs:any namespace="##other" processContents="lax" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence></xs:extension>
  </xs:complexContent></xs:complexType>
  <xs:element name="root" type="d:Node">
    <xs:key name="codes"><xs:selector xpath="d:child"/><xs:field xpath="@code"/></xs:key>
  </xs:element>
  <xs:element name="alternate" type="d:Derived" substitutionGroup="d:root"/>
  <xs:element name="other"><xs:complexType><xs:sequence>
    <xs:element name="value" type="xs:string"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""


def compile_schema(text=SCHEMA):
    return pyxsd.compile(StringIO(text), mode=pyxsd.ParseModes.LAX.replace(namespaces="strict"))


def walk(particle):
    yield particle
    for child in particle.children:
        yield from walk(child)


def test_native_descriptions_preserve_groups_aliases_and_effective_constraints():
    from pyxsd.introspection import describe_component, describe_schema

    schema = compile_schema()
    schema.require_valid()
    overview = describe_schema(schema)
    assert "element:{urn:demo}root" in overview.roots
    node = describe_component(schema, "type:{urn:demo}Node")
    assert node.name == "{urn:demo}Node"
    assert node.mixed and node.documentation == "A recursive node."
    assert node.assertions == ("true()",)
    choice = next(p for p in walk(node.particle) if p.kind == "choice")
    assert (choice.min_occurs, choice.max_occurs) == (1, 1)
    assert any((p.min_occurs, p.max_occurs) == (0, 4) for p in walk(node.particle))
    code = next(m for m in node.members if m.kind == "element" and m.xml_name.endswith("}code"))
    assert code.python_name == "code_element"
    attribute = next(m for m in node.members if m.kind == "attribute")
    assert attribute.python_name == "code" and attribute.fixed == "X"
    small = describe_component(schema, "type:{urn:demo}Small")
    assert {f.name: f.value for f in small.facets}["max_inclusive"] == 10
    assert {f.name: f.value for f in small.facets}["patterns"] == (("[0-9]+",),)
    value = next(m for m in node.members if m.xml_name.endswith("}value"))
    assert value.nillable and value.default == "1"
    assert value.occurrence_precision == "conservative"
    assert value.python_type == "Small"
    assert {f.name: f.value for f in value.facets}["min_inclusive"] == 0
    with pytest.raises(FrozenInstanceError):
        node.name = "changed"


def test_ids_distinguish_local_declarations_and_survive_equivalent_compiles():
    from pyxsd.introspection import describe_schema, find_components

    first, second = compile_schema(), compile_schema()
    assert describe_schema(first) == describe_schema(second)
    values = find_components(first, text="value", kind="element")
    assert len(values) == 2
    assert len({v.id for v in values}) == 2
    assert all(not v.global_declaration and v.location.path for v in values)
    anonymous = [c for c in find_components(first, kind="type") if c.name is None]
    assert len(anonymous) == 1
    assert anonymous[0].owner_id == "element:{urn:demo}other"
    assert find_components(first, namespace="absent") == ()
    assert find_components(first, namespace="urn:demo")


def test_derivation_substitution_identity_and_wildcards_are_native_facts():
    from pyxsd.introspection import describe_component

    schema = compile_schema()
    derived = describe_component(schema, "type:{urn:demo}Derived")
    assert derived.derivation == "extension"
    assert "type:{urn:demo}Node" in derived.base_types
    wildcard = next(p.wildcard for p in walk(derived.particle) if p.kind == "any")
    assert wildcard.namespace == "##other" and wildcard.process_contents == "lax"
    alternate = describe_component(schema, "element:{urn:demo}alternate")
    assert alternate.substitution_group == "element:{urn:demo}root"
    root = describe_component(schema, "element:{urn:demo}root")
    key = root.identity_constraints[0]
    assert (key.kind, key.name, key.selector, key.fields) == ("key", "codes", "d:child", ("@code",))


def test_graph_traversal_is_bounded_and_bidirectional():
    from pyxsd.introspection import describe_schema, related_components

    schema = compile_schema()
    outgoing = related_components(schema, "element:{urn:demo}root", direction="uses", depth=8)
    ids = [c.id for c in outgoing]
    assert "type:{urn:demo}Node" in ids and "type:{urn:demo}Small" in ids
    assert len(ids) == len(set(ids)) < len(describe_schema(schema).components)
    incoming = related_components(schema, "type:{urn:demo}Node", direction="used_by", depth=1)
    assert "element:{urn:demo}root" in {c.id for c in incoming}
    assert related_components(schema, "type:{urn:demo}Node", direction="uses", depth=0) == ()
    with pytest.raises(ValueError, match="direction"):
        related_components(schema, "type:{urn:demo}Node", direction="sideways", depth=1)
    with pytest.raises(ValueError, match="depth"):
        related_components(schema, "type:{urn:demo}Node", direction="uses", depth=-1)


def test_unknown_component_is_not_silently_substituted():
    from pyxsd.introspection import describe_component, related_components

    schema = compile_schema()
    with pytest.raises(KeyError):
        describe_component(schema, "type:missing")
    with pytest.raises(KeyError):
        related_components(schema, "type:missing", direction="uses", depth=1)


def test_native_exploration_does_not_require_a_projectable_shape():
    from pyxsd.introspection import describe_component

    schema = compile_schema()
    assert describe_component(schema, "type:{urn:demo}Node").mixed


def test_projected_models_reject_native_mixed_content():
    pytest.importorskip("pydantic")
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations import pydantic as integration

    with pytest.raises(IntegrationError, match="mixed"):
        integration.models(compile_schema()).model_for(element="{urn:demo}root")


def test_effective_assertions_and_named_group_graphs():
    from pyxsd.introspection import describe_component, related_components

    schema = compile_schema()
    group = describe_component(schema, "group:{urn:demo}Content")
    assert group.particle.kind == "choice"
    assert {p.component_id for p in walk(group.particle) if p.kind == "element"}
    derived = describe_component(schema, "type:{urn:demo}Derived")
    assert derived.assertions == ("true()",)
    assert "type:{urn:demo}Small" in {
        c.id for c in related_components(schema, group.id, direction="uses", depth=3)
    }


def test_pattern_groups_are_not_flattened_across_inheritance():
    from pyxsd.introspection import describe_component

    schema = compile_schema("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:simpleType name="Base"><xs:restriction base="xs:string">
        <xs:pattern value="a.*"/><xs:pattern value="b.*"/>
      </xs:restriction></xs:simpleType>
      <xs:simpleType name="Derived"><xs:restriction base="Base">
        <xs:pattern value=".*z"/>
      </xs:restriction></xs:simpleType>
      <xs:element name="root" type="Derived"/>
    </xs:schema>""")
    facets = {f.name: f for f in describe_component(schema, "type:Derived").facets}
    assert facets["patterns"].value == (("a.*", "b.*"), (".*z",))
    assert facets["patterns"].representation == "xsd"


def test_attribute_use_occurrences_and_attribute_groups():
    from pyxsd.introspection import describe_component

    schema = compile_schema("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:attributeGroup name="Attrs"><xs:attribute name="required" type="xs:int" use="required"/>
        <xs:attribute name="optional" type="xs:string"/>
      </xs:attributeGroup>
      <xs:complexType name="Record"><xs:attributeGroup ref="Attrs"/>
        <xs:attribute name="forbidden" use="prohibited"/>
        <xs:anyAttribute namespace="##other" processContents="skip"/>
      </xs:complexType><xs:element name="root" type="Record"/>
    </xs:schema>""")
    record = describe_component(schema, "type:Record")
    members = {m.xml_name: m for m in record.members}
    assert (members["required"].min_occurs, members["required"].max_occurs) == (1, 1)
    assert (members["optional"].min_occurs, members["optional"].max_occurs) == (0, 1)
    assert (members["forbidden"].min_occurs, members["forbidden"].max_occurs) == (0, 0)
    assert record.attribute_wildcard.process_contents == "skip"
    attrs = describe_component(schema, "attributeGroup:Attrs")
    assert {m.xml_name for m in attrs.members} == {"required", "optional"}


@pytest.mark.parametrize("directive", ["include", "redefine"])
def test_composed_declarations_keep_original_source_addresses(tmp_path, directive):
    from pyxsd.introspection import describe_component, find_components

    base = tmp_path / "base.xsd"
    base.write_text(
        """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="T"><xs:sequence><xs:element name="value" type="xs:string"/>
      </xs:sequence></xs:complexType></xs:schema>""",
        encoding="utf-8",
    )
    body = (
        ""
        if directive == "include"
        else """<xs:complexType name="T"><xs:complexContent>
      <xs:extension base="t:T"><xs:attribute name="count" type="xs:int"/></xs:extension>
      </xs:complexContent></xs:complexType>"""
    )
    main = tmp_path / "main.xsd"
    main.write_text(
        f"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
      xmlns:t="urn:types" targetNamespace="urn:types">
      <xs:{directive} schemaLocation="base.xsd">{body}</xs:{directive}>
      <xs:element name="root" type="t:T"/></xs:schema>""",
        encoding="utf-8",
    )
    schema = pyxsd.compile(main, mode=pyxsd.ParseModes.LAX.replace(namespaces="strict"))
    schema.require_valid()
    assert describe_component(schema, "type:{urn:types}T").location.source == str(
        main.resolve() if directive == "redefine" else base.resolve()
    )
    value = find_components(schema, text="value", kind="element")[0]
    assert value.location.source == str(base.resolve())
    assert [(step.name.rsplit("}", 1)[-1], step.index) for step in value.location.path] == [
        ("schema", 1),
        ("complexType", 1),
        ("sequence", 1),
        ("element", 1),
    ]


def test_error_bearing_schema_keeps_native_inventory_and_report_unchanged():
    from pyxsd.introspection import describe_component

    schema = compile_schema("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="Broken"><xs:notAllowed/></xs:complexType>
      <xs:element name="root" type="Missing"/>
    </xs:schema>""")
    before = schema.report.to_dict()
    assert schema.report.has_errors
    assert describe_component(schema, "type:Broken").name == "Broken"
    assert describe_component(schema, "element:root").python_type is None
    assert schema.report.to_dict() == before


def test_native_members_respect_restriction_overrides_and_shadowed_accessors():
    from pyxsd.introspection import describe_component

    schema = compile_schema("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="Base"><xs:attribute name="code" type="xs:int"/>
        <xs:attribute name="hidden" type="xs:string"/>
      </xs:complexType>
      <xs:complexType name="Derived"><xs:complexContent><xs:restriction base="Base">
        <xs:attribute name="code" type="xs:int" use="required"/>
        <xs:attribute name="hidden" type="xs:string"/>
      </xs:restriction></xs:complexContent></xs:complexType>
      <xs:element name="root" type="Derived"/></xs:schema>""")
    schema.classes["Derived"].hidden = property(lambda self: "computed")
    derived = describe_component(schema, "type:Derived")
    code = [m for m in derived.members if m.xml_name == "code"]
    assert len(code) == 1 and code[0].use == "required"
    assert not any(m.python_name == "hidden" for m in derived.members)


def test_decimal_and_date_enumerations_have_immutable_exact_values():
    from decimal import Decimal

    from pyxsd.introspection import describe_component

    schema = compile_schema("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:simpleType name="Amount"><xs:restriction base="xs:decimal">
        <xs:minInclusive value="0.10"/><xs:enumeration value="0.25"/>
      </xs:restriction></xs:simpleType>
      <xs:simpleType name="Day"><xs:restriction base="xs:date">
        <xs:enumeration value="2026-10-25"/>
      </xs:restriction></xs:simpleType>
    </xs:schema>""")
    amount = {f.name: f.value for f in describe_component(schema, "type:Amount").facets}
    assert amount["min_inclusive"] == Decimal("0.10")
    day = {f.name: f.value for f in describe_component(schema, "type:Day").facets}
    assert day["enumeration_values"] == ("2026-10-25",)


def test_compiled_pattern_fallback_is_explicit_when_class_inventory_is_absent():
    import re

    from pyxsd.introspection import describe_component

    schema = compile_schema("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:simpleType name="Base"><xs:restriction base="xs:string">
        <xs:pattern value="a.*"/>
      </xs:restriction></xs:simpleType>
      <xs:simpleType name="Derived"><xs:restriction base="Base"><xs:minLength value="2"/>
      </xs:restriction></xs:simpleType><xs:element name="root" type="Derived"/>
    </xs:schema>""")
    schema.classes.pop("Base")
    schema.classes.pop("Derived")
    schema.parse(StringIO("<root>ab</root>")).require_valid()
    description = describe_component(schema, "type:Derived")
    pattern = next(f for f in description.facets if f.name == "patterns")
    assert pattern.representation == "python-regex"
    assert re.fullmatch(pattern.value[0][0], "ab")
    assert not re.fullmatch(pattern.value[0][0], "bb")


def test_simple_content_is_not_misreported_as_mixed_content():
    from pyxsd.introspection import describe_component

    schema = compile_schema("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="Amount"><xs:simpleContent><xs:extension base="xs:decimal">
        <xs:attribute name="currency" type="xs:string"/>
      </xs:extension></xs:simpleContent></xs:complexType>
      <xs:element name="root" type="Amount"/></xs:schema>""")
    assert not describe_component(schema, "type:Amount").mixed
    assert not describe_component(schema, "element:root").mixed


def test_restriction_without_an_own_pattern_preserves_its_base_pattern():
    from pyxsd.introspection import describe_component

    schema = compile_schema("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:simpleType name="Base"><xs:restriction base="xs:string">
        <xs:pattern value="a.*"/>
      </xs:restriction></xs:simpleType>
      <xs:simpleType name="Derived"><xs:restriction base="Base"><xs:minLength value="2"/>
      </xs:restriction></xs:simpleType>
    </xs:schema>""")
    pattern = next(
        f for f in describe_component(schema, "type:Derived").facets if f.name == "patterns"
    )
    assert pattern.representation == "xsd" and pattern.value == (("a.*",),)


def test_any_type_keeps_its_mixed_content_capability():
    from pyxsd.introspection import describe_component

    schema = compile_schema(
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="root"/></xs:schema>'
    )
    assert describe_component(schema, "element:root").mixed


@pytest.mark.parametrize(
    "allowed,invalid,expected", [("true", "false", True), ("false", "true", False)]
)
def test_boolean_enumerations_remain_booleans_in_descriptions_and_report_json(
    allowed, invalid, expected
):
    import json

    from pyxsd.introspection import describe_component

    schema = compile_schema(f'''<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:simpleType name="Flag"><xs:restriction base="xs:boolean">
        <xs:enumeration value="{allowed}"/>
      </xs:restriction></xs:simpleType><xs:element name="root" type="Flag"/>
    </xs:schema>''')
    description = describe_component(schema, "type:Flag")
    enum = next(f.value for f in description.facets if f.name == "enumeration_values")
    assert enum[0] is expected
    report = schema.parse(StringIO(f"<root>{invalid}</root>")).report
    evidence = json.loads(report.to_json())["issues"][0]["constraint"]["expected"]
    assert evidence[0] is expected
