"""Schema-shaped projection must not inherit descriptor/dictionary ambiguities."""

from decimal import Decimal

import pytest

from integration_helpers import RECORD_SCHEMA, compile_schema, parse


def projection(schema, element="root", path=()):
    from pyxsd.integrations._shape import ShapeSet

    return ShapeSet(schema).resolve(element, path)


def test_project_stable_repetition_bool_decimal_and_effective_defaults():
    from pyxsd.integrations._values import project

    schema = compile_schema(RECORD_SCHEMA)
    shape = projection(schema)
    empty = parse(schema, "<root><count>7</count></root>")
    assert project(shape, empty.root) == {"count": 7, "item": [], "@version": 9}
    document = parse(
        schema,
        "<root><count>7</count><item>1</item><flag>true</flag><amount>9999</amount></root>",
    )
    assert project(shape, document.root) == {
        "count": 7,
        "item": [1],
        "flag": True,
        "amount": {"$": Decimal("9999"), "@currency": "USD"},
        "@version": 9,
    }
    assert type(project(shape, document.root)["flag"]) is bool
    assert "@version" not in project(shape, document.root, explicit=True)


def test_projection_reads_current_scalar_lexical_state():
    from pyxsd.integrations._values import project

    schema = compile_schema(RECORD_SCHEMA)
    document = parse(schema, "<root><count>7</count></root>")
    document.root.count = 42
    assert project(projection(schema), document.root)["count"] == 42


def test_complex_nil_preserves_attributes_and_nil_member():
    from pyxsd.integrations._values import project

    schema = compile_schema(RECORD_SCHEMA)
    doc = parse(
        schema,
        '<root xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><count>7</count>'
        '<item xsi:nil="true"/><missing xsi:nil="true" reason="unknown"/></root>',
    )
    data = project(projection(schema), doc.root)
    assert data["item"] == [None]
    assert data["missing"] == {"$nil": True, "@reason": "unknown"}
    doc.root._children_[-1]._value_ = ["unexpected"]
    from pyxsd.integrations import IntegrationError

    with pytest.raises(IntegrationError, match="nil"):
        project(projection(schema), doc.root)


@pytest.mark.parametrize(
    "particle, message",
    [
        (
            '<xs:sequence maxOccurs="unbounded"><xs:element name="x" type="xs:int"/></xs:sequence>',
            "repeated",
        ),
        ('<xs:sequence><xs:any processContents="skip"/></xs:sequence>', "wildcard"),
        ('<xs:sequence><xs:element name="r" type="T" minOccurs="0"/></xs:sequence>', "recursive"),
    ],
)
def test_unsupported_reachable_shapes_fail_before_instances(particle, message):
    from pyxsd.integrations import IntegrationError

    schema = compile_schema(
        f'<xs:complexType name="T">{particle}</xs:complexType><xs:element name="root" type="T"/>'
    )
    with pytest.raises(IntegrationError, match=message):
        projection(schema)


def test_simple_choice_and_optional_group_keep_presence_relationships():
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations._values import project

    schema = compile_schema("""<xs:element name="root"><xs:complexType><xs:sequence>
      <xs:choice><xs:element name="a" type="xs:int"/><xs:element name="b" type="xs:int"/></xs:choice>
      <xs:sequence minOccurs="0"><xs:element name="x" type="xs:int"/>
        <xs:element name="y" type="xs:int"/></xs:sequence>
      </xs:sequence></xs:complexType></xs:element>""")
    doc = parse(schema, "<root><a>1</a><x>2</x><y>3</y></root>")
    shape = projection(schema)
    assert project(shape, doc.root) == {"a": 1, "x": 2, "y": 3}
    doc.root._children_.pop()
    with pytest.raises(IntegrationError, match="y"):
        project(shape, doc.root)


def test_local_lookup_and_declaration_identity_are_schema_scoped():
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations._values import project

    schema = compile_schema(RECORD_SCHEMA)
    doc = parse(schema, "<root><count>7</count></root>")
    assert project(projection(schema, path=("count",)), doc.root._children_[0]) == 7
    other = compile_schema(RECORD_SCHEMA)
    with pytest.raises(IntegrationError, match="declaration"):
        project(projection(other, path=("count",)), doc.root._children_[0])


def test_unrelated_unsupported_declaration_does_not_block_supported_projection():
    schema = compile_schema(
        RECORD_SCHEMA + '<xs:element name="other"><xs:complexType mixed="true"/></xs:element>'
    )
    assert projection(schema).name == "root"


def test_xsd_list_lexical_form_round_trips_for_fresh_validation():
    schema = compile_schema("""<xs:simpleType name="L"><xs:list itemType="xs:int"/></xs:simpleType>
    <xs:element name="root" type="L"/>""")
    document = parse(schema, "<root>1 2</root>")
    assert document.root.lexical() == "1 2"
    document.revalidate().require_valid()


@pytest.mark.parametrize("type_name, lexical", [("decimal", "0.00000000001"), ("double", "INF")])
def test_native_scalar_lexical_form_round_trips_without_python_spellings(type_name, lexical):
    schema = compile_schema(f'<xs:element name="root" type="xs:{type_name}"/>')
    document = parse(schema, f"<root>{lexical}</root>")
    assert document.root.lexical() == lexical
    document.revalidate().require_valid()


@pytest.mark.parametrize(
    "element, path", [("missing", ()), ("root", ("absent",)), ("root", ["count"])]
)
def test_missing_or_invalid_declaration_selection_is_rejected(element, path):
    from pyxsd.integrations import IntegrationError

    with pytest.raises(IntegrationError, match="declaration"):
        projection(compile_schema(RECORD_SCHEMA), element, path)


@pytest.mark.parametrize(
    "body, reason",
    [
        ('<xs:complexType mixed="true"/>', "mixed"),
        ("<xs:complexType><xs:anyAttribute/></xs:complexType>", "wildcard"),
        (
            '<xs:complexType><xs:choice><xs:sequence><xs:element name="a" type="xs:int"/></xs:sequence><xs:element name="b" type="xs:int"/></xs:choice></xs:complexType>',
            "choice",
        ),
        ('<xs:simpleType><xs:union memberTypes="xs:int xs:string"/></xs:simpleType>', "union"),
    ],
)
def test_other_unsupported_shapes_are_diagnosed(body, reason):
    from pyxsd.integrations import IntegrationError

    schema = compile_schema(f'<xs:element name="root">{body}</xs:element>')
    with pytest.raises(IntegrationError, match=reason):
        projection(schema)


def test_fixed_and_prohibited_attributes_and_scalar_fixed_mutation():
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations._values import project

    schema = compile_schema("""<xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="v" type="xs:int" fixed="2"/>
    </xs:sequence><xs:attribute name="fixed" type="xs:int" fixed="2"/>
    <xs:attribute name="unused" use="prohibited"/></xs:complexType></xs:element>""")
    doc = parse(schema, '<root fixed="2"><v>2</v></root>')
    shape = projection(schema)
    assert project(shape, doc.root) == {"@fixed": 2, "v": 2}
    for name, raw, reason in [
        ("fixed", "3", "fixed"),
        ("unused", "x", "prohibited"),
        ("other", "x", "undeclared"),
    ]:
        doc.root._attribs_ = {name: raw}
        with pytest.raises(IntegrationError, match=reason):
            project(shape, doc.root)
    doc.root._attribs_ = {}
    doc.root._children_[0]._value_ = ["3"]
    with pytest.raises(IntegrationError, match="fixed"):
        project(shape, doc.root)


def test_required_attributes_and_unexpected_text_or_child_mutations():
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations._values import project

    schema = compile_schema(RECORD_SCHEMA)
    doc = parse(schema, '<root><count>7</count><missing reason="known"/></root>')
    shape = projection(schema)
    doc.root._children_[1]._attribs_ = {}
    with pytest.raises(IntegrationError, match="required attribute"):
        project(shape, doc.root)
    doc.root._children_.pop()
    doc.root._value_ = ["unexpected"]
    with pytest.raises(IntegrationError, match="unexpected text"):
        project(shape, doc.root)
    doc.root._value_ = None
    doc.root._children_.append(doc.root)
    with pytest.raises(IntegrationError, match="undeclared child"):
        project(shape, doc.root)


def test_qname_snapshot_expanded_identity_and_stale_context_rejection():
    from pyxsd.integrations import IntegrationError
    from pyxsd.integrations._values import project

    schema = compile_schema('<xs:element name="root" type="xs:QName"/>')
    doc = parse(schema, '<root xmlns:p="urn:t">p:value</root>')
    shape = projection(schema)
    assert project(shape, doc.root) == "{urn:t}value"
    doc.root._value_ = ["p:other"]
    with pytest.raises(IntegrationError, match="context"):
        project(shape, doc.root)


def test_builtin_token_lists_are_list_values_not_lexical_strings():
    from pyxsd.integrations._values import project

    schema = compile_schema('<xs:element name="root" type="xs:NMTOKENS"/>')
    doc = parse(schema, "<root>a b</root>")
    assert project(projection(schema), doc.root) == ["a", "b"]
