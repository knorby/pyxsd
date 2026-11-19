"""RELAX NG record column compilation and fixed Arrow schemas; rows are Task 6."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, dataclass
from types import SimpleNamespace

import pytest

pa = pytest.importorskip("pyarrow")
from integration_helpers import compile_schema, parse  # noqa: E402
from pyxsd.facets import FacetConstraints  # noqa: E402
from pyxsd.integrations import IntegrationError  # noqa: E402
from pyxsd.integrations._projection import compile_projection  # noqa: E402
from pyxsd.integrations.arrow import RecordProjection  # noqa: E402
from pyxsd.integrations.arrow import records as arrow_records  # noqa: E402
from pyxsd.integrations.projection import (  # noqa: E402
    FieldSource,
    field_source_metadata,
)
from pyxsd.relaxng.contracts import ScalarContract, candidate_id  # noqa: E402
from pyxsd.relaxng.datatypes import (  # noqa: E402
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    ScalarDatatype,
)
from pyxsd.relaxng.names import AnyName, Name  # noqa: E402
from pyxsd.relaxng.patterns import Data  # noqa: E402
from pyxsd.relaxng.projection import Cardinality, ContentContract  # noqa: E402
from pyxsd.relaxng.records import (  # noqa: E402
    RelaxNGRecordProjection,
    _atomic_arrow,
    _check_default_route,
    _child_step,
    _content_cannot_match,
    _content_may_be_empty,
    _default_record_fields,
    _resolve_row,
    _select_contract,
    _source_endpoint,
    _start_route,
    compile_relaxng_projection,
)
from pyxsd.relaxng.schema import RelaxNGSchema  # noqa: E402

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
INT = (XSD, "int")


def compile_element(
    body: str,
    *,
    name: str = "doc",
    ns: str | None = None,
    datatype_libraries: Mapping[str, DatatypeLibraryRegistration] | None = None,
) -> RelaxNGSchema:
    namespace = f' ns="{ns}"' if ns else ""
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="{name}"{namespace} '
            f'datatypeLibrary="{XSD}">{body}</element>'
        ),
        datatype_libraries=datatype_libraries,
    )
    schema.require_valid()
    return schema


def compile_grammar(body: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(io.StringIO(f'<grammar xmlns="{NS}">{body}</grammar>'))
    schema.require_valid()
    return schema


def rows_schema() -> RelaxNGSchema:
    return compile_grammar(
        f"""
        <start>
          <element name="orders" datatypeLibrary="{XSD}">
            <attribute name="version"><data type="int"/></attribute>
            <element name="header"><element name="code"><text/></element></element>
            <zeroOrMore>
              <element name="order">
                <attribute name="number"><text/></attribute>
                <element name="customer"><attribute name="account"><text/></attribute></element>
                <zeroOrMore>
                  <element name="line">
                    <attribute name="sku"><text/></attribute>
                    <element name="quantity"><data type="int"/></element>
                  </element>
                </zeroOrMore>
              </element>
            </zeroOrMore>
          </element>
        </start>
        """
    )


def row_columns() -> dict[str, FieldSource]:
    return {
        "quantity": FieldSource(path=("quantity",)),
        "sku": FieldSource(attribute="sku"),
        "number": FieldSource(scope="ancestor", levels=1, attribute="number"),
        "version": FieldSource(scope="root", attribute="version"),
        "code": FieldSource(scope="root", path=("header", "code")),
    }


def bind(schema: RelaxNGSchema, column: FieldSource, *, path=("order", "line")):
    plan = compile_relaxng_projection(
        schema, element="orders", path=path, columns={"column": column}
    )
    return plan.columns[0]


def message(error: IntegrationError) -> str:
    return str(error)


def test_field_source_selectors_are_optional_exclusive_and_frozen():
    plain = FieldSource()
    assert plain.datatype is None
    assert plain.candidate_id is None
    assert field_source_metadata(plain) == {
        "scope": "record",
        "path": (),
        "attribute": None,
        "levels": 0,
    }
    assert list(field_source_metadata(plain)) == ["scope", "path", "attribute", "levels"]
    datatype = field_source_metadata(FieldSource(datatype=("", "int")))
    assert datatype["datatype"] == ("", "int")
    assert "candidate_id" not in datatype
    candidate = field_source_metadata(FieldSource(candidate_id="candidate"))
    assert candidate["candidate_id"] == "candidate"
    assert "datatype" not in candidate
    with pytest.raises(FrozenInstanceError):
        plain.datatype = ("", "int")
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
        {"datatype": ("", "int"), "candidate_id": "x"},
        {"datatype": "int"},
        {"datatype": ("",)},
        {"datatype": ("", "int", "x")},
        {"datatype": (1, "int")},
        {"datatype": ("", "")},
        {"candidate_id": ""},
        {"candidate_id": 1},
    ]:
        with pytest.raises(IntegrationError):
            FieldSource(**arguments)
    with pytest.raises(TypeError):
        field_source_metadata(object())  # type: ignore[arg-type]


def test_existing_xsd_source_metadata_bytes_and_projection_are_unchanged():
    schema = compile_schema(
        '<xs:element name="root"><xs:complexType><xs:sequence>'
        '<xs:element name="value" type="xs:int"/></xs:sequence>'
        "</xs:complexType></xs:element>"
    )
    projection = arrow_records(
        schema, element="root", columns={"value": FieldSource(path=("value",))}
    )
    assert isinstance(projection, RecordProjection)
    assert projection.schema.metadata[b"pyxsd:projection"] == b"2"
    assert (
        projection.schema.field("value").metadata[b"pyxsd:source"]
        == b'{"scope": "record", "path": ["value"], "attribute": null, "levels": 0}'
    )
    legacy = arrow_records(schema, element="root")
    assert legacy.schema.metadata[b"pyxsd:projection"] == b"1"
    assert legacy.table(parse(schema, "<root><value>3</value></root>")).to_pylist() == [
        {"value": 3}
    ]


def test_xsd_compile_rejects_relaxng_selectors():
    schema = compile_schema(
        '<xs:element name="root"><xs:complexType><xs:sequence>'
        '<xs:element name="value" type="xs:int"/></xs:sequence>'
        "</xs:complexType></xs:element>"
    )
    for source in [
        FieldSource(path=("value",), datatype=INT),
        FieldSource(path=("value",), candidate_id="candidate"),
    ]:
        with pytest.raises(IntegrationError, match="RELAX NG"):
            compile_projection(schema, element="root", path=(), columns={"value": source})
        with pytest.raises(IntegrationError, match="RELAX NG"):
            arrow_records(schema, element="root", columns={"value": source})


def test_record_ancestor_root_and_path_scopes():
    schema = rows_schema()
    plan = compile_relaxng_projection(
        schema, element="orders", path=("order", "line"), columns=row_columns()
    )
    bindings = {binding.name: binding for binding in plan.columns}
    assert [route.expanded_name for route in plan.row_chain] == ["orders", "order", "line"]
    assert plan.row_path == ("orders", "order", "line")
    assert plan.mode == "columns"
    assert plan.columns[0].source.path == ("quantity",)
    assert bindings["quantity"].occurrence_route.expanded_name == "quantity"
    assert bindings["quantity"].declaration == ("orders", "order", "line", "quantity")
    assert bindings["sku"].declaration[-1] == "@sku"
    assert bindings["number"].declaration == ("orders", "order", "@number")
    assert bindings["version"].declaration == ("orders", "@version")
    assert bindings["code"].declaration == ("orders", "header", "code")
    assert all(not binding.nullable for binding in plan.columns if binding.name != "code")
    assert bindings["code"].nullable
    assert [binding.arrow_shape for binding in plan.columns] == [
        pa.int64(),
        pa.string(),
        pa.string(),
        pa.int64(),
        pa.string(),
    ]


def test_local_clark_and_eqname_spellings():
    schema = compile_grammar(
        f"""
        <start>
          <element name="doc" ns="urn:x" datatypeLibrary="{XSD}">
            <attribute name="version"><data type="int"/></attribute>
            <element name="row" ns="urn:x"><data type="int"/></element>
          </element>
        </start>
        """
    )
    assert _start_route(schema.projection, "{urn:x}doc").expanded_name == "{urn:x}doc"
    assert _start_route(schema.projection, "Q{urn:x}doc").expanded_name == "{urn:x}doc"
    for element in ["doc", "", "{urn:x", "Q{urn:x", "p:doc", "a}b"]:
        with pytest.raises(IntegrationError):
            _start_route(schema.projection, element)
    for step in ["{urn:x}row", "Q{urn:x}row"]:
        plan = compile_relaxng_projection(
            schema,
            element="{urn:x}doc",
            path=(step,),
            columns={"value": FieldSource()},
        )
        assert plan.row_path == ("{urn:x}doc", "{urn:x}row")
    with pytest.raises(IntegrationError):
        compile_relaxng_projection(
            schema,
            element="{urn:x}doc",
            path=("row",),
            columns={"value": FieldSource()},
        )


def test_start_route_wildcard_and_open_name_paths():
    schema = compile_grammar(
        """
        <start>
          <element>
            <anyName/>
            <zeroOrMore>
              <element>
                <anyName/>
                <text/>
              </element>
            </zeroOrMore>
          </element>
        </start>
        """
    )
    plan = compile_relaxng_projection(
        schema,
        element="{urn:any}doc",
        path=("anything",),
        columns={"value": FieldSource()},
    )
    assert plan.row_path == ("{urn:any}doc", "anything")
    assert plan.columns[0].arrow_shape == pa.string()
    assert plan.columns[0].nullable
    assert plan.columns[0].occurrence_route.expanded_name is None


def test_optional_attribute_optional_path_missing_attribute_and_text_nullable():
    schema = compile_grammar(
        f"""
        <start>
          <element name="doc" datatypeLibrary="{XSD}">
            <optional><attribute name="optional"><text/></attribute></optional>
            <optional><element name="container"><element name="value"><data type="int"/></element></element></optional>
            <element name="text"><text/></element>
            <element name="number"><data type="int"/></element>
          </element>
        </start>
        """
    )
    plan = compile_relaxng_projection(
        schema,
        element="doc",
        path=(),
        columns={
            "optional": FieldSource(attribute="optional"),
            "value": FieldSource(path=("container", "value")),
            "text": FieldSource(path=("text",)),
            "number": FieldSource(path=("number",)),
        },
    )
    bindings = {binding.name: binding for binding in plan.columns}
    assert bindings["optional"].nullable
    assert bindings["value"].nullable
    assert bindings["text"].nullable
    assert not bindings["number"].nullable
    with pytest.raises(IntegrationError, match="missing"):
        compile_relaxng_projection(
            schema,
            element="doc",
            path=(),
            columns={"missing": FieldSource(attribute="absent")},
        )


def test_ancestor_above_root_unknown_repeated_and_recursive_paths():
    schema = rows_schema()
    with pytest.raises(IntegrationError, match="above"):
        compile_relaxng_projection(
            schema,
            element="orders",
            path=("order", "line"),
            columns={"x": FieldSource(scope="ancestor", levels=3)},
        )
    with pytest.raises(IntegrationError, match="unknown"):
        compile_relaxng_projection(
            schema,
            element="orders",
            path=("order", "line"),
            columns={"x": FieldSource(path=("absent",))},
        )
    with pytest.raises(IntegrationError, match="repeated"):
        compile_relaxng_projection(
            schema,
            element="orders",
            path=("order",),
            columns={"x": FieldSource(path=("line",))},
        )
    recursive = compile_grammar(
        f"""
        <start>
          <element name="doc" datatypeLibrary="{XSD}">
            <element name="node"><optional><ref name="node"/></optional></element>
          </element>
        </start>
        <define name="node">
          <element name="node"><optional><ref name="node"/></optional></element>
        </define>
        """
    )
    with pytest.raises(IntegrationError, match="repeats"):
        compile_relaxng_projection(
            recursive,
            element="doc",
            path=("node",),
            columns={"x": FieldSource(path=("node",))},
        )


def test_namespaced_attribute_local_spelling_is_ambiguous():
    schema = compile_grammar(
        f"""
        <start>
          <element name="doc" ns="urn:x" datatypeLibrary="{XSD}" xmlns:a="urn:a" xmlns:b="urn:b">
            <attribute name="id" ns="urn:a"><text/></attribute>
            <attribute name="id" ns="urn:b"><text/></attribute>
            <element name="row" ns="urn:x"><element name="value" ns="urn:x"><data type="int"/></element></element>
          </element>
        </start>
        """
    )
    with pytest.raises(IntegrationError, match="ambiguous"):
        compile_relaxng_projection(
            schema,
            element="{urn:x}doc",
            path=("{urn:x}row",),
            columns={"id": FieldSource(scope="ancestor", levels=1, attribute="id")},
        )
    with pytest.raises(IntegrationError, match="missing"):
        compile_relaxng_projection(
            schema,
            element="{urn:x}doc",
            path=("{urn:x}row",),
            columns={"id": FieldSource(scope="ancestor", levels=1, attribute="{urn:c}id")},
        )
    unique = compile_grammar(
        f"""
        <start>
          <element name="doc" datatypeLibrary="{XSD}" xmlns:a="urn:a">
            <attribute name="id" ns="urn:a"><text/></attribute>
            <element name="row"><data type="int"/></element>
          </element>
        </start>
        """
    )
    unique_plan = compile_relaxng_projection(
        unique,
        element="doc",
        path=("row",),
        columns={"id": FieldSource(scope="ancestor", levels=1, attribute="id")},
    )
    assert unique_plan.columns[0].declaration == ("doc", "@{urn:a}id")
    selected = compile_relaxng_projection(
        schema,
        element="{urn:x}doc",
        path=("{urn:x}row",),
        columns={"id": FieldSource(scope="ancestor", levels=1, attribute="{urn:a}id")},
    )
    assert selected.columns[0].declaration == ("{urn:x}doc", "@{urn:a}id")


def test_unique_ambiguous_datatype_and_candidate_selection():
    schema = compile_element('<choice><data type="int"/><text/></choice>')
    with pytest.raises(IntegrationError) as error:
        compile_relaxng_projection(schema, element="doc", path=(), columns={"value": FieldSource()})
    text = message(error.value)
    assert "column 'value'" in text
    assert "ambiguous; set datatype/candidate_id" in text
    selected = compile_relaxng_projection(
        schema,
        element="doc",
        path=(),
        columns={"value": FieldSource(datatype=INT)},
    )
    assert selected.columns[0].selected_contract.type_name == "int"
    assert selected.columns[0].arrow_shape == pa.int64()
    with pytest.raises(IntegrationError, match="matches no"):
        compile_relaxng_projection(
            schema,
            element="doc",
            path=(),
            columns={"value": FieldSource(datatype=(XSD, "long"))},
        )
    builtin = compile_element('<choice><data type="string" datatypeLibrary=""/><text/></choice>')
    builtin_selected = compile_relaxng_projection(
        builtin,
        element="doc",
        path=(),
        columns={"value": FieldSource(datatype=("", "string"))},
    )
    assert builtin_selected.columns[0].selected_contract.library_uri == ""
    assert builtin_selected.columns[0].selected_contract.type_name == "string"


def test_datatype_matching_several_contracts_requires_candidate_id():
    schema = compile_element(
        '<choice><value type="int">1</value><value type="int">2</value></choice>'
    )
    with pytest.raises(IntegrationError, match="set candidate_id"):
        compile_relaxng_projection(
            schema,
            element="doc",
            path=(),
            columns={"value": FieldSource(datatype=INT)},
        )
    root = schema.projection.routes[schema.projection.roots[0]]
    contracts = tuple(root.content.scalar_contracts)
    assert len(contracts) == 2
    for contract in contracts:
        identifier = candidate_id(schema._grammar_fingerprint, contract)
        selected = compile_relaxng_projection(
            schema,
            element="doc",
            path=(),
            columns={"value": FieldSource(candidate_id=identifier)},
        )
        assert selected.columns[0].selected_contract == contract
        assert selected.columns[0].candidate == identifier
        assert selected.columns[0].candidate_id == identifier
    with pytest.raises(IntegrationError, match="unknown"):
        compile_relaxng_projection(
            schema,
            element="doc",
            path=(),
            columns={"value": FieldSource(candidate_id="unknown")},
        )


def test_candidate_ids_are_grammar_scoped_not_value_scoped():
    first = compile_element(
        '<choice><value type="int">1</value><value type="int">2</value></choice>'
    )
    second = compile_element(
        '<attribute name="extra"><text/></attribute>'
        '<choice><value type="int">1</value><value type="int">2</value></choice>'
    )
    first_contract = first.projection.routes[first.projection.roots[0]].content.scalar_contracts[0]
    second_contract = second.projection.routes[second.projection.roots[0]].content.scalar_contracts[
        0
    ]
    assert candidate_id(first._grammar_fingerprint, first_contract) != candidate_id(
        second._grammar_fingerprint, second_contract
    )


@pytest.mark.parametrize(
    ("data_type", "expected"),
    [
        ("string", pa.string()),
        ("token", pa.string()),
        ("QName", pa.string()),
        ("anyURI", pa.string()),
        ("date", pa.string()),
        ("time", pa.string()),
        ("dateTime", pa.string()),
        ("duration", pa.string()),
        ("boolean", pa.bool_()),
        ("int", pa.int64()),
        ("integer", pa.int64()),
        ("long", pa.int64()),
        ("decimal", pa.decimal128(38, 18)),
        ("double", pa.float64()),
        ("float", pa.float64()),
        ("base64Binary", pa.binary()),
        ("hexBinary", pa.binary()),
    ],
)
def test_scalar_arrow_types(data_type, expected):
    schema = compile_element(f'<data type="{data_type}"/>')
    plan = compile_relaxng_projection(
        schema, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == expected
    assert plan.columns[0].arrow_fallback is None
    assert not plan.columns[0].nullable


def test_unrepresentable_decimal_and_datatype_level_list_fall_back_to_string():
    decimal = compile_element('<data type="decimal"><param name="fractionDigits">19</param></data>')
    plan = compile_relaxng_projection(
        decimal, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == pa.string()
    assert "decimal128" in plan.columns[0].arrow_fallback
    idrefs = compile_element('<data type="IDREFS"/>')
    plan = compile_relaxng_projection(
        idrefs, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == pa.string()
    assert "datatype-level list" in plan.columns[0].arrow_fallback
    projection = RelaxNGRecordProjection(decimal, element="doc", columns={"value": FieldSource()})
    assert (
        "decimal128" in projection.schema.field("value").metadata[b"pyxsd:arrow_fallback"].decode()
    )


def test_unsupported_xsd_backing_type_falls_back_to_string():
    datatype = ScalarDatatype(
        library_uri="urn:weird",
        type_name="weird",
        xsd_type=object,
        parameters=(),
        context=DatatypeContext(None, {}),
        constraints=FacetConstraints(),
        white_space="collapse",
    )
    contract = ScalarContract("atomic", "urn:weird", "weird", (), (), "identity")
    arrow, fallback = _atomic_arrow(
        contract,
        SimpleNamespace(encoding_patterns={"identity": Data(datatype=datatype)}),
    )
    assert arrow == pa.string()
    assert "unsupported XSD backing type" in fallback


class CustomIntegerDatatypeFactory(DatatypeFactory):
    """Use a real XSD Integer class so the Arrow mapping is safe."""

    def create(self, type_name, parameters, context):
        from pyxsd.facets import FacetConstraints
        from pyxsd.xsd_data_types import Integer

        del type_name
        return ScalarDatatype(
            library_uri="urn:custom",
            type_name="integer",
            xsd_type=Integer,
            parameters=parameters,
            context=context,
            constraints=FacetConstraints(),
            white_space="collapse",
        )


@dataclass(frozen=True)
class OpaqueDatatype:
    """A custom datatype with stable identity but no XSD backing type."""

    library_uri: str = "urn:opaque"
    type_name: str = "opaque"
    parameters: tuple[object, ...] = ()
    white_space: str = "preserve"
    semantic_context: tuple[tuple[str, str], ...] = ()

    def allows(self, lexical: str, context: object) -> bool:
        del lexical, context
        return True

    def equal(self, left: str, right: str, context: object) -> bool:
        del context
        return left == right


class OpaqueDatatypeFactory(DatatypeFactory):
    """A custom datatype with stable identity but no XSD backing type."""

    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return OpaqueDatatype()


def registration(factory):
    return DatatypeLibraryRegistration(factory, "custom-fingerprint")


def test_custom_datatypes_map_by_xsd_backing_type_or_string_fallback():
    schema = compile_element(
        '<data type="integer" datatypeLibrary="urn:custom"/>',
        datatype_libraries={"urn:custom": registration(CustomIntegerDatatypeFactory())},
    )
    plan = compile_relaxng_projection(
        schema, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == pa.int64()
    opaque = compile_element(
        '<data type="opaque" datatypeLibrary="urn:opaque"/>',
        datatype_libraries={"urn:opaque": registration(OpaqueDatatypeFactory())},
    )
    plan = compile_relaxng_projection(
        opaque, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == pa.string()
    assert "custom datatype" in plan.columns[0].arrow_fallback
    projection = RelaxNGRecordProjection(opaque, element="doc", columns={"value": FieldSource()})
    assert b"pyxsd:arrow_fallback" in projection.schema.field("value").metadata
    list_schema = compile_element(
        '<list><zeroOrMore><data type="opaque" datatypeLibrary="urn:opaque"/></zeroOrMore></list>',
        datatype_libraries={"urn:opaque": registration(OpaqueDatatypeFactory())},
    )
    list_plan = compile_relaxng_projection(
        list_schema, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert list_plan.columns[0].arrow_shape == pa.list_(
        pa.field("item", pa.string(), nullable=False)
    )
    assert "custom datatype" in list_plan.columns[0].arrow_fallback


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            '<list><group><data type="int"/><data type="int"/></group></list>',
            pa.list_(pa.field("item", pa.int64(), nullable=False)),
        ),
        (
            '<list><zeroOrMore><data type="int"/></zeroOrMore></list>',
            pa.list_(pa.field("item", pa.int64(), nullable=False)),
        ),
    ],
)
def test_homogeneous_fixed_and_repeated_list_shapes(body, expected):
    schema = compile_element(body)
    plan = compile_relaxng_projection(
        schema, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].selected_contract.kind == "list"
    assert plan.columns[0].arrow_shape == expected


def test_heterogeneous_and_interpretation_lists_fail():
    heterogeneous = compile_element(
        '<list><group><data type="int"/><data type="string"/></group></list>'
    )
    with pytest.raises(IntegrationError, match="heterogeneous"):
        compile_relaxng_projection(
            heterogeneous, element="doc", path=(), columns={"value": FieldSource()}
        )
    interpretation = compile_element(
        '<list><choice><data type="int"/><data type="string"/></choice></list>'
    )
    with pytest.raises(IntegrationError, match="homogeneous or fixed"):
        compile_relaxng_projection(
            interpretation, element="doc", path=(), columns={"value": FieldSource()}
        )
    empty = compile_element("<list><empty/></list>")
    with pytest.raises(IntegrationError, match="no scalar token"):
        compile_relaxng_projection(empty, element="doc", path=(), columns={"value": FieldSource()})


def test_repeated_and_struct_wildcard_endpoints_fail():
    repeated = rows_schema()
    with pytest.raises(IntegrationError, match="repeated"):
        compile_relaxng_projection(
            repeated,
            element="orders",
            path=(),
            columns={"order": FieldSource(path=("order",))},
        )
    with pytest.raises(IntegrationError, match="struct"):
        compile_relaxng_projection(
            repeated,
            element="orders",
            path=(),
            columns={"order": FieldSource(path=("header",))},
        )
    wildcard = compile_grammar(
        '<start><element name="doc"><zeroOrMore><element><anyName/><text/></element>'
        "</zeroOrMore></element></start>"
    )
    with pytest.raises(IntegrationError, match="wildcard"):
        compile_relaxng_projection(
            wildcard,
            element="doc",
            path=(),
            columns={"doc": FieldSource()},
        )
    empty = compile_element("<empty/>")
    with pytest.raises(IntegrationError, match="no static scalar"):
        compile_relaxng_projection(
            empty,
            element="doc",
            path=(),
            columns={"value": FieldSource()},
        )


def test_default_primitive_schema_and_metadata():
    schema = compile_element('<data type="int"/>')
    projection = RelaxNGRecordProjection(schema, element="doc")
    assert isinstance(arrow_records(schema, element="doc"), RelaxNGRecordProjection)
    assert arrow_records(schema, element="doc").schema.equals(
        projection.schema, check_metadata=True
    )
    assert projection.schema.names == ["value"]
    assert projection.schema.field("value").type == pa.int64()
    assert not projection.schema.field("value").nullable
    metadata = projection.schema.field("value").metadata
    assert metadata[b"pyxsd:type"] == b"int"
    assert metadata[b"pyxsd:arrow"] == b"int64"
    assert json.loads(metadata[b"pyxsd:contract"])["kind"] == "atomic"
    contract = schema.projection.routes[schema.projection.roots[0]].content.scalar_contracts[0]
    assert (
        metadata[b"pyxsd:candidate"] == candidate_id(schema._grammar_fingerprint, contract).encode()
    )
    assert projection.schema.metadata[b"pyxsd:projection"] == b"3"
    assert projection.schema.metadata[b"pyxsd:format"] == b"relaxng"
    assert projection.schema.metadata[b"pyxsd:mode"] == b"default"
    assert projection.schema.metadata[b"pyxsd:declaration"] == b'["doc"]'
    assert projection.schema.metadata[b"pyxsd:row"] == b'["doc"]'
    assert projection.schema.metadata[b"pyxsd:grammar"] == schema._grammar_fingerprint.encode()
    assert b"pyxsd:presence" in projection.schema.metadata
    assert b"pyxsd:exactness" in projection.schema.metadata

    text = RelaxNGRecordProjection(compile_element("<text/>"), element="doc")
    assert text.schema.field("value").type == pa.string()
    assert text.schema.field("value").nullable


def test_default_record_schema_attributes_scalar_child_and_nested_struct():
    schema = compile_element(
        """
        <attribute name="version"><data type="int"/></attribute>
        <element name="count"><data type="int"/></element>
        <optional><element name="flag"><data type="boolean"/></element></optional>
        <zeroOrMore><element name="item"><data type="int"/></element></zeroOrMore>
        <element name="amount">
          <attribute name="currency"><text/></attribute>
          <data type="decimal"/>
        </element>
        """
    )
    projection = RelaxNGRecordProjection(schema, element="doc")
    assert projection.schema.names == ["version", "amount", "count", "flag", "item"]
    assert projection.schema.field("version").type == pa.int64()
    assert not projection.schema.field("version").nullable
    amount = projection.schema.field("amount").type
    assert amount.field("currency").type == pa.string()
    assert amount.field("$").type == pa.decimal128(38, 18)
    assert projection.schema.field("flag").nullable
    assert projection.schema.field("item").type == pa.list_(
        pa.field("item", pa.int64(), nullable=False)
    )
    assert not projection.schema.field("item").nullable
    assert json.loads(projection.schema.field("amount").metadata[b"pyxsd:declaration"]) == [
        "doc",
        "amount",
    ]


def test_default_nested_and_repeated_structs():
    schema = compile_grammar(
        f"""
        <start>
          <element name="doc" datatypeLibrary="{XSD}">
            <element name="row">
              <attribute name="id"><data type="int"/></attribute>
              <element name="child"><element name="value"><text/></element></element>
            </element>
            <zeroOrMore>
              <element name="group">
                <element name="value"><data type="int"/></element>
              </element>
            </zeroOrMore>
          </element>
        </start>
        """
    )
    projection = RelaxNGRecordProjection(schema, element="doc")
    row = projection.schema.field("row").type
    assert isinstance(row, pa.StructType)
    assert row.field("id").type == pa.int64()
    assert isinstance(row.field("child").type, pa.StructType)
    assert row.field("child").type.field("value").type == pa.string()
    group = projection.schema.field("group").type
    assert isinstance(group, pa.ListType)
    assert isinstance(group.value_type, pa.StructType)
    assert group.value_type.field("value").type == pa.int64()


def test_default_recursive_and_empty_routes_fail():
    recursive = compile_grammar(
        f"""
        <start>
          <element name="doc" datatypeLibrary="{XSD}">
            <element name="node"><optional><ref name="node"/></optional></element>
          </element>
        </start>
        <define name="node">
          <element name="node"><optional><ref name="node"/></optional></element>
        </define>
        """
    )
    with pytest.raises(IntegrationError, match="recursive"):
        RelaxNGRecordProjection(recursive, element="doc")
    repeated = compile_grammar(
        f"""
        <start>
          <element name="doc" datatypeLibrary="{XSD}">
            <zeroOrMore><element name="node"><optional><ref name="node"/></optional></element></zeroOrMore>
          </element>
        </start>
        <define name="node">
          <element name="node"><optional><ref name="node"/></optional></element>
        </define>
        """
    )
    with pytest.raises(IntegrationError, match="recursive"):
        RelaxNGRecordProjection(repeated, element="doc")
    empty = compile_element("<empty/>")
    with pytest.raises(IntegrationError, match="no static scalar"):
        RelaxNGRecordProjection(empty, element="doc")


def test_default_mixed_generic_wildcard_and_ambiguous_fail():
    mixed = compile_element('<mixed><element name="x"><text/></element></mixed>')
    with pytest.raises(IntegrationError, match="mixed"):
        RelaxNGRecordProjection(mixed, element="doc")
    generic = compile_element(
        '<choice><data type="int"/><element name="x"><text/></element></choice>'
    )
    with pytest.raises(IntegrationError, match="generic"):
        RelaxNGRecordProjection(generic, element="doc")
    wildcard = compile_element("<zeroOrMore><element><anyName/><text/></element></zeroOrMore>")
    with pytest.raises(IntegrationError, match="wildcard"):
        RelaxNGRecordProjection(wildcard, element="doc")
    ambiguous = compile_element(
        '<element name="x"><choice><data type="int"/><text/></choice></element>'
    )
    with pytest.raises(IntegrationError, match="ambiguous"):
        RelaxNGRecordProjection(ambiguous, element="doc")


def test_default_route_helpers_cover_notallowed_and_impossible_content():
    not_allowed = ContentContract("notAllowed")
    shared = ContentContract("empty")
    assert _content_cannot_match(not_allowed)
    assert _content_cannot_match(ContentContract("choice", (not_allowed, not_allowed)))
    assert _content_cannot_match(ContentContract("repeat", (not_allowed,)))
    assert _content_may_be_empty(ContentContract("choice", (not_allowed, shared)))
    assert _content_may_be_empty(ContentContract("repeat", (shared,)))
    assert _content_may_be_empty(ContentContract("choice", (shared, shared)))
    assert _content_may_be_empty(
        ContentContract("group", (shared, ContentContract("choice", (shared, not_allowed))))
    )
    processed = ContentContract("empty")
    second = ContentContract("group", (processed, ContentContract("empty")))
    assert _content_may_be_empty(ContentContract("group", (second, processed)))
    assert not _content_may_be_empty(not_allowed)
    assert not _content_may_be_empty(ContentContract("mystery"))
    assert not _content_cannot_match(ContentContract("mystery"))
    nested = ContentContract(
        "group", (not_allowed, ContentContract("choice", (shared, not_allowed)))
    )
    assert not _content_may_be_empty(nested)
    route = SimpleNamespace(
        wildcards=(),
        content=SimpleNamespace(mixed=False, model=not_allowed),
        generic_reason=None,
    )
    with pytest.raises(IntegrationError, match="notAllowed"):
        _check_default_route(route)


def test_lazy_pyarrow_import_reports_the_optional_extra(monkeypatch):
    import builtins

    import pyxsd.relaxng.records as records_module

    real_import = builtins.__import__

    def missing_pyarrow(name, *args, **kwargs):
        if name == "pyarrow":
            raise ModuleNotFoundError("No module named 'pyarrow'", name="pyarrow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_pyarrow)
    with pytest.raises(ImportError, match=r"pyxsd\[arrow\]"):
        records_module._pyarrow()

    def different_missing_module(name, *args, **kwargs):
        if name == "pyarrow":
            raise ModuleNotFoundError("No module named 'other'", name="other")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", different_missing_module)
    with pytest.raises(ModuleNotFoundError, match="other"):
        records_module._pyarrow()


def test_record_projection_rows_are_explicitly_unimplemented(tmp_path):
    projection = RelaxNGRecordProjection(compile_element("<text/>"), element="doc")
    with pytest.raises(IntegrationError, match="Task 6"):
        list(projection.batches(None))
    with pytest.raises(IntegrationError, match="Task 6"):
        list(projection._rows(None, None, None))
    with pytest.raises(IntegrationError, match="Task 6"):
        projection.table(None)
    destination = tmp_path / "out.parquet"
    with pytest.raises(IntegrationError, match="Task 6"):
        projection.write_parquet(None, destination)
    assert not destination.exists()
    with pytest.raises(IntegrationError, match="Task 6"):
        projection.write_dataset([], destination)


def test_projection_and_binding_are_frozen():
    plan = compile_relaxng_projection(
        compile_element('<data type="int"/>'),
        element="doc",
        path=(),
        columns={"value": FieldSource()},
    )
    with pytest.raises(FrozenInstanceError):
        plan.columns = ()
    with pytest.raises(FrozenInstanceError):
        plan.columns[0].name = "other"


def test_compile_validates_schema_columns_names_and_sources():
    schema = compile_element('<data type="int"/>')
    with pytest.raises(IntegrationError, match="RelaxNGSchema"):
        compile_relaxng_projection(object(), element="doc", path=(), columns=None)  # type: ignore[arg-type]
    for columns in [{}, None]:
        if columns is None:
            continue
        with pytest.raises(IntegrationError, match="nonempty"):
            compile_relaxng_projection(schema, element="doc", path=(), columns=columns)
    with pytest.raises(IntegrationError, match="nonempty"):
        compile_relaxng_projection(schema, element="doc", path=(), columns={"": FieldSource()})
    with pytest.raises(IntegrationError, match="FieldSource"):
        compile_relaxng_projection(
            schema,
            element="doc",
            path=(),
            columns={"value": "source"},  # type: ignore[dict-item]
        )
    with pytest.raises(IntegrationError, match="row path"):
        compile_relaxng_projection(
            schema,
            element="doc",
            path=["bad"],
            columns={"value": FieldSource()},  # type: ignore[arg-type]
        )
    with pytest.raises(IntegrationError, match="row path"):
        compile_relaxng_projection(
            schema, element="doc", path=("",), columns={"value": FieldSource()}
        )
    with pytest.raises(IntegrationError, match="row route"):
        compile_relaxng_projection(
            schema, element="absent", path=(), columns={"value": FieldSource()}
        )


def test_explicit_metadata_includes_selectors_contract_candidate_and_arrow():
    schema = compile_element('<choice><data type="int"/><text/></choice>')
    plan = compile_relaxng_projection(
        schema,
        element="doc",
        path=(),
        columns={"value!": FieldSource(datatype=INT)},
    )
    projection = RelaxNGRecordProjection(
        schema, element="doc", columns={"value!": FieldSource(datatype=INT)}
    )
    field = projection.schema.field("value!")
    assert field.type == pa.int64()
    source = json.loads(field.metadata[b"pyxsd:source"])
    assert source["datatype"] == list(INT)
    assert "candidate_id" not in source
    assert json.loads(field.metadata[b"pyxsd:contract"])["type_name"] == "int"
    assert field.metadata[b"pyxsd:candidate"] == plan.columns[0].candidate.encode()
    assert field.metadata[b"pyxsd:arrow"] == b"int64"
    assert field.metadata[b"pyxsd:row"] == b'["doc"]'
    assert projection.schema.metadata[b"pyxsd:mode"] == b"columns"

    identifier = plan.columns[0].candidate
    projection = RelaxNGRecordProjection(
        schema, element="doc", columns={"value!": FieldSource(candidate_id=identifier)}
    )
    source = json.loads(projection.schema.field("value!").metadata[b"pyxsd:source"])
    assert source["candidate_id"] == identifier
    assert "datatype" not in source


def test_private_resolution_helpers_cover_ambiguous_and_missing_edges():
    name = Name(None, "x")
    plan = SimpleNamespace(
        routes={
            "a": SimpleNamespace(
                expanded_name="x",
                name_class=name,
                route_id="a",
                children={},
                wildcards=(),
            ),
            "b": SimpleNamespace(
                expanded_name="x",
                name_class=name,
                route_id="b",
                children={},
                wildcards=(),
            ),
        },
        roots=("a", "b"),
    )
    with pytest.raises(IntegrationError, match="several"):
        _start_route(plan, "x")
    wildcard_plan = SimpleNamespace(
        routes={
            "a": SimpleNamespace(
                expanded_name=None,
                name_class=AnyName(),
                route_id="a",
                children={},
                wildcards=(),
            ),
            "b": SimpleNamespace(
                expanded_name=None,
                name_class=AnyName(),
                route_id="b",
                children={},
                wildcards=(),
            ),
        },
        roots=("a", "b"),
    )
    with pytest.raises(IntegrationError, match="wildcard"):
        _start_route(wildcard_plan, "x")
    route = SimpleNamespace(
        children={"x": SimpleNamespace(child_route_id=None)},
        wildcards=(
            SimpleNamespace(role="child", name_class=AnyName(), child_route_id="a"),
            SimpleNamespace(role="child", name_class=AnyName(), child_route_id="b"),
        ),
    )
    with pytest.raises(IntegrationError, match="no compiled child route"):
        _child_step(plan, route, "x", "step")
    wildcard_route = SimpleNamespace(
        children={},
        wildcards=(
            SimpleNamespace(role="child", name_class=AnyName(), child_route_id="a"),
            SimpleNamespace(role="child", name_class=AnyName(), child_route_id="b"),
        ),
    )
    with pytest.raises(IntegrationError, match="several"):
        _child_step(plan, wildcard_route, "x", "step")
    missing = SimpleNamespace(children={}, wildcards=())
    with pytest.raises(IntegrationError, match="unknown"):
        _child_step(plan, missing, "x", "step")


def test_private_resolution_helpers_cover_missing_row_and_wildcard_routes(monkeypatch):
    import pyxsd.relaxng.records as records_module

    plan = SimpleNamespace(routes={}, roots=("root",))
    monkeypatch.setattr(records_module, "_start_route", lambda *_: SimpleNamespace(route_id="root"))
    monkeypatch.setattr(
        records_module,
        "_child_step",
        lambda *_: SimpleNamespace(child_route_id="missing"),
    )
    with pytest.raises(IntegrationError, match="no compiled route"):
        _resolve_row(plan, "doc", ("x",))
    wildcard_route = SimpleNamespace(
        children={},
        wildcards=(SimpleNamespace(role="child", name_class=AnyName(), child_route_id=None),),
    )
    with pytest.raises(IntegrationError, match="no compiled child route"):
        _child_step(SimpleNamespace(routes={}), wildcard_route, "x", "step")


def test_private_endpoint_uses_child_contracts_when_route_content_is_empty(monkeypatch):
    import pyxsd.relaxng.records as records_module

    schema = compile_element('<data type="int"/>')
    plan = schema.projection
    contract = plan.routes[plan.roots[0]].content.scalar_contracts[0]
    route = SimpleNamespace(
        route_id="row",
        expanded_name="doc",
        children={},
        wildcards=(),
        content=SimpleNamespace(scalar_contracts=(), mixed=False, model=ContentContract("empty")),
        generic_reason=None,
    )
    edge = SimpleNamespace(scalar_contracts=(contract,), child_route_id="doc")
    monkeypatch.setattr(records_module, "_resolve_row", lambda *_: (route, (route,), ("doc",)))
    monkeypatch.setattr(
        records_module,
        "_source_endpoint",
        lambda *_: (route, ("doc",), False, edge),
    )
    monkeypatch.setattr(records_module, "_check_endpoint", lambda *_: None)
    result = compile_relaxng_projection(
        schema, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert result.columns[0].selected_contract == contract


def test_private_default_helpers_cover_missing_child_routes_and_unused_fallbacks(monkeypatch):
    import pyxsd.relaxng.records as records_module

    route = SimpleNamespace(
        route_id="route",
        attributes={},
        children={
            "x": SimpleNamespace(
                child_route_id="missing",
                expanded_name="x",
                alias="x",
                cardinality=Cardinality(1, 1),
            )
        },
        content=SimpleNamespace(scalar_contracts=(), model=ContentContract("empty")),
        wildcards=(),
        generic_reason=None,
    )
    plan = SimpleNamespace(routes={}, encoding_patterns={})
    with pytest.raises(IntegrationError, match="no route"):
        _default_record_fields(route, plan, "grammar", ("doc",), ("doc",), ("doc",))
    route.children["x"] = SimpleNamespace(
        child_route_id=None, expanded_name="x", alias="x", cardinality=Cardinality(1, 1)
    )
    with pytest.raises(IntegrationError, match="no compiled route"):
        _default_record_fields(route, plan, "grammar", ("doc",), ("doc",), ("doc",))
    contract = ScalarContract("atomic", XSD, "int", (), (), "identity")
    monkeypatch.setattr(records_module, "scalar_kind", lambda xsd_type: "mystery")
    arrow, fallback = _atomic_arrow(
        contract,
        SimpleNamespace(
            encoding_patterns={
                "identity": Data(
                    datatype=ScalarDatatype(
                        library_uri=XSD,
                        type_name="int",
                        xsd_type=int,
                        parameters=(),
                        context=None,  # type: ignore[arg-type]
                        constraints=None,  # type: ignore[arg-type]
                        white_space="collapse",
                    )
                )
            }
        ),
    )
    assert arrow == pa.string()
    assert "unsupported XSD scalar kind" in fallback


def test_private_selection_and_source_helpers_cover_defensive_branches(monkeypatch):
    import pyxsd.relaxng.records as records_module

    contract = ScalarContract("atomic", XSD, "int", (), (), "identity")
    with pytest.raises(IntegrationError, match="no static"):
        _select_contract(FieldSource(), (), grammar_fingerprint="grammar", description="endpoint")
    with pytest.raises(IntegrationError, match="several"):
        monkeypatch.setattr(records_module, "candidate_id", lambda *_: "same")
        _select_contract(
            FieldSource(candidate_id="same"),
            (contract, ScalarContract("atomic", XSD, "long", (), (), "other")),
            grammar_fingerprint="grammar",
            description="endpoint",
        )

    source = compile_grammar(
        f"""
        <start>
          <element name="doc" datatypeLibrary="{XSD}">
            <element name="row"><element name="value"><data type="int"/></element></element>
          </element>
        </start>
        """
    )
    plan = source.projection
    row = plan.routes[plan.roots[0]].children["row"]
    assert row.child_route_id is not None
    row_route = plan.routes[row.child_route_id]
    edge = SimpleNamespace(
        cardinality=Cardinality(1, 1),
        child_route_id="missing",
        expanded_name="value",
        scalar_contracts=(),
        field=None,
    )
    fake_plan = SimpleNamespace(routes={}, roots=plan.roots)
    monkeypatch.setattr(
        records_module, "_resolve_row", lambda *_: (row_route, (row_route,), ("doc", "row"))
    )
    with pytest.raises(IntegrationError, match="no compiled route"):
        _source_endpoint(fake_plan, (row_route,), ("doc", "row"), FieldSource(path=("value",)))
    assert edge.child_route_id == "missing"


def test_private_list_and_primitive_fallbacks(monkeypatch):
    import pyxsd.relaxng.records as records_module

    contract = ScalarContract("list", "library", "list", (), (), "identity")
    with pytest.raises(IntegrationError, match="derivable"):
        monkeypatch.setattr(records_module, "_list_token_plan", lambda *_: None)
        from pyxsd.relaxng.records import _contract_arrow

        _contract_arrow(
            contract,
            SimpleNamespace(
                encoding_patterns={
                    "identity": __import__("pyxsd.relaxng.patterns", fromlist=["List"]).List(
                        Data(
                            datatype=ScalarDatatype(
                                library_uri=XSD,
                                type_name="int",
                                xsd_type=int,
                                parameters=(),
                                context=SimpleNamespace(namespaces={}),  # type: ignore[arg-type]
                                constraints=SimpleNamespace(
                                    total_digits=None, fraction_digits=None
                                ),
                                white_space="collapse",
                            )
                        )
                    )
                }
            ),
        )
    contract = ScalarContract("atomic", XSD, "int", (), (), "missing-pattern")
    arrow, fallback = _atomic_arrow(contract, SimpleNamespace(encoding_patterns={}))
    assert arrow == pa.string()
    assert "custom datatype" in fallback
