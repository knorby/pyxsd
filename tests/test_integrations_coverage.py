"""Coverage-focused tests for the optional-integration adapters.

Targets the residual value-coercion, shape, projection, Pydantic and Arrow
paths across ``pyxsd.integrations``. Every test asserts observable behavior:
converted values, projected shapes, or raised error types. Tests needing an
optional extra skip per-test where that extra is absent.
"""

from decimal import Decimal

import pytest

from integration_helpers import RECORD_SCHEMA, compile_schema, parse
from pyxsd import xsd_data_types as xd
from pyxsd.integrations import IntegrationError
from pyxsd.integrations._projection import compile_projection, iter_projected_rows
from pyxsd.integrations._shape import ShapeSet, resolve_route, scalar_kind
from pyxsd.integrations._values import (
    _project,
    check_counts,
    check_shell,
    lexical_value,
    nil_state,
    plain_value,
    prepare_document,
    read_attributes,
    value_equal,
)
from pyxsd.integrations.projection import FieldSource


@pytest.fixture
def pydantic():
    return pytest.importorskip("pydantic")


@pytest.fixture
def pa():
    return pytest.importorskip("pyarrow")


def models(schema):
    from pyxsd.integrations.pydantic import models as build

    return build(schema)


def records(schema, **kwargs):
    from pyxsd.integrations.arrow import records as build

    return build(schema, **kwargs)


# --- integrations/_values.py -------------------------------------------------


def test_prepare_document_rejects_unbound_root_and_foreign_schema():
    schema = compile_schema('<xs:element name="r" type="xs:int"/>')
    document = parse(schema, "<r>1</r>")
    document.root = None
    with pytest.raises(IntegrationError, match="no bound root"):
        prepare_document(schema, document, False)

    foreign = parse(compile_schema('<xs:element name="r" type="xs:int"/>'), "<r>1</r>")
    with pytest.raises(IntegrationError, match="another compiled schema"):
        prepare_document(schema, foreign, False)


def test_plain_value_rejects_unresolved_qname_context():
    with pytest.raises(IntegrationError, match="trustworthy namespace context"):
        plain_value(xd.QName, xd.QName("local"))


def test_lexical_value_requires_current_binding_for_qname_prefix():
    schema = compile_schema('<xs:element name="r" type="xs:QName"/>')
    declaration = resolve_route(schema, "r").declarations[0]
    with pytest.raises(IntegrationError, match="no current namespace binding"):
        lexical_value(xd.QName, "zz:x", declaration=declaration)


def test_value_equal_uses_native_value_space_keys():
    assert value_equal(xd.NMTOKENS, ["a", "b"], ["a", "b"])
    assert not value_equal(xd.NMTOKENS, ["a"], ["a", "b"])
    assert value_equal(xd.Base64Binary, b"\x00", b"\x00")
    assert value_equal(xd.Boolean, True, "true")
    assert not value_equal(xd.Boolean, True, False)
    assert value_equal(xd.Decimal, Decimal("1.0"), Decimal("1.00"))


def test_check_counts_reports_prohibited_group_with_members():
    from pyxsd.content_model import Particle

    schema = compile_schema(
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="a" type="xs:int"/></xs:sequence></xs:complexType></xs:element>'
    )
    shape = ShapeSet(schema).shallow("r")
    element = shape.particle.children[0]
    # Schema legality forbids authoring a maxOccurs="0" model group directly,
    # so exercise the compiled representation the guard sees.
    shape.particle = Particle("sequence", 0, 0, [element])
    with pytest.raises(IntegrationError, match="prohibited group contains elements"):
        check_counts(shape, {id(element.descriptor): 1})


def test_project_rejects_node_that_is_already_active():
    schema = compile_schema('<xs:element name="r" type="xs:int"/>')
    document = parse(schema, "<r>1</r>")
    shape = ShapeSet(schema).shallow("r")
    with pytest.raises(IntegrationError, match="cyclic bound input"):
        _project(shape, document.root, False, {id(document.root)}, {})


def test_nil_state_rejects_duplicate_current_markers():
    schema = compile_schema('<xs:element name="r" type="xs:int"/>')
    document = parse(schema, "<r>1</r>")
    document.root._attribs_ = {
        "xsi:nil": "true",
        "{http://www.w3.org/2001/XMLSchema-instance}nil": "true",
    }
    with pytest.raises(IntegrationError, match="multiple xsi:nil"):
        nil_state(document.root, current=True)


def test_check_shell_rejects_runtime_polymorphism():
    schema = compile_schema(
        '<xs:complexType name="Base"><xs:sequence><xs:element name="v" type="xs:int"/>'
        "</xs:sequence></xs:complexType>"
        '<xs:complexType name="Derived"><xs:complexContent><xs:extension base="Base">'
        '<xs:attribute name="x" type="xs:int"/></xs:extension></xs:complexContent></xs:complexType>'
        '<xs:element name="r" type="Base"/>'
    )
    document = parse(
        schema,
        '<r xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="Derived"><v>1</v></r>',
    )
    with pytest.raises(IntegrationError, match="polymorphism is unsupported"):
        check_shell(ShapeSet(schema).shallow("r"), document.root, {})


def test_check_shell_rejects_children_on_simple_content():
    schema = compile_schema(
        '<xs:element name="r"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="xs:int"/></xs:simpleContent></xs:complexType></xs:element>'
    )
    document = parse(schema, "<r>5</r>")
    document.root._children_.append(document.root)
    with pytest.raises(IntegrationError, match="scalar/simple content contains children"):
        check_shell(ShapeSet(schema).shallow("r"), document.root, {})


def test_read_attributes_skips_absent_optional_attribute_without_default():
    schema = compile_schema(
        '<xs:element name="r"><xs:complexType><xs:attribute name="opt" type="xs:int"/>'
        '<xs:attribute name="req" type="xs:int" use="required"/></xs:complexType></xs:element>'
    )
    document = parse(schema, '<r req="1"/>')
    shape = ShapeSet(schema).shallow("r")
    assert read_attributes(shape, document.root, {}) == {"@req": 1}


# --- integrations/_shape.py --------------------------------------------------


@pytest.mark.parametrize("cls", [xd.AnyType, xd.AnyAtomicType, int])
def test_scalar_kind_rejects_unsupported_types(cls):
    with pytest.raises(IntegrationError, match="unsupported scalar type"):
        scalar_kind(cls)


def test_shape_set_rejects_ambiguous_declaration_positions():
    schema = compile_schema(
        '<xs:element name="v" type="xs:int"/><xs:element name="r"><xs:complexType>'
        '<xs:sequence><xs:element ref="v"/><xs:element ref="v"/></xs:sequence>'
        "</xs:complexType></xs:element>"
    )
    with pytest.raises(IntegrationError, match="ambiguous declaration positions for v"):
        ShapeSet(schema).resolve("r")


# --- integrations/_projection.py ---------------------------------------------


ORDERS = """<xs:element name="orders"><xs:complexType><xs:sequence>
<xs:element name="order" minOccurs="0" maxOccurs="unbounded"><xs:complexType><xs:sequence>
<xs:element name="line" minOccurs="0" maxOccurs="unbounded"><xs:complexType><xs:sequence>
<xs:element name="quantity" type="xs:int"/>
</xs:sequence><xs:attribute name="sku" use="required"/></xs:complexType></xs:element>
</xs:sequence><xs:attribute name="number" use="required"/></xs:complexType></xs:element>
</xs:sequence></xs:complexType></xs:element>"""

ORDER_XML = """<orders><order number="A"><line sku="X"><quantity>2</quantity></line>
<line sku="Y"><quantity>3</quantity></line></order>
<order number="B"><line sku="Z"><quantity>4</quantity></line></order></orders>"""

ANCESTOR_COLUMNS = {"number": FieldSource(scope="ancestor", levels=1, attribute="number")}


def test_compile_projection_wraps_route_resolution_failure():
    schema = compile_schema(ORDERS)
    with pytest.raises(IntegrationError, match=r"column 'x'.*missing: global element"):
        compile_projection(schema, element="missing", path=(), columns={"x": FieldSource()})


def test_compile_projection_rejects_missing_attribute_endpoint():
    schema = compile_schema(ORDERS)
    with pytest.raises(IntegrationError, match="attribute endpoint is missing or prohibited"):
        compile_projection(
            schema,
            element="orders",
            path=("order", "line"),
            columns={"x": FieldSource(attribute="absent")},
        )


def test_iter_projected_rows_rejects_foreign_document():
    schema = compile_schema(ORDERS)
    plan = compile_projection(
        schema, element="orders", path=("order", "line"), columns=ANCESTOR_COLUMNS
    )
    foreign = parse(compile_schema(ORDERS), ORDER_XML)
    with pytest.raises(IntegrationError, match="another compiled schema"):
        list(iter_projected_rows(plan, foreign, selector="order/line", namespaces=None))


def test_purely_ancestor_columns_report_structural_route_failure():
    schema = compile_schema(ORDERS)
    document = parse(schema, ORDER_XML)
    del document.root._children_[0]._children_[0]._attribs_["sku"]
    plan = compile_projection(
        schema, element="orders", path=("order", "line"), columns=ANCESTOR_COLUMNS
    )
    with pytest.raises(IntegrationError, match=r"row 1 structural route .*required attribute"):
        list(iter_projected_rows(plan, document, selector="order/line", namespaces=None))


def test_iter_projected_rows_rejects_mismatched_document_root():
    schema = compile_schema(ORDERS + '<xs:element name="other" type="xs:string"/>')
    plan = compile_projection(
        schema, element="orders", path=("order", "line"), columns=ANCESTOR_COLUMNS
    )
    document = parse(schema, "<other>1</other>")
    with pytest.raises(IntegrationError, match="document root does not match configured route"):
        list(iter_projected_rows(plan, document, selector=None, namespaces=None))


# --- integrations/pydantic.py ------------------------------------------------


def test_qname_local_input_and_pydantic_model_input(pydantic):
    from pydantic import BaseModel

    schema = compile_schema(
        '<xs:element name="r"><xs:complexType>'
        '<xs:attribute name="kind" type="xs:QName"/></xs:complexType></xs:element>'
    )
    root = models(schema).model_for(element="r")
    assert root.model_validate({"@kind": "localname"}).attr_kind == "localname"

    class Payload(BaseModel):
        count: int

    record = models(compile_schema(RECORD_SCHEMA)).model_for(element="root")
    snapshot = record.model_validate(Payload(count=5))
    assert snapshot.count == 5


def test_assertion_facet_numeric_type_requires_xml_evidence(pydantic):
    schema = compile_schema(
        '<xs:element name="r"><xs:simpleType><xs:restriction base="xs:int">'
        '<xs:assertion test="true()"/></xs:restriction></xs:simpleType></xs:element>'
    )
    with pytest.raises(IntegrationError, match="assertion facets need XML evidence"):
        models(schema).model_for(element="r")


def test_nil_fixed_value_and_non_boolean_nil_inputs(pydantic):
    fixed = compile_schema(
        '<xs:element name="r" nillable="true" fixed="2"><xs:complexType><xs:simpleContent>'
        '<xs:extension base="xs:int"><xs:attribute name="a" type="xs:int"/>'
        "</xs:extension></xs:simpleContent></xs:complexType></xs:element>"
    )
    root = models(fixed).model_for(element="r")
    with pytest.raises(pydantic.ValidationError, match="nil element has a fixed value"):
        root.model_validate({"$nil": True})

    record = models(compile_schema(RECORD_SCHEMA)).model_for(element="root")
    with pytest.raises(pydantic.ValidationError, match="nil state must be a boolean"):
        record.model_validate({"count": 1, "missing": {"$nil": "yes", "@reason": "x"}})


def test_nillable_primitive_root_null_and_fixed_validation(pydantic):
    nillable = compile_schema('<xs:element name="r" type="xs:int" nillable="true"/>')
    root = models(nillable).model_for(element="r")
    assert root.model_validate(None).root is None

    fixed = compile_schema('<xs:element name="r" type="xs:int" nillable="true" fixed="2"/>')
    fixed_root = models(fixed).model_for(element="r")
    assert fixed_root.model_validate(2).root == 2
    with pytest.raises(pydantic.ValidationError, match="fixed element value violated"):
        fixed_root.model_validate(3)


def test_prohibited_attribute_is_omitted_from_model_fields(pydantic):
    schema = compile_schema(
        '<xs:element name="r"><xs:complexType>'
        '<xs:attribute name="unused" type="xs:int" use="prohibited"/>'
        '<xs:attribute name="ok" type="xs:int"/></xs:complexType></xs:element>'
    )
    root = models(schema).model_for(element="r")
    assert list(root.model_fields) == ["attr_ok"]
    assert root.model_validate({"@ok": 1}).model_dump(by_alias=True, exclude_unset=True) == {
        "@ok": 1
    }
    with pytest.raises(pydantic.ValidationError):
        root.model_validate({"@unused": 1})


# --- integrations/arrow.py ---------------------------------------------------


@pytest.mark.parametrize(
    "restriction, expected_type, expected_value",
    [
        (
            '<xs:restriction base="xs:byte"><xs:maxInclusive value="100"/></xs:restriction>',
            "int8",
            1,
        ),
        (
            '<xs:restriction base="xs:byte"><xs:minInclusive value="-100"/></xs:restriction>',
            "int8",
            1,
        ),
        (
            '<xs:restriction base="xs:integer"><xs:minInclusive value="-9999999999999999999999"/>'
            '<xs:maxInclusive value="9999999999999999999999"/></xs:restriction>',
            "string",
            "1",
        ),
    ],
)
def test_integer_restriction_selects_width_or_falls_back_to_string(
    pa, restriction, expected_type, expected_value
):
    schema = compile_schema(
        f'<xs:element name="root"><xs:simpleType>{restriction}</xs:simpleType></xs:element>'
    )
    projection = records(schema, element="root")
    assert str(projection.schema.field("value").type) == expected_type
    assert projection.table(parse(schema, "<root>1</root>")).to_pylist() == [
        {"value": expected_value}
    ]


def test_prohibited_attribute_is_omitted_from_struct_records(pa):
    schema = compile_schema(
        '<xs:element name="root"><xs:complexType><xs:sequence><xs:element name="v" type="xs:int"/>'
        "</xs:sequence>"
        '<xs:attribute name="unused" type="xs:int" use="prohibited"/>'
        '<xs:attribute name="ok" type="xs:int"/></xs:complexType></xs:element>'
    )
    projection = records(schema, element="root")
    assert projection.schema.names == ["@ok", "v"]
    document = parse(schema, '<root ok="3"><v>1</v></root>')
    assert projection.table(document).to_pylist() == [{"@ok": 3, "v": 1}]


def test_write_parquet_rejects_invalid_batch_size(pa, tmp_path):
    schema = compile_schema(RECORD_SCHEMA)
    projection = records(schema, element="root")
    document = parse(schema, "<root><count>7</count></root>")
    target = tmp_path / "invalid.parquet"
    with pytest.raises(IntegrationError, match="batch_size must be a positive integer"):
        projection.write_parquet(document, target, batch_size=0)
    assert not target.exists()


# --- integrations/_dataset.py ------------------------------------------------


def test_publish_rejects_existing_destination_and_non_posix_fallback(pa, tmp_path, monkeypatch):
    import sys

    from pyxsd.integrations._dataset import publish

    staging = tmp_path / "staging"
    staging.mkdir()
    existing = tmp_path / "out"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="destination already exists"):
        publish(staging, existing)

    fallback = tmp_path / "staging2"
    fallback.mkdir()
    (fallback / "manifest.json").write_text("complete")
    target = tmp_path / "out2"
    monkeypatch.setattr(sys, "platform", "win32")
    publish(fallback, target)
    assert not fallback.exists()
    assert (target / "manifest.json").read_text() == "complete"
