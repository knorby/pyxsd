"""The ``SchemaPool`` element graph: lazy, memoised, and cycle-safe.

A recursive or mutually recursive content model must compose into a finite
graph whose cycle is object identity, expose every level of a deep finite
model, and terminate every traversal API. Each declaration is adapted once
per schema proxy, and a declaration's children resolve on first use.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyxsd
from pyxsd.schema import Schema
from pyxsd.xdm.schema import PyxsdElement, SchemaPool

XSD_NS = "http://www.w3.org/2001/XMLSchema"
XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
T = "urn:t"


def _schema(body: str) -> str:
    return f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{T}"
        xmlns:t="{T}" elementFormDefault="qualified">
      {body}
    </xs:schema>"""


def _compose(body: str) -> tuple[Schema, SchemaPool]:
    schema = Schema.compile(io.StringIO(_schema(body)))
    schema.require_valid()
    return schema, SchemaPool.from_schema(schema)


def _iter_names(root: PyxsdElement, limit: int = 5000) -> list[str]:
    """Collect a whole traversal, failing rather than hanging on a cycle.

    ``iter()`` skips a branch that repeats a declaration already on it.
    If that guard regresses, an unbounded walk of the bundled recursive
    schema would never return; the bound turns that into a test failure.
    """
    names: list[str] = []
    for element in root.iter():
        names.append(element.name or "")
        assert len(names) <= limit, "schema graph traversal did not terminate"
    return names


RECURSIVE = """
  <xs:element name="a" type="t:a"/>
  <xs:complexType name="a">
    <xs:sequence><xs:element ref="t:a" minOccurs="0"/></xs:sequence>
  </xs:complexType>
"""

MUTUAL = """
  <xs:element name="a" type="t:a"/>
  <xs:element name="b" type="t:b"/>
  <xs:complexType name="a">
    <xs:sequence><xs:element ref="t:b" minOccurs="0"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="b">
    <xs:sequence><xs:element ref="t:a" minOccurs="0"/></xs:sequence>
  </xs:complexType>
"""


def test_self_recursive_content_model_composes() -> None:
    _, pool = _compose(RECURSIVE)
    element = pool.get_element(f"{{{T}}}a")
    assert element is not None
    assert element.type is not None
    assert element.type.name == f"{{{T}}}a"
    assert len(element.children) == 1
    assert len(element) == 1

    child = element.children[0]
    assert child.name == f"{{{T}}}a"
    assert child.type is element.type
    # The cycle is the same adapter, not a copy or a truncated branch.
    assert child.children[0] is child


def test_recursive_declaration_validates_an_instance() -> None:
    schema, pool = _compose(RECURSIVE)
    element = pool.get_element(f"{{{T}}}a")
    assert element is not None
    document = schema.parse(io.BytesIO(b'<t:a xmlns:t="urn:t"><t:a/></t:a>'))
    assert document.is_valid
    assert element.is_valid(document.root)


def test_mutually_recursive_content_models_compose() -> None:
    _, pool = _compose(MUTUAL)
    element = pool.get_element(f"{{{T}}}a")
    assert element is not None
    assert element.type is not None
    assert element.type.name == f"{{{T}}}a"

    first = element.children[0]
    assert first.name == f"{{{T}}}b"
    assert first.type is not None
    assert first.type.name == f"{{{T}}}b"
    second = first.children[0]
    assert second.name == f"{{{T}}}a"
    # a -> b -> a closes the cycle on the b reference already built.
    assert second.children[0] is first


def test_recursive_traversal_terminates_with_the_documented_shape() -> None:
    _, pool = _compose(RECURSIVE)
    element = pool.get_element(f"{{{T}}}a")
    assert element is not None

    # One declaration plus the reference that closes the cycle: a bounded,
    # exact count rather than a timeout.
    assert _iter_names(element) == [f"{{{T}}}a", f"{{{T}}}a"]
    assert [item.name for item in element.iter("*")] == [f"{{{T}}}a", f"{{{T}}}a"]
    assert element.find(f"./{{{T}}}a") is element.children[0]
    assert element.find(f"{{{T}}}a") is element.children[0]
    assert element.find(f"{{{T}}}missing") is None
    assert len(element) == 1
    assert list(element) == [element.children[0]]


def _deep_schema(depth: int) -> str:
    declarations = ['<xs:element name="n0" type="t:t0"/>']
    for level in range(depth + 1):
        if level == depth:
            declarations.append(f'<xs:complexType name="t{level}"><xs:sequence/></xs:complexType>')
            continue
        declarations.append(
            f'<xs:complexType name="t{level}"><xs:sequence>'
            f'<xs:element name="n{level + 1}" type="t:t{level + 1}" minOccurs="0"/>'
            "</xs:sequence></xs:complexType>"
        )
    return "\n".join(declarations)


def test_deep_finite_content_model_is_not_truncated() -> None:
    depth = 30
    _, pool = _compose(_deep_schema(depth))
    element = pool.get_element(f"{{{T}}}n0")
    assert element is not None

    names = []
    node = element
    while node.children:
        node = node.children[0]
        names.append(node.name)
    assert len(names) == depth
    assert names[-1] == f"{{{T}}}n{depth}"
    assert len(list(element.iter())) == depth + 1


def test_non_recursive_element_graph_is_unchanged() -> None:
    _, pool = _compose(
        """
        <xs:element name="order">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="head" type="xs:string"/>
              <xs:element name="item" type="xs:int" maxOccurs="unbounded"/>
            </xs:sequence>
          </xs:complexType>
        </xs:element>
        """
    )
    order = pool.get_element(f"{{{T}}}order")
    assert order is not None
    assert [child.name for child in order.children] == [
        f"{{{T}}}head",
        f"{{{T}}}item",
    ]
    assert [child.type.name for child in order.children if child.type is not None] == [
        f"{{{XSD_NS}}}string",
        f"{{{XSD_NS}}}int",
    ]
    assert [child.name for child in order] == [f"{{{T}}}head", f"{{{T}}}item"]
    assert [item.name for item in order.iter()] == [
        f"{{{T}}}order",
        f"{{{T}}}head",
        f"{{{T}}}item",
    ]
    model = order.type.model_group if order.type is not None else None
    assert model is not None
    assert [child.name for child in model.iter_elements()] == [
        child.name for child in order.children
    ]


def test_element_named_schema_keeps_its_adapter() -> None:
    """A declaration named ``schema`` survives the class's schema stamp.

    The compile stamps the owning ``Schema`` onto every generated class
    under the ``schema`` attribute. Before the class builder reserved that
    key, an element declaration of the same name was replaced by the stamp
    and ``_getElements`` raised ``AttributeError``.
    """
    schema, pool = _compose(
        """
        <xs:element name="env" type="t:environmentType"/>
        <xs:complexType name="environmentType">
          <xs:sequence>
            <xs:element name="schema" type="xs:string" minOccurs="0"/>
            <xs:element name="source" type="xs:string" minOccurs="0"/>
          </xs:sequence>
        </xs:complexType>
        """
    )
    element = pool.get_element(f"{{{T}}}env")
    assert element is not None
    assert [child.name for child in element.children] == [
        f"{{{T}}}schema",
        f"{{{T}}}source",
    ]
    assert schema.classes["environmentType"].schema is schema


def test_attribute_named_schema_keeps_its_adapter() -> None:
    """Attribute declarations named ``schema`` survive the class stamp.

    The compile stamps the owning ``Schema`` onto every generated class
    under the ``schema`` attribute, after the class body exists. A local
    attribute use and a reference to a global, namespaced attribute both
    bind a descriptor under that name; without an alias the stamp replaced
    the descriptor while ``_attributeNames_`` still listed the key, and
    ``_attribute_descriptors`` read ``name`` off the ``Schema``.
    """
    schema, pool = _compose(
        """
        <xs:attribute name="schema" type="xs:string"/>
        <xs:element name="local" type="t:localType"/>
        <xs:complexType name="localType">
          <xs:attribute name="schema" type="xs:int"/>
        </xs:complexType>
        <xs:element name="referring" type="t:referringType"/>
        <xs:complexType name="referringType">
          <xs:attribute ref="t:schema"/>
        </xs:complexType>
        """
    )

    cases = (
        ("local", f"{{{XSD_NS}}}int", b'<t:local xmlns:t="urn:t" schema="5"/>', 5),
        ("referring", f"{{{XSD_NS}}}string", b'<t:referring xmlns:t="urn:t" t:schema="x"/>', "x"),
    )
    for declaration, expected_type, instance, value in cases:
        element = pool.get_element(f"{{{T}}}{declaration}")
        assert element is not None
        descriptor = element.attrib.get("schema")
        assert descriptor is not None
        assert descriptor.name == "schema"
        assert descriptor.type is not None
        assert descriptor.type.name == expected_type

        document = schema.parse(io.BytesIO(instance))
        assert document.is_valid
        # The value binds through the alias; ``schema`` itself is the stamp.
        assert document.root.schema_attribute == value

    for type_name in ("localType", "referringType"):
        cls = schema.classes[type_name]
        # The stamp keeps the natural accessor...
        assert cls.schema is schema
        # ...and the declaration is bound under its alias, which
        # ``_attributeNames_`` records like any other use.
        assert cls._attributeNames_ == ["schema_attribute"]
        bound = cls.__dict__["schema_attribute"]
        assert bound.name == "schema"
        assert bound.bindingKey == "schema_attribute"


def test_xhtml_shaped_recursion_composes() -> None:
    _, pool = _compose(
        """
        <xs:element name="div">
          <xs:complexType>
            <xs:choice minOccurs="0" maxOccurs="unbounded">
              <xs:element ref="t:div"/>
              <xs:element name="p" type="xs:string"/>
            </xs:choice>
          </xs:complexType>
        </xs:element>
        """
    )
    div = pool.get_element(f"{{{T}}}div")
    assert div is not None
    assert [child.name for child in div.children] == [f"{{{T}}}div", f"{{{T}}}p"]
    assert div.children[0].children[0] is div.children[0]
    # div, the div reference, and ``p`` under the reference; the ``p``
    # reached through div's own content model is a second branch, not a
    # cycle, so it is walked again.
    assert len(list(div.iter())) == 4
    assert len(list(div.iter(f"{{{T}}}p"))) == 2


def test_bundled_xslt_schema_composes() -> None:
    resource = Path(pyxsd.__file__).parent / "xslt" / "resources" / "schema-for-xslt20.xsd"
    pool = SchemaPool.from_imports([resource])
    template = pool.get_element(f"{{{XSLT_NS}}}template")
    assert template is not None
    names = [child.name for child in template.children]
    # The template content model is recursive: ``literal-result-element``
    # accepts instruction content that can contain another template. The
    # graph still terminates and keeps every declaration it reaches.
    assert f"{{{XSLT_NS}}}literal-result-element" in names
    visited = _iter_names(template)
    assert visited[0] == f"{{{XSLT_NS}}}template"
    assert visited.count(f"{{{XSLT_NS}}}literal-result-element") >= 2
    assert len(visited) > len(template)


def test_declaration_without_a_type_has_no_children() -> None:
    _, pool = _compose(RECURSIVE)
    declaration = SimpleNamespace(expandedInstanceName=lambda **_: "t:free")
    element = PyxsdElement(pool, declaration, None)
    assert element.type is None
    assert element.children == ()
    assert list(element) == []
    assert len(element) == 0
    assert [item.name for item in element.iter()] == ["t:free"]


def test_attribute_api_shape_survives_the_lazy_graph() -> None:
    """``attrib`` stays a mapping from the element's type."""
    _, pool = _compose(
        """
        <xs:element name="node" type="t:nodeType"/>
        <xs:complexType name="nodeType">
          <xs:sequence><xs:element name="child" type="xs:string" minOccurs="0"/></xs:sequence>
          <xs:attribute name="id" type="xs:string"/>
        </xs:complexType>
        """
    )
    node = pool.get_element(f"{{{T}}}node")
    assert isinstance(node, PyxsdElement)
    assert node is not None
    attrib: Any = node.attrib
    assert "id" in attrib
    assert node.get("id") is attrib["id"]
