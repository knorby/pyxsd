"""Member annotation resolution for stub emission.

Pins the seam Tasks 4-5 render from: each class's members in declaration
order, occurrence facts folded through the compiled particle tree (a
choice branch or an optional ancestor makes a member optional; a
repeated ancestor makes it a list), attribute ``use`` and value
constraints, and the degradation rules for names that are not Python
identifiers.
"""

import io

from pyxsd.schema import Schema
from pyxsd.stubs import _effective_occurs, _inventory, _members


def members_of(xsd: str, class_name: str):
    schema = Schema.compile(io.StringIO(xsd))
    inventory = _inventory(schema)
    names = {id(e.cls): e.name for e in inventory}
    entry = next(e for e in inventory if e.name == class_name)
    return {m.name: m for m in _members(entry.cls, names, compact=False)}


OCCURS = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="order"><xs:sequence>
    <xs:element name="one" type="xs:int"/>
    <xs:element name="opt" type="xs:int" minOccurs="0"/>
    <xs:element name="many" type="xs:int" maxOccurs="unbounded"/>
    <xs:element name="optmany" type="xs:int" minOccurs="0" maxOccurs="unbounded"/>
  </xs:sequence>
  <xs:attribute name="req" type="xs:string" use="required"/>
  <xs:attribute name="optAttr" type="xs:string"/>
  <xs:attribute name="off" type="xs:string" use="prohibited"/>
  </xs:complexType>
  <xs:element name="root" type="order"/>
</xs:schema>"""


def test_occurrence_annotations():
    m = members_of(OCCURS, "order")
    assert m["one"].annotation == "Int"
    assert m["opt"].annotation == "Int | None"
    assert m["many"].annotation == "list[Int]"
    assert m["optmany"].annotation == "list[Int] | None"


def test_attribute_annotations():
    m = members_of(OCCURS, "order")
    assert m["req"].annotation == "String"
    assert m["req"].kind == "attribute"
    assert m["optAttr"].annotation == "String | None"
    assert "off" not in m or m["off"].name == ""  # prohibited: comment-only


def test_prohibited_attribute_is_comment_only():
    m = members_of(OCCURS, "order")
    assert any(member.name == "" and "off" in (member.comment or "") for member in m.values())


CHOICE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="shape"><xs:choice>
    <xs:element name="square" type="xs:int"/>
    <xs:element name="circle" type="xs:int"/>
  </xs:choice></xs:complexType>
  <xs:element name="root" type="shape"/>
</xs:schema>"""


def test_choice_branch_annotated_optional():
    m = members_of(CHOICE, "shape")
    assert m["square"].annotation == "Int | None"
    assert m["circle"].annotation == "Int | None"


DEFAULTS = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="cfg"><xs:sequence>
    <xs:element name="level" type="xs:int" default="3"/>
  </xs:sequence></xs:complexType>
  <xs:element name="root" type="cfg"/>
</xs:schema>"""


def test_default_reads_as_str_when_absent():
    m = members_of(DEFAULTS, "cfg")
    assert m["level"].annotation == "Int | str | None"
    assert "default" in (m["level"].comment or "")


ATTRIBUTE_DEFAULT = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="cfg">
    <xs:attribute name="mode" type="xs:string" default="standard"/>
    <xs:attribute name="key" type="xs:string" fixed="k-1"/>
  </xs:complexType>
  <xs:element name="root" type="cfg"/>
</xs:schema>"""


def test_attribute_default_and_fixed_annotations():
    m = members_of(ATTRIBUTE_DEFAULT, "cfg")
    assert m["mode"].annotation == "String | str | None"
    assert "default" in (m["mode"].comment or "")
    assert m["key"].annotation == "String | str | None"
    assert "fixed" in (m["key"].comment or "")


REF_DEFAULT = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="item" type="xs:int" default="9"/>
  <xs:complexType name="bag"><xs:sequence>
    <xs:element ref="item" minOccurs="0"/>
  </xs:sequence></xs:complexType>
  <xs:element name="root" type="bag"/>
</xs:schema>"""


def test_reference_site_with_referred_default_is_absent_as_none():
    # The constraint lives on the referred declaration; the site's own
    # absent read has no lexical default to fall back to and returns
    # None, so the comment must not claim a raw-str read.
    m = members_of(REF_DEFAULT, "bag")
    assert m["item"].annotation == "Int | str | None"
    assert "referenced declaration" in (m["item"].comment or "")


LIST_TYPE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="intList"><xs:list itemType="xs:int"/></xs:simpleType>
  <xs:complexType name="bag"><xs:sequence>
    <xs:element name="runs" type="intList" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType>
  <xs:element name="root" type="bag"/>
</xs:schema>"""


def test_repeated_list_typed_element():
    m = members_of(LIST_TYPE, "bag")
    assert m["runs"].annotation == "list[intList]"


def test_builtin_base_methods_are_not_members():
    # ``XsdDataType`` and friends declare public methods (``lexical``,
    # ``tokens``); those are base-library API the runtime classes
    # already have, not members a simple type's body added. The stub
    # for a facet-only simple type therefore has no members at all.
    m = members_of(LIST_TYPE, "intList")
    assert m == {}


NILLABLE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="rec"><xs:sequence>
    <xs:element name="v" type="xs:int" nillable="true"/>
  </xs:sequence></xs:complexType>
  <xs:element name="root" type="rec"/>
</xs:schema>"""


def test_nillable_is_commented_not_unioned():
    m = members_of(NILLABLE, "rec")
    assert m["v"].annotation == "Int"
    assert "nillable" in (m["v"].comment or "")


def test_non_identifier_name_becomes_comment():
    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="rec"><xs:sequence>
    <xs:element name="ship-to" type="xs:string"/>
  </xs:sequence></xs:complexType>
  <xs:element name="root" type="rec"/>
</xs:schema>"""
    m = members_of(xsd, "rec")
    assert all(not member.name for member in m.values())
    assert any("ship-to" in (member.comment or "") for member in m.values())


def test_keyword_member_name_becomes_comment():
    # ``class`` is a legal XSD name and a legal class-attribute key, but
    # ``obj.class`` cannot be written; the member must not break the stub.
    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="rec"><xs:sequence>
    <xs:element name="class" type="xs:string"/>
  </xs:sequence></xs:complexType>
  <xs:element name="root" type="rec"/>
</xs:schema>"""
    m = members_of(xsd, "rec")
    assert all(not member.name for member in m.values())
    assert any("class" in (member.comment or "") for member in m.values())


DERIVED = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="base"><xs:sequence>
    <xs:element name="a" type="xs:string"/></xs:sequence></xs:complexType>
  <xs:complexType name="derived"><xs:complexContent><xs:extension base="base">
    <xs:sequence><xs:element name="b" type="xs:int"/></xs:sequence>
  </xs:extension></xs:complexContent></xs:complexType>
  <xs:element name="root" type="derived"/>
</xs:schema>"""


def test_inherited_members_are_not_rerendered():
    # The base class gets its own stub block; re-listing ``a`` on
    # ``derived`` would duplicate it and invent a second accessor.
    m = members_of(DERIVED, "derived")
    assert m["b"].kind == "element"
    assert "a" not in m


def test_overlay_merge_attributes_inherited_descriptors_to_the_overlay():
    # An overlay class replaces the generated class in ``schema.classes``
    # (``load_overlay_classes``). Descriptors defined by a base that is no
    # longer in the inventory must still render: the overlay's stub block
    # is their only home. Built with a direct subclass because an overlay
    # file cannot reference the runtime-generated class by name.
    schema = Schema.compile(io.StringIO(DERIVED))
    generated = schema.classes["base"]

    class base_overlay(generated):
        name = "base"

        @property
        def label(self) -> str:
            return "x"

    schema.classes["base"] = base_overlay
    names = {id(e.cls): e.name for e in _inventory(schema)}
    m = {member.name: member for member in _members(base_overlay, names, compact=False)}
    assert m["a"].annotation == "String"
    assert m["label"].kind == "extension"
    assert m["label"].annotation == "(self) -> str"


REPEATED_GROUP = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="rep"><xs:sequence maxOccurs="unbounded">
    <xs:element name="x" type="xs:int" minOccurs="0"/>
  </xs:sequence></xs:complexType>
  <xs:element name="root" type="rep"/>
</xs:schema>"""


def test_repeated_ancestor_forces_list_and_optional():
    m = members_of(REPEATED_GROUP, "rep")
    assert m["x"].annotation == "list[Int] | None"


def test_effective_occurs_folds_the_compiled_tree():
    schema = Schema.compile(io.StringIO(REPEATED_GROUP))
    cls = schema.classes["rep"]
    descriptor = cls.__dict__["x"]
    assert _effective_occurs(cls.__dict__["_contentModel_"], descriptor) == (0, None)


GROUP_CHOICE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:group name="g"><xs:sequence>
    <xs:element name="other" type="xs:int"/></xs:sequence></xs:group>
  <xs:complexType name="holder"><xs:choice>
    <xs:group ref="g"/>
    <xs:element name="z" type="xs:string"/>
  </xs:choice></xs:complexType>
  <xs:element name="root" type="holder"/>
</xs:schema>"""


def test_group_expanded_choice_branch_annotated_optional():
    # A named group binds per-use copies of its declarations into the
    # class body, while the particle tree carries the group's shared
    # declarations; the fold must find the copy's particle all the
    # same, or the choice branch is wrongly annotated as required.
    m = members_of(GROUP_CHOICE, "holder")
    assert m["other"].annotation == "Int | None"
    assert m["z"].annotation == "String | None"


def test_group_expanded_choice_branch_matches_a_parsed_document():
    # Side by side with the annotation: taking the other choice branch
    # leaves the group's element read as None.
    schema = Schema.compile(io.StringIO(GROUP_CHOICE))
    parsed = schema.parse(io.StringIO("<root><z>hi</z></root>"))
    assert parsed.root.other is None
    assert parsed.root.z == "hi"


GROUP_CHOICE_INSIDE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:group name="g"><xs:choice>
    <xs:element name="other" type="xs:int"/>
    <xs:element name="tail" type="xs:int"/>
  </xs:choice></xs:group>
  <xs:complexType name="holder"><xs:sequence><xs:group ref="g"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="holder"/>
</xs:schema>"""


def test_choice_inside_group_annotated_optional():
    m = members_of(GROUP_CHOICE_INSIDE, "holder")
    assert m["other"].annotation == "Int | None"
    assert m["tail"].annotation == "Int | None"


GROUP_UNBOUNDED_INSIDE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:group name="g"><xs:sequence maxOccurs="unbounded">
    <xs:element name="other" type="xs:int"/></xs:sequence></xs:group>
  <xs:complexType name="holder"><xs:sequence><xs:group ref="g"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="holder"/>
</xs:schema>"""


def test_unbounded_compositor_inside_group_annotates_a_list():
    m = members_of(GROUP_UNBOUNDED_INSIDE, "holder")
    assert m["other"].annotation == "list[Int]"


GROUP_IN_UNBOUNDED_ANCESTOR = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:group name="g"><xs:sequence>
    <xs:element name="other" type="xs:int"/></xs:sequence></xs:group>
  <xs:complexType name="holder"><xs:sequence maxOccurs="unbounded">
    <xs:group ref="g"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="holder"/>
</xs:schema>"""


def test_group_inside_unbounded_ancestor_annotates_a_list():
    m = members_of(GROUP_IN_UNBOUNDED_ANCESTOR, "holder")
    assert m["other"].annotation == "list[Int]"


NESTED_GROUPS = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:group name="inner"><xs:sequence>
    <xs:element name="deep" type="xs:int"/></xs:sequence></xs:group>
  <xs:group name="outer"><xs:sequence>
    <xs:group ref="inner"/>
    <xs:element name="shallow" type="xs:string"/></xs:sequence></xs:group>
  <xs:complexType name="holder"><xs:choice>
    <xs:group ref="outer"/>
    <xs:element name="z" type="xs:string"/>
  </xs:choice></xs:complexType>
  <xs:element name="root" type="holder"/>
</xs:schema>"""


def test_nested_group_expansion_folds_every_ancestor():
    m = members_of(NESTED_GROUPS, "holder")
    assert m["deep"].annotation == "Int | None"
    assert m["shallow"].annotation == "String | None"


def test_effective_occurs_folds_group_expanded_copies():
    schema = Schema.compile(io.StringIO(GROUP_CHOICE))
    cls = schema.classes["holder"]
    model = cls.__dict__["_contentModel_"]
    assert _effective_occurs(model, cls.__dict__["other"]) == (0, 1)


GROUP_REF_TWICE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:group name="g"><xs:sequence>
    <xs:element name="other" type="xs:int"/></xs:sequence></xs:group>
  <xs:complexType name="holder"><xs:sequence>
    <xs:choice><xs:group ref="g" maxOccurs="unbounded"/></xs:choice>
    <xs:group ref="g" minOccurs="0"/>
  </xs:sequence></xs:complexType>
  <xs:element name="root" type="holder"/>
</xs:schema>"""


def test_each_group_reference_copy_folds_through_its_own_particle():
    # Two references to one group are an ambiguity error (the schema's
    # element positions are not unique), but the copies each still fold
    # through the particle carrying their own reference occurrence: the
    # first copy's unbounded reference is still a choice branch, the
    # second copy's optional reference is not.
    schema = Schema.compile(io.StringIO(GROUP_REF_TWICE))
    cls = schema.classes["holder"]
    model = cls.__dict__["_contentModel_"]
    assert _effective_occurs(model, cls.__dict__["other"]) == (0, None)
    assert _effective_occurs(model, cls.__dict__["other|2"]) == (0, 1)
