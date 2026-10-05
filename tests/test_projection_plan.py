"""Contextual scalar plans use compiled declarations, never optional adapters."""

from dataclasses import FrozenInstanceError

import pytest

from integration_helpers import compile_schema, parse
from pyxsd.integrations import IntegrationError


def test_field_source_arguments():
    from pyxsd.integrations.projection import FieldSource

    assert FieldSource().path == ()
    assert FieldSource(scope="ancestor", levels=2, path=("{urn:x}v",)).levels == 2
    with pytest.raises(FrozenInstanceError):
        FieldSource().levels = 1
    for arguments in [
        {"scope": "bad"},
        {"path": "x"},
        {"path": ["x"]},
        {"path": (1,)},
        {"path": ("",)},
        {"path": ("$",)},
        {"attribute": ""},
        {"attribute": "@x"},
        {"attribute": 1},
        {"levels": True},
        {"levels": 1},
        {"scope": "ancestor"},
        {"scope": "ancestor", "levels": -1},
        {"scope": "root", "levels": 1},
    ]:
        with pytest.raises(IntegrationError):
            FieldSource(**arguments)


def test_resolve_local_row_route():
    from pyxsd.integrations._shape import resolve_route

    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="v" type="xs:int" maxOccurs="unbounded"/>
    </xs:sequence></xs:complexType></xs:element>""")
    route = resolve_route(schema, "r", ("v",))
    assert [d.instanceName(parser=schema._host) for d in route.declarations] == ["r", "v"]
    assert route.edges[0].max_occurs is None
    for path in [("absent",), "v", ("",)]:
        with pytest.raises(IntegrationError):
            resolve_route(schema, "r", path)


def test_route_distinguishes_reused_declarations():
    from pyxsd.integrations._shape import resolve_route

    schema = compile_schema("""<xs:element name="v" type="xs:int"/>
    <xs:complexType name="T"><xs:sequence><xs:element ref="v"/></xs:sequence></xs:complexType>
    <xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="a" type="T"/><xs:element name="b" type="T"/>
    </xs:sequence></xs:complexType></xs:element>""")
    a, b = resolve_route(schema, "r", ("a", "v")), resolve_route(schema, "r", ("b", "v"))
    assert a.declarations[-1] is b.declarations[-1]
    assert a.edges != b.edges
    ambiguous = compile_schema("""<xs:element name="v" type="xs:int"/>
    <xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element ref="v"/><xs:element ref="v"/>
    </xs:sequence></xs:complexType></xs:element>""")
    with pytest.raises(IntegrationError, match="ambiguous"):
        resolve_route(ambiguous, "r", ("v",))


def test_resolve_does_not_build_unselected_subtrees():
    from pyxsd.integrations._shape import ShapeSet, resolve_route

    schema = compile_schema("""<xs:complexType name="T"><xs:sequence>
    <xs:element name="again" type="T" minOccurs="0"/></xs:sequence></xs:complexType>
    <xs:element name="r"><xs:complexType><xs:sequence><xs:element name="v" type="xs:int"/>
    <xs:element name="deep" type="T" minOccurs="0"/></xs:sequence></xs:complexType></xs:element>""")
    assert resolve_route(schema, "r", ("v",)).declarations[-1].getType().name == "int"
    with pytest.raises(IntegrationError, match="recursive"):
        ShapeSet(schema).resolve("r")


def guarded(schema, document):
    from pyxsd.integrations._shape import ShapeSet
    from pyxsd.integrations._values import guard_node

    return guard_node(ShapeSet(schema).shallow("r"), document.root, {})


def test_guard_optional_group_activation():
    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence minOccurs="0">
    <xs:element name="a" type="xs:int"/><xs:element name="b" type="xs:int"/>
    </xs:sequence></xs:complexType></xs:element>""")
    guarded(schema, parse(schema, "<r/>"))
    doc = parse(schema, "<r><a>1</a><b>2</b></r>")
    doc.root._children_.pop()
    with pytest.raises(IntegrationError, match="b"):
        guarded(schema, doc)


def test_guard_required_choice():
    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:choice>
    <xs:element name="a" type="xs:int" nillable="true"/><xs:element name="b" type="xs:int"/>
    </xs:choice></xs:complexType></xs:element>""")
    doc = parse(
        schema, '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><a xsi:nil="true"/></r>'
    )
    guarded(schema, doc)
    node = doc.root._children_[0]
    doc.root._children_.clear()
    with pytest.raises(IntegrationError, match="choice"):
        guarded(schema, doc)
    doc.root._children_ = [node, parse(schema, "<r><b>2</b></r>").root._children_[0]]
    with pytest.raises(IntegrationError, match="choice"):
        guarded(schema, doc)


def test_nil_retains_required_attributes():
    schema = compile_schema("""<xs:element name="r" nillable="true"><xs:complexType>
    <xs:attribute name="currency" use="required" fixed="USD"/>
    </xs:complexType></xs:element>""")
    doc = parse(
        schema,
        '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true" currency="USD"/>',
    )
    assert guarded(schema, doc)[1] == {"@currency": "USD"}
    del doc.root._attribs_["currency"]
    with pytest.raises(IntegrationError, match="required attribute"):
        guarded(schema, doc)


def test_selected_lexical_mutation_is_current():
    from pyxsd.integrations._shape import ShapeSet
    from pyxsd.integrations._values import read_scalar

    schema = compile_schema('<xs:element name="r" type="xs:int"/>')
    doc = parse(schema, "<r>1</r>")
    doc.root._value_ = ["42"]
    assert read_scalar(ShapeSet(schema).shallow("r"), doc.root, {}) == 42
    doc.root._value_ = ["bad"]
    with pytest.raises(IntegrationError, match="invalid"):
        read_scalar(ShapeSet(schema).shallow("r"), doc.root, {})


def test_effective_defaults_and_fixed_values():
    from pyxsd.integrations._shape import ShapeSet
    from pyxsd.integrations._values import read_scalar

    schema = compile_schema('<xs:element name="r" type="xs:int" default="7"/>')
    assert read_scalar(ShapeSet(schema).shallow("r"), parse(schema, "<r/>").root, {}) == 7
    schema = compile_schema("""<xs:element name="r"><xs:complexType>
    <xs:attribute name="a" type="xs:int" default="9"/>
    <xs:attribute name="b" type="xs:int" fixed="2"/></xs:complexType></xs:element>""")
    doc = parse(schema, "<r/>")
    assert guarded(schema, doc)[1] == {"@a": 9, "@b": 2}
    doc.root._attribs_["b"] = "3"
    with pytest.raises(IntegrationError, match="fixed"):
        guarded(schema, doc)


def test_qname_binding_rebound():
    from pyxsd.integrations._shape import ShapeSet
    from pyxsd.integrations._values import guard_node, read_scalar

    schema = compile_schema('<xs:element name="r" type="xs:QName"/>')
    doc = parse(schema, '<r xmlns:p="urn:old">p:x</r>')
    shape = ShapeSet(schema).shallow("r")
    bindings = guard_node(shape, doc.root, {})[0]
    assert read_scalar(shape, doc.root, bindings) == "{urn:old}x"
    doc.root._attribs_["xmlns:p"] = "urn:new"
    with pytest.raises(IntegrationError, match="namespace"):
        read_scalar(shape, doc.root, guard_node(shape, doc.root, {})[0])


def test_unselected_sibling_participates_in_counts():
    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="selected" type="xs:int"/><xs:element name="other" type="xs:int"/>
    </xs:sequence></xs:complexType></xs:element>""")
    doc = parse(schema, "<r><selected>1</selected><other>2</other></r>")
    doc.root._children_.pop()
    with pytest.raises(IntegrationError, match="other"):
        guarded(schema, doc)


ORDERS = """<xs:element name="orders"><xs:complexType><xs:sequence>
<xs:element name="order" minOccurs="0" maxOccurs="unbounded"><xs:complexType><xs:sequence>
<xs:element name="customer"><xs:complexType><xs:attribute name="account" use="required"/></xs:complexType></xs:element>
<xs:element name="line" minOccurs="0" maxOccurs="unbounded"><xs:complexType><xs:sequence>
<xs:element name="quantity" type="xs:int"/>
</xs:sequence><xs:attribute name="sku" use="required"/></xs:complexType></xs:element>
</xs:sequence><xs:attribute name="number" use="required"/></xs:complexType></xs:element>
</xs:sequence><xs:attribute name="version" type="xs:int" default="9"/></xs:complexType></xs:element>"""
ORDER_XML = """<orders><order number="A"><customer account="C1"/>
<line sku="X"><quantity>2</quantity></line><line sku="Y"><quantity>3</quantity></line></order>
<order number="B"><customer account="C2"/><line sku="Z"><quantity>4</quantity></line></order></orders>"""


def order_columns():
    from pyxsd.integrations.projection import FieldSource as F

    return {
        "number": F(scope="ancestor", levels=1, attribute="number"),
        "account": F(scope="ancestor", levels=1, path=("customer",), attribute="account"),
        "sku": F(attribute="sku"),
        "quantity": F(path=("quantity",)),
    }


def rows(
    schema,
    document,
    *,
    columns=None,
    element="orders",
    path=("order", "line"),
    selector="order/line",
):
    from pyxsd.integrations._projection import compile_projection, iter_projected_rows
    from pyxsd.integrations._values import prepare_document

    plan = compile_projection(
        schema, element=element, path=path, columns=columns or order_columns()
    )
    return list(
        iter_projected_rows(
            plan, prepare_document(schema, document, False), selector=selector, namespaces=None
        )
    )


def test_contextual_order_associations():
    schema = compile_schema(ORDERS)
    assert rows(schema, parse(schema, ORDER_XML)) == [
        {"number": "A", "account": "C1", "sku": "X", "quantity": 2},
        {"number": "A", "account": "C1", "sku": "Y", "quantity": 3},
        {"number": "B", "account": "C2", "sku": "Z", "quantity": 4},
    ]


def test_root_grandparent_and_copied_columns():
    from pyxsd.integrations._projection import compile_projection, iter_projected_rows
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema(ORDERS)
    columns = {
        "root": F(scope="root", attribute="version"),
        "grandparent": F(scope="ancestor", levels=2, attribute="version"),
    }
    plan = compile_projection(schema, element="orders", path=("order", "line"), columns=columns)
    columns.clear()
    assert (
        list(
            iter_projected_rows(
                plan, parse(schema, ORDER_XML), selector="order/line", namespaces=None
            )
        )
        == [{"root": 9, "grandparent": 9}] * 3
    )
    with pytest.raises(IntegrationError, match="above"):
        compile_projection(
            schema,
            element="orders",
            path=("order", "line"),
            columns={"x": F(scope="ancestor", levels=3)},
        )


@pytest.mark.parametrize(
    "source", ["repeated", "struct", "list", "union", "empty", "name", "value"]
)
def test_preparation_rejects_unsupported_columns(source):
    from pyxsd.integrations._projection import compile_projection
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema(ORDERS)
    columns = {"x": F(scope="root", path=("order",))} if source == "repeated" else {"x": F()}
    element, path = "orders", ("order", "line")
    if source in ("list", "union"):
        body = (
            '<xs:list itemType="xs:int"/>'
            if source == "list"
            else '<xs:union memberTypes="xs:int xs:string"/>'
        )
        schema = compile_schema(
            f'<xs:element name="r"><xs:simpleType>{body}</xs:simpleType></xs:element>'
        )
        element, path = "r", ()
    if source == "empty":
        columns = {}
    if source == "name":
        columns = {"": F()}
    if source == "value":
        columns = {"x": "quantity"}
    with pytest.raises(IntegrationError):
        compile_projection(schema, element=element, path=path, columns=columns)


def test_nullable_choice_optional_defaults_and_simple_content():
    from pyxsd.integrations._projection import compile_projection
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="container" minOccurs="0"><xs:complexType><xs:sequence>
    <xs:element name="value" type="xs:int"/></xs:sequence></xs:complexType></xs:element>
    <xs:element name="defaulted" type="xs:int" default="7" minOccurs="0"/>
    <xs:choice><xs:element name="a" type="xs:int"/><xs:element name="b" type="xs:int"/></xs:choice>
    </xs:sequence></xs:complexType></xs:element>""")
    columns = {
        "value": F(path=("container", "value")),
        "defaulted": F(path=("defaulted",)),
        "a": F(path=("a",)),
    }
    plan = compile_projection(schema, element="r", path=(), columns=columns)
    assert all(c.nullable for c in plan.columns)
    assert rows(
        schema,
        parse(schema, "<r><b>2</b></r>"),
        element="r",
        path=(),
        selector=None,
        columns=columns,
    ) == [{"value": None, "defaulted": None, "a": None}]
    doc = parse(schema, "<r><container><value>3</value></container><defaulted/><a>1</a></r>")
    assert rows(schema, doc, element="r", path=(), selector=None, columns=columns) == [
        {"value": 3, "defaulted": 7, "a": 1}
    ]
    doc.root._children_[0]._children_.clear()
    with pytest.raises(IntegrationError, match="value"):
        rows(schema, doc, element="r", path=(), selector=None, columns=columns)


def test_empty_selection_and_required_route_guards():
    schema = compile_schema(ORDERS)
    assert rows(schema, parse(schema, "<orders/>")) == []
    doc = parse(schema, '<orders><order number="A"><customer account="C1"/></order></orders>')
    doc.root._children_[0]._children_.clear()
    with pytest.raises(IntegrationError, match="customer"):
        rows(schema, doc)
    with pytest.raises(IntegrationError, match="route"):
        rows(schema, parse(schema, ORDER_XML), selector="order/customer")
    with pytest.raises(IntegrationError, match="route"):
        rows(schema, parse(schema, ORDER_XML), selector=None)


def test_duplicate_singleton_cycle_and_multiparent_mutations():
    schema = compile_schema(ORDERS)
    doc = parse(schema, ORDER_XML)
    line = doc.root._children_[0]._children_[1]
    line._children_.append(line._children_[0])
    with pytest.raises(IntegrationError, match="count"):
        rows(schema, doc)
    doc = parse(schema, ORDER_XML)
    doc.root._children_.append(doc.root)
    with pytest.raises(IntegrationError, match="cyclic"):
        rows(schema, doc)
    doc = parse(schema, ORDER_XML)
    doc.root._children_[1]._children_.append(doc.root._children_[0]._children_[1])
    with pytest.raises(IntegrationError, match=r"multiple|multi-parent"):
        rows(schema, doc)


def test_reused_declaration_wrong_route_is_rejected():
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema("""<xs:element name="v" type="xs:int"/>
    <xs:complexType name="T"><xs:sequence><xs:element ref="v"/></xs:sequence></xs:complexType>
    <xs:element name="r"><xs:complexType><xs:sequence><xs:element name="a" type="T"/>
    <xs:element name="b" type="T"/></xs:sequence></xs:complexType></xs:element>""")
    doc = parse(schema, "<r><a><v>1</v></a><b><v>1</v></b></r>")
    assert rows(schema, doc, element="r", path=("a", "v"), selector="a/v", columns={"v": F()}) == [
        {"v": 1}
    ]
    with pytest.raises(IntegrationError, match="route"):
        rows(schema, doc, element="r", path=("a", "v"), selector="b/v", columns={"v": F()})


def test_unselected_deep_type_and_validation_boundary():
    from pyxsd import ValidationError
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="v" type="xs:int"/><xs:element name="deep"><xs:complexType><xs:sequence>
    <xs:element name="x"><xs:simpleType><xs:union memberTypes="xs:int xs:boolean"/></xs:simpleType></xs:element>
    </xs:sequence></xs:complexType></xs:element></xs:sequence></xs:complexType></xs:element>""")
    doc = parse(schema, "<r><v>2</v><deep><x>1</x></deep></r>")
    doc.root._children_[1]._children_[0]._value_ = ["invalid"]
    assert rows(
        schema, doc, element="r", path=(), selector=None, columns={"v": F(path=("v",))}
    ) == [{"v": 2}]
    with pytest.raises(ValidationError):
        doc.revalidate().require_valid()
    doc.root._children_.pop()
    with pytest.raises(IntegrationError, match="deep"):
        rows(schema, doc, element="r", path=(), selector=None, columns={"v": F(path=("v",))})


def test_namespace_distinct_routes_and_ancestor_qname_bindings():
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema(
        """<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="v" type="xs:QName"/><xs:element name="v" form="unqualified" type="xs:QName"/>
    </xs:sequence></xs:complexType></xs:element>""",
        namespace="urn:t",
    )
    doc = parse(schema, '<t:r xmlns:t="urn:t" xmlns:p="urn:a"><t:v>p:x</t:v><v>p:y</v></t:r>')
    columns = {"q": F(path=("{urn:t}v",)), "n": F(path=("v",))}
    assert rows(schema, doc, element="{urn:t}r", path=(), selector=None, columns=columns) == [
        {"q": "{urn:a}x", "n": "{urn:a}y"}
    ]
    doc.root._attribs_["xmlns:p"] = "urn:b"
    with pytest.raises(IntegrationError, match="namespace"):
        rows(schema, doc, element="{urn:t}r", path=(), selector=None, columns=columns)


@pytest.mark.parametrize("attribute", [False, True])
def test_empty_ancestor_qname_override_cannot_reuse_cached_identity(attribute):
    from pyxsd.integrations.projection import FieldSource as F

    body = (
        '<xs:complexType><xs:attribute name="q" type="xs:QName"/></xs:complexType>'
        if attribute
        else '<xs:simpleType><xs:restriction base="xs:QName"/></xs:simpleType>'
    )
    schema = compile_schema(
        f'<xs:element name="r"><xs:complexType><xs:sequence><xs:element name="v">{body}</xs:element></xs:sequence></xs:complexType></xs:element>'
    )
    xml = (
        '<r xmlns:p="urn:q"><v q="p:x"/></r>' if attribute else '<r xmlns:p="urn:q"><v>p:x</v></r>'
    )
    doc = parse(schema, xml)
    columns = {"q": F(path=("v",), attribute="q" if attribute else None)}
    assert rows(schema, doc, element="r", path=(), selector=None, columns=columns) == [
        {"q": "{urn:q}x"}
    ]
    doc.root._attribs_["xmlns:p"] = ""
    with pytest.raises(IntegrationError, match="namespace"):
        rows(schema, doc, element="r", path=(), selector=None, columns=columns)


def test_selected_structural_failure_has_row_and_column_context():
    schema = compile_schema(ORDERS)
    doc = parse(schema, ORDER_XML)
    del doc.root._children_[0]._children_[2]._attribs_["sku"]
    with pytest.raises(IntegrationError, match=r"row 2.*column 'sku'.*required attribute") as error:
        rows(schema, doc)
    assert error.value.__cause__ is not None


def test_unsupported_row_endpoint_has_preparation_column_context():
    from pyxsd.integrations._projection import compile_projection
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema(
        '<xs:element name="r"><xs:simpleType><xs:union memberTypes="xs:int xs:string"/></xs:simpleType></xs:element>'
    )
    with pytest.raises(IntegrationError, match=r"column 'v'.*source.*union") as error:
        compile_projection(schema, element="r", path=(), columns={"v": F()})
    assert error.value.__cause__ is not None


def test_whole_record_local_qname_keeps_existing_namespace_evidence_policy():
    from pyxsd.integrations._shape import ShapeSet
    from pyxsd.integrations._values import project

    schema = compile_schema(
        '<xs:element name="r"><xs:complexType><xs:sequence><xs:element name="v" type="xs:QName"/></xs:sequence></xs:complexType></xs:element>'
    )
    doc = parse(schema, '<r xmlns:p="urn:q"><v>p:x</v></r>')
    assert project(ShapeSet(schema).resolve("r", ("v",)), doc.root._children_[0]) == "{urn:q}x"


def test_nil_container_suppresses_column_descent_but_preserves_attributes():
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="payload" nillable="true"><xs:complexType><xs:sequence>
    <xs:element name="v" type="xs:int"/></xs:sequence>
    <xs:attribute name="currency" use="required"/></xs:complexType></xs:element>
    </xs:sequence></xs:complexType></xs:element>""")
    doc = parse(
        schema,
        '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><payload xsi:nil="true" currency="USD"/></r>',
    )
    columns = {
        "v": F(path=("payload", "v")),
        "currency": F(path=("payload",), attribute="currency"),
    }
    assert rows(schema, doc, element="r", path=(), selector=None, columns=columns) == [
        {"v": None, "currency": "USD"}
    ]
    del doc.root._children_[0]._attribs_["currency"]
    with pytest.raises(IntegrationError, match="required attribute"):
        rows(schema, doc, element="r", path=(), selector=None, columns=columns)


@pytest.mark.parametrize("lexical", ["false", "invalid"])
def test_current_nil_attribute_cannot_disagree_with_bound_nil_state(lexical):
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema("""<xs:element name="r" nillable="true"><xs:complexType><xs:simpleContent>
    <xs:extension base="xs:string"/></xs:simpleContent></xs:complexType></xs:element>""")
    doc = parse(schema, '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>')
    doc.root._attribs_["xsi:nil"] = lexical
    with pytest.raises(IntegrationError, match=r"row 1 column 'v'.*nil"):
        rows(schema, doc, element="r", path=(), selector=None, columns={"v": F()})


def test_nil_primitive_document_root_projects_null():
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema('<xs:element name="r" type="xs:int" nillable="true"/>')
    doc = parse(schema, '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>')
    assert rows(schema, doc, element="r", path=(), selector=None, columns={"v": F()}) == [
        {"v": None}
    ]


def test_current_nil_marker_requires_nillable_declaration_even_when_false():
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema('<xs:element name="r" type="xs:int"/>')
    doc = parse(schema, "<r>5</r>")
    doc.root._attribs_["xsi:nil"] = "false"
    with pytest.raises(IntegrationError, match="nillable"):
        rows(schema, doc, element="r", path=(), selector=None, columns={"v": F()})


def test_repeated_ancestors_keep_independent_parsed_qname_identities():
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="order" maxOccurs="unbounded"><xs:complexType><xs:sequence>
    <xs:element name="line" type="xs:int" maxOccurs="unbounded"/>
    </xs:sequence><xs:attribute name="q" type="xs:QName"/></xs:complexType></xs:element>
    </xs:sequence></xs:complexType></xs:element>""")
    doc = parse(
        schema,
        '<r><order xmlns:p="urn:a" q="p:x"><line>1</line><line>2</line></order><order xmlns:p="urn:b" q="p:x"><line>3</line></order></r>',
    )
    columns = {"q": F(scope="ancestor", levels=1, attribute="q"), "v": F()}
    assert rows(
        schema, doc, element="r", path=("order", "line"), selector="order/line", columns=columns
    ) == [{"q": "{urn:a}x", "v": 1}, {"q": "{urn:a}x", "v": 2}, {"q": "{urn:b}x", "v": 3}]
    doc.root._children_[1]._attribs_["xmlns:p"] = "urn:a"
    with pytest.raises(IntegrationError, match="namespace"):
        rows(
            schema, doc, element="r", path=("order", "line"), selector="order/line", columns=columns
        )


def test_unselected_recursive_type_does_not_require_recursive_projection():
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema("""<xs:complexType name="Branch"><xs:sequence>
    <xs:element name="nested" type="Branch" minOccurs="0"/>
    </xs:sequence></xs:complexType><xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="v" type="xs:int"/><xs:element name="tree" type="Branch"/>
    </xs:sequence></xs:complexType></xs:element>""")
    doc = parse(schema, "<r><v>7</v><tree><nested><nested/></nested></tree></r>")
    assert rows(
        schema, doc, element="r", path=(), selector=None, columns={"v": F(path=("v",))}
    ) == [{"v": 7}]


@pytest.mark.parametrize("count", [100, 200, 400])
def test_shared_root_guard_child_visits_are_linear(monkeypatch, count):
    from pyxsd.integrations import _projection
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="v" type="xs:int" maxOccurs="unbounded"/>
    </xs:sequence><xs:attribute name="label"/></xs:complexType></xs:element>""")
    doc = parse(schema, '<r label="L">' + "<v>1</v>" * count + "</r>")
    original = _projection.guard_node
    visits = []

    def measured(shape, node, bindings, **kwargs):
        if node is doc.root:
            visits.append(len(node._children_))
        return original(shape, node, bindings, **kwargs)

    monkeypatch.setattr(_projection, "guard_node", measured)
    result = rows(
        schema,
        doc,
        element="r",
        path=("v",),
        selector="v",
        columns={"label": F(scope="root", attribute="label"), "value": F()},
    )
    assert result == [{"label": "L", "value": 1}] * count
    assert visits == [count]  # One real guard, not a row-dependent rescan.


@pytest.mark.parametrize("count", [50, 100])
def test_shared_order_and_branch_guards_run_once_per_occurrence(monkeypatch, count):
    from pyxsd.integrations import _projection
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema(ORDERS)
    xml = (
        "<orders>"
        + "".join(
            f'<order number="{number}"><customer account="{account}"/>'
            + '<line sku="X"><quantity>2</quantity></line>' * count
            + "</order>"
            for number, account in [("A", "C1"), ("B", "C2")]
        )
        + "</orders>"
    )
    doc = parse(schema, xml)
    original = _projection.guard_node
    calls = {}
    visits = {}

    def measured(shape, node, bindings, **kwargs):
        key = id(node)
        calls[key] = calls.get(key, 0) + 1
        visits[key] = visits.get(key, 0) + len(node._children_)
        return original(shape, node, bindings, **kwargs)

    monkeypatch.setattr(_projection, "guard_node", measured)
    columns = order_columns()
    columns.update(
        {
            "number_again": columns["number"],
            "account_again": columns["account"],
            "version": F(scope="root", attribute="version"),
        }
    )
    result = rows(schema, doc, columns=columns)
    assert [
        (r["number"], r["account"], r["number_again"], r["account_again"], r["version"])
        for r in result
    ] == [("A", "C1", "A", "C1", 9)] * count + [("B", "C2", "B", "C2", 9)] * count
    assert calls[id(doc.root)] == 1
    for order in doc.root._children_:
        assert calls[id(order)] == 1
        assert visits[id(order)] == count + 1
        assert calls[id(order._children_[0])] == 1  # Off-row customer branch.
        for line in order._children_[1:]:
            assert calls[id(line)] == 1


def test_frame_state_is_independent_across_interleaved_extractions_and_mutations():
    from pyxsd.integrations._projection import compile_projection, iter_projected_rows
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema(ORDERS)
    columns = {**order_columns(), "version": F(scope="root", attribute="version")}
    plan = compile_projection(schema, element="orders", path=("order", "line"), columns=columns)
    first = parse(schema, ORDER_XML)
    second = parse(
        schema,
        ORDER_XML.replace('number="A"', 'number="D"').replace('account="C1"', 'account="C9"'),
    )
    first.root._attribs_["version"] = "1"
    second.root._attribs_["version"] = "2"
    left = iter_projected_rows(plan, first, selector="order/line", namespaces=None)
    right = iter_projected_rows(plan, second, selector="order/line", namespaces=None)
    assert (next(left)["number"], next(right)["number"]) == ("A", "D")
    assert next(left)["version"] == 1
    assert next(right)["version"] == 2
    assert next(left)["account"] == "C2"
    assert next(right)["account"] == "C2"
    assert list(left) == list(right) == []

    first.root._attribs_["version"] = "3"
    first.root._children_[0]._children_[0]._attribs_["account"] = "C3"
    result = list(iter_projected_rows(plan, first, selector="order/line", namespaces=None))
    assert [(r["version"], r["account"]) for r in result] == [(3, "C3"), (3, "C3"), (3, "C2")]
    first.root._children_[0]._children_[0]._attribs_["account"] = "C4"
    fresh = first.revalidate()
    assert fresh.root is not first.root
    result = list(iter_projected_rows(plan, fresh, selector="order/line", namespaces=None))
    assert result[0]["account"] == "C4"
    first.root._children_[0]._children_[0]._attribs_.pop("account")
    with pytest.raises(IntegrationError, match="required attribute"):
        list(iter_projected_rows(plan, first, selector="order/line", namespaces=None))


def test_branch_frames_keep_occurrence_route_and_namespace_context():
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="order" maxOccurs="unbounded"><xs:complexType><xs:sequence>
    <xs:element name="left" type="xs:QName"/><xs:element name="right" type="xs:QName"/>
    <xs:element name="line" type="xs:int" maxOccurs="unbounded"/>
    </xs:sequence></xs:complexType></xs:element></xs:sequence></xs:complexType></xs:element>""")
    doc = parse(
        schema,
        '<r><order><left xmlns:p="urn:a">p:x</left><right xmlns:p="urn:b">p:x</right><line>1</line><line>2</line></order><order><left xmlns:p="urn:c">p:x</left><right xmlns:p="urn:d">p:x</right><line>3</line></order></r>',
    )
    for order, uris in zip(
        doc.root._children_, [("urn:a", "urn:b"), ("urn:c", "urn:d")], strict=True
    ):
        order._attribs_["xmlns:p"] = "urn:outer"
        for node, uri in zip(order._children_[:2], uris, strict=True):
            node._attribs_["xmlns:p"] = uri
    columns = {
        "left": F(scope="ancestor", levels=1, path=("left",)),
        "right": F(scope="ancestor", levels=1, path=("right",)),
        "again": F(scope="ancestor", levels=1, path=("left",)),
        "v": F(),
    }
    assert rows(
        schema, doc, element="r", path=("order", "line"), selector="order/line", columns=columns
    ) == [
        {"left": "{urn:a}x", "right": "{urn:b}x", "again": "{urn:a}x", "v": 1},
        {"left": "{urn:a}x", "right": "{urn:b}x", "again": "{urn:a}x", "v": 2},
        {"left": "{urn:c}x", "right": "{urn:d}x", "again": "{urn:c}x", "v": 3},
    ]
    doc.root._children_[0]._children_[1]._attribs_["xmlns:p"] = "urn:changed"
    with pytest.raises(IntegrationError, match="namespace"):
        rows(
            schema, doc, element="r", path=("order", "line"), selector="order/line", columns=columns
        )


def test_root_column_route_reuses_its_active_singleton_ancestor_frame(monkeypatch):
    from pyxsd.integrations import _projection
    from pyxsd.integrations.projection import FieldSource as F

    schema = compile_schema("""<xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="group"><xs:complexType><xs:sequence><xs:element name="v" type="xs:int" maxOccurs="unbounded"/>
    </xs:sequence><xs:attribute name="label"/></xs:complexType></xs:element>
    </xs:sequence></xs:complexType></xs:element>""")
    doc = parse(schema, '<r><group label="L">' + "<v>1</v>" * 100 + "</group></r>")
    group = doc.root._children_[0]
    original = _projection.guard_node
    calls = []

    def measured(shape, node, bindings, **kwargs):
        if node is group:
            calls.append(len(node._children_))
        return original(shape, node, bindings, **kwargs)

    monkeypatch.setattr(_projection, "guard_node", measured)
    columns = {
        "root_label": F(scope="root", path=("group",), attribute="label"),
        "ancestor_label": F(scope="ancestor", levels=1, attribute="label"),
        "v": F(),
    }
    assert (
        rows(schema, doc, element="r", path=("group", "v"), selector="group/v", columns=columns)
        == [{"root_label": "L", "ancestor_label": "L", "v": 1}] * 100
    )
    assert calls == [100]


def test_repeated_row_frames_are_released_and_close_releases_ancestor_frames(monkeypatch):
    import weakref

    from pyxsd.integrations import _projection

    schema = compile_schema(ORDERS)
    doc = parse(schema, ORDER_XML)
    plan = _projection.compile_projection(
        schema, element="orders", path=("order", "line"), columns=order_columns()
    )
    original = _projection._Frame.validated
    ancestors, row_frames = [], []

    def measured(frame):
        if frame._state is None:
            refs = row_frames if frame.shape is plan.row_shapes[-1] else ancestors
            refs.append(weakref.ref(frame))
            if refs is row_frames:
                assert sum(ref() is not None for ref in row_frames) <= 1
        return original(frame)

    monkeypatch.setattr(_projection._Frame, "validated", measured)
    iterator = _projection.iter_projected_rows(plan, doc, selector="order/line", namespaces=None)
    assert next(iterator)["sku"] == "X"
    assert any(ref() is not None for ref in ancestors)
    assert next(iterator)["sku"] == "Y"
    iterator.close()
    assert all(ref() is None for ref in [*ancestors, *row_frames])
    assert list(
        _projection.iter_projected_rows(plan, doc, selector="order/line", namespaces=None)
    ) == [
        {"number": "A", "account": "C1", "sku": "X", "quantity": 2},
        {"number": "A", "account": "C1", "sku": "Y", "quantity": 3},
        {"number": "B", "account": "C2", "sku": "Z", "quantity": 4},
    ]
    assert all(ref() is None for ref in [*ancestors, *row_frames])
