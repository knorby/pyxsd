"""RELAX NG record column compilation and fixed Arrow schemas; rows are Task 6."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, dataclass
from decimal import Decimal

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
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
from pyxsd.relaxng.contracts import candidate_id  # noqa: E402
from pyxsd.relaxng.datatypes import (  # noqa: E402
    DatatypeFactory,
    DatatypeLibraryRegistration,
    ScalarDatatype,
)
from pyxsd.relaxng.projection import ContentContract  # noqa: E402
from pyxsd.relaxng.records import (  # noqa: E402
    RelaxNGRecordProjection,
    _content_cannot_match,
    _content_may_be_empty,
    _integer_bounds,
    _start_route,
    compile_relaxng_projection,
)
from pyxsd.relaxng.schema import RelaxNGSchema  # noqa: E402
from pyxsd.xsd_data_types import Decimal as XsdDecimal  # noqa: E402

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
        pa.int32(),
        pa.string(),
        pa.string(),
        pa.int32(),
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
    assert selected.columns[0].arrow_shape == pa.int32()
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
        ("int", pa.int32()),
        ("long", pa.int64()),
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


def decimal_body(total: int | None = None, fraction: int | None = None) -> str:
    params = ""
    if total is not None:
        params += f'<param name="totalDigits">{total}</param>'
    if fraction is not None:
        params += f'<param name="fractionDigits">{fraction}</param>'
    return f'<data type="decimal">{params}</data>'


def integer_body(
    minimum: int | None = None,
    maximum: int | None = None,
    *,
    min_exclusive: int | None = None,
    max_exclusive: int | None = None,
    type_name: str = "integer",
    total_digits: int | None = None,
) -> str:
    params = ""
    if minimum is not None:
        params += f'<param name="minInclusive">{minimum}</param>'
    if maximum is not None:
        params += f'<param name="maxInclusive">{maximum}</param>'
    if min_exclusive is not None:
        params += f'<param name="minExclusive">{min_exclusive}</param>'
    if max_exclusive is not None:
        params += f'<param name="maxExclusive">{max_exclusive}</param>'
    if total_digits is not None:
        params += f'<param name="totalDigits">{total_digits}</param>'
    return f'<data type="{type_name}">{params}</data>'


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('<data type="byte"/>', pa.int8()),
        ('<data type="short"/>', pa.int16()),
        ('<data type="int"/>', pa.int32()),
        ('<data type="long"/>', pa.int64()),
        ('<data type="unsignedByte"/>', pa.uint8()),
        ('<data type="unsignedShort"/>', pa.uint16()),
        ('<data type="unsignedInt"/>', pa.uint32()),
        ('<data type="unsignedLong"/>', pa.uint64()),
        (integer_body(0, 255), pa.uint8()),
        (integer_body(0, 256), pa.uint16()),
        (integer_body(-128, 127), pa.int8()),
        (integer_body(min_exclusive=-129, max_exclusive=128), pa.int8()),
        (integer_body(-(2**63), 2**63 - 1), pa.int64()),
        (integer_body(0, 2**63 - 1), pa.uint64()),
        (integer_body(0, 2**64 - 1), pa.uint64()),
        ('<data type="int"><param name="minInclusive">0</param></data>', pa.uint32()),
        ('<data type="int"><param name="maxExclusive">256</param></data>', pa.int32()),
        (
            '<data type="int"><param name="minInclusive">-32768</param>'
            '<param name="maxExclusive">256</param></data>',
            pa.int16(),
        ),
        ('<data type="long"><param name="minInclusive">0</param></data>', pa.uint64()),
        (integer_body(maximum=255, type_name="nonNegativeInteger"), pa.uint8()),
        (integer_body(maximum=255, type_name="positiveInteger"), pa.uint8()),
        (integer_body(minimum=-128, type_name="negativeInteger"), pa.int8()),
        (integer_body(minimum=-128, type_name="nonPositiveInteger"), pa.int8()),
        (integer_body(max_exclusive=256, type_name="positiveInteger"), pa.uint8()),
        (integer_body(min_exclusive=-129, type_name="negativeInteger"), pa.int8()),
        (integer_body(total_digits=2), pa.int8()),
        (integer_body(total_digits=18), pa.int64()),
        (integer_body(total_digits=19, type_name="nonNegativeInteger"), pa.uint64()),
        (integer_body(total_digits=2, type_name="positiveInteger"), pa.uint8()),
        (integer_body(total_digits=2, type_name="negativeInteger"), pa.int8()),
        (integer_body(total_digits=2, type_name="byte"), pa.int8()),
        (integer_body(total_digits=2, type_name="short"), pa.int8()),
        (integer_body(total_digits=2, type_name="unsignedLong"), pa.uint8()),
        (integer_body(0, 9, total_digits=2), pa.uint8()),
        (integer_body(min_exclusive=0, max_exclusive=100, total_digits=2), pa.uint8()),
        (integer_body(-129, 127), pa.int16()),
        (integer_body(-128, 128), pa.int16()),
        (integer_body(0, 65535), pa.uint16()),
        (integer_body(0, 65536), pa.uint32()),
        (integer_body(0, 2**32 - 1), pa.uint32()),
        (integer_body(0, 2**32), pa.uint64()),
    ],
)
def test_bounded_integer_arrow_shapes_cover_the_declared_range(body, expected):
    schema = compile_element(body)
    plan = compile_relaxng_projection(
        schema, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == expected
    assert plan.columns[0].arrow_fallback is None
    projection = RelaxNGRecordProjection(schema, element="doc", columns={"value": FieldSource()})
    field = projection.schema.field("value")
    assert field.type == expected
    assert field.metadata[b"pyxsd:arrow"] == str(expected).encode()
    assert b"pyxsd:arrow_fallback" not in field.metadata


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (integer_body(maximum=255, type_name="positiveInteger"), (1, 255)),
        (integer_body(maximum=255, type_name="nonNegativeInteger"), (0, 255)),
        (integer_body(minimum=-128, type_name="negativeInteger"), (-128, -1)),
        (integer_body(minimum=-128, type_name="nonPositiveInteger"), (-128, 0)),
        (integer_body(total_digits=2), (-99, 99)),
        (integer_body(-10, 10, total_digits=2), (-10, 10)),
        (integer_body(-1000, 1000, total_digits=2), (-99, 99)),
        (integer_body(total_digits=3, type_name="byte"), (-128, 127)),
        (integer_body(min_exclusive=2**60, max_exclusive=2**60 + 2), (2**60 + 1, 2**60 + 1)),
    ],
)
def test_effective_integer_bounds_intersect_every_declared_limit(body, expected):
    schema = compile_element(body)
    contract = schema.projection.routes[schema.projection.roots[0]].content.scalar_contracts[0]
    datatype = schema.projection.encoding_patterns[contract.pattern_identity].datatype
    assert _integer_bounds(datatype) == expected


def test_unbounded_and_wide_integer_contracts_fall_back_to_string():
    for body, reason in [
        ('<data type="integer"/>', "unbounded integer range"),
        (integer_body(0), "unbounded integer range"),
        (integer_body(maximum=0), "unbounded integer range"),
        (integer_body(0, 2**64), "exceeds the 64-bit Arrow integer range"),
        (integer_body(-(2**65), 2**65), "exceeds the 64-bit Arrow integer range"),
        (integer_body(total_digits=19), "exceeds the 64-bit Arrow integer range"),
        (integer_body(-(2**63) - 1, 0), "exceeds the 64-bit Arrow integer range"),
    ]:
        schema = compile_element(body)
        plan = compile_relaxng_projection(
            schema, element="doc", path=(), columns={"value": FieldSource()}
        )
        assert plan.columns[0].arrow_shape == pa.string()
        assert plan.columns[0].arrow_fallback is not None
        assert reason in plan.columns[0].arrow_fallback
        projection = RelaxNGRecordProjection(schema, element="doc")
        field = projection.schema.field("value")
        assert field.type == pa.string()
        assert reason in field.metadata[b"pyxsd:arrow_fallback"].decode()
    projection = RelaxNGRecordProjection(
        compile_element('<data type="integer"/>'), element="doc", columns={"value": FieldSource()}
    )
    assert projection.schema.field("value").type == pa.string()
    assert (
        "unbounded integer range"
        in projection.schema.field("value").metadata[b"pyxsd:arrow_fallback"].decode()
    )


@pytest.mark.parametrize("digits", [4301, 5000])
def test_huge_integer_instances_validate_and_project_as_text(digits):
    value = "9" * digits
    for body in (
        f'<data type="integer"><param name="totalDigits">{digits}</param></data>',
        '<data type="integer"/>',
    ):
        schema = compile_element(body)
        document = parse(schema, f"<doc>{value}</doc>")
        assert document.is_valid, document.report
        projection = RelaxNGRecordProjection(schema, element="doc")
        assert projection.schema.field("value").type == pa.string()
        assert projection.table(document).to_pylist() == [{"value": value}]

        explicit = RelaxNGRecordProjection(schema, element="doc", columns={"value": FieldSource()})
        assert explicit.table(document).to_pylist() == [{"value": value}]


@pytest.mark.parametrize("digits", [4301, 5000])
def test_huge_total_digits_fall_back_without_formatting_the_limit(digits):
    body = integer_body(total_digits=digits)
    schema = compile_element(body)
    projection = RelaxNGRecordProjection(schema, element="doc")
    field = projection.schema.field("value")
    assert field.type == pa.string()
    assert (
        "exceeds the 64-bit Arrow integer range" in field.metadata[b"pyxsd:arrow_fallback"].decode()
    )
    assert projection.table(parse(schema, "<doc>1</doc>")).to_pylist() == [{"value": "1"}]

    list_schema = compile_element(f"<list>{body}</list>")
    list_projection = RelaxNGRecordProjection(list_schema, element="doc")
    list_field = list_projection.schema.field("value")
    assert list_field.type == pa.list_(pa.field("item", pa.string(), nullable=False))
    assert (
        "exceeds the 64-bit Arrow integer range"
        in list_field.metadata[b"pyxsd:arrow_fallback"].decode()
    )
    assert list_projection.table(parse(list_schema, "<doc>1</doc>")).to_pylist() == [
        {"value": ["1"]}
    ]


def test_unbounded_integer_values_project_without_overflow(tmp_path):
    value = str(2**80)
    schema = compile_element('<element name="value"><data type="integer"/></element>')
    projection = RelaxNGRecordProjection(schema, element="doc")
    document = parse(schema, f"<doc><value>{value}</value></doc>")
    assert projection.schema.field("value").type == pa.string()
    assert projection.table(document).to_pylist() == [{"value": value}]
    explicit = RelaxNGRecordProjection(
        schema, element="doc", columns={"value": FieldSource(path=("value",))}
    )
    assert explicit.table(document).to_pylist() == [{"value": value}]
    destination = tmp_path / "wide.parquet"
    projection.write_parquet(document, destination)
    assert pq.read_table(destination).to_pylist() == [{"value": value}]
    assert pq.read_schema(destination).field("value").type == pa.string()

    repeated = compile_element(
        '<zeroOrMore><element name="value"><data type="integer"/></element></zeroOrMore>'
    )
    repeated_projection = RelaxNGRecordProjection(repeated, element="doc")
    repeated_document = parse(
        repeated, f"<doc><value>1</value><value>{value}</value><value>-2</value></doc>"
    )
    assert repeated_projection.schema.field("value").type == pa.list_(
        pa.field("item", pa.string(), nullable=False)
    )
    assert (
        "unbounded integer range"
        in repeated_projection.schema.field("value").metadata[b"pyxsd:arrow_fallback"].decode()
    )
    assert repeated_projection.table(repeated_document).to_pylist() == [
        {"value": ["1", value, "-2"]}
    ]

    tokens = compile_element('<list><zeroOrMore><data type="integer"/></zeroOrMore></list>')
    token_projection = RelaxNGRecordProjection(tokens, element="doc")
    token_document = parse(tokens, f"<doc>1 {value}</doc>")
    assert token_projection.schema.field("value").type == pa.list_(
        pa.field("item", pa.string(), nullable=False)
    )
    assert (
        "unbounded integer range"
        in token_projection.schema.field("value").metadata[b"pyxsd:arrow_fallback"].decode()
    )
    assert token_projection.table(token_document).to_pylist() == [{"value": ["1", value]}]

    attribute = compile_element('<attribute name="value"><data type="integer"/></attribute>')
    attribute_projection = RelaxNGRecordProjection(attribute, element="doc")
    attribute_document = parse(attribute, f'<doc value="{value}"/>')
    assert attribute_projection.schema.field("value").type == pa.string()
    assert attribute_projection.table(attribute_document).to_pylist() == [{"value": value}]


def test_unsigned_long_and_narrow_bounded_integers_round_trip(tmp_path):
    schema = compile_element(
        '<element name="big"><data type="unsignedLong"/></element>'
        f'<element name="small">{integer_body(0, 255)}</element>'
    )
    projection = RelaxNGRecordProjection(schema, element="doc")
    document = parse(schema, f"<doc><big>{2**64 - 1}</big><small>255</small></doc>")
    assert projection.schema.field("big").type == pa.uint64()
    assert projection.schema.field("small").type == pa.uint8()
    expected = [{"big": 2**64 - 1, "small": 255}]
    assert projection.table(document).to_pylist() == expected
    destination = tmp_path / "bounded.parquet"
    projection.write_parquet(document, destination)
    assert pq.read_table(destination).to_pylist() == expected


def test_decimal_arrow_types_use_both_facets_and_fall_back_when_unbounded():
    for total, fraction, expected in [
        (30, 0, pa.decimal128(30, 0)),
        (38, 0, pa.decimal128(38, 0)),
        (12, 4, pa.decimal128(12, 4)),
    ]:
        schema = compile_element(decimal_body(total, fraction))
        plan = compile_relaxng_projection(
            schema, element="doc", path=(), columns={"value": FieldSource()}
        )
        assert plan.columns[0].arrow_shape == expected
        assert plan.columns[0].arrow_fallback is None
    thirty = compile_element(decimal_body(30, 0))
    plan = compile_relaxng_projection(
        thirty, element="doc", path=(), columns={"value": FieldSource()}
    )
    pa.array([Decimal("9" * 30)], type=plan.columns[0].arrow_shape)
    for total, fraction, reason in [
        (None, None, "unbounded decimal"),
        (None, 0, "unbounded decimal"),
        (30, None, "unbounded decimal"),
        (39, 0, "precision"),
        (38, 20, "scale"),
    ]:
        schema = compile_element(decimal_body(total, fraction))
        plan = compile_relaxng_projection(
            schema, element="doc", path=(), columns={"value": FieldSource()}
        )
        assert plan.columns[0].arrow_shape == pa.string()
        assert reason in plan.columns[0].arrow_fallback
    projection = RelaxNGRecordProjection(
        compile_element(decimal_body(30, 0)),
        element="doc",
        columns={"value": FieldSource()},
    )
    assert projection.schema.field("value").type == pa.decimal128(30, 0)
    assert b"pyxsd:arrow_fallback" not in projection.schema.field("value").metadata
    unbounded = RelaxNGRecordProjection(
        compile_element(decimal_body()), element="doc", columns={"value": FieldSource()}
    )
    assert (
        "unbounded decimal"
        in unbounded.schema.field("value").metadata[b"pyxsd:arrow_fallback"].decode()
    )


def test_decimal_list_and_default_layouts_preserve_facet_precision():
    list_schema = compile_element(
        "<list><zeroOrMore>" + decimal_body(30, 0) + "</zeroOrMore></list>"
    )
    plan = compile_relaxng_projection(
        list_schema, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == pa.list_(
        pa.field("item", pa.decimal128(30, 0), nullable=False)
    )
    default_schema = compile_element('<element name="amount">' + decimal_body(30, 0) + "</element>")
    projection = RelaxNGRecordProjection(default_schema, element="doc")
    assert projection.schema.field("amount").type == pa.decimal128(30, 0)
    unbounded_default = compile_element('<element name="amount">' + decimal_body() + "</element>")
    unbounded = RelaxNGRecordProjection(unbounded_default, element="doc")
    assert unbounded.schema.field("amount").type == pa.string()
    assert (
        "unbounded decimal"
        in unbounded.schema.field("amount").metadata[b"pyxsd:arrow_fallback"].decode()
    )


def test_unbounded_datatype_level_list_falls_back_to_string():
    idrefs = compile_element('<data type="IDREFS"/>')
    plan = compile_relaxng_projection(
        idrefs, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == pa.string()
    assert "datatype-level list" in plan.columns[0].arrow_fallback


class UnsupportedBackingFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name
        return ScalarDatatype(
            library_uri="urn:weird",
            type_name="weird",
            xsd_type=object,
            parameters=parameters,
            context=context,
            constraints=FacetConstraints(),
            white_space="collapse",
        )


def test_unsupported_xsd_backing_type_falls_back_to_string():
    schema = compile_element(
        '<data type="weird" datatypeLibrary="urn:weird"/>',
        datatype_libraries={"urn:weird": registration(UnsupportedBackingFactory())},
    )
    plan = compile_relaxng_projection(
        schema, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == pa.string()
    assert "unsupported XSD backing type" in plan.columns[0].arrow_fallback


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
    assert plan.columns[0].arrow_shape == pa.string()
    assert "unbounded integer range" in plan.columns[0].arrow_fallback
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
            pa.list_(pa.field("item", pa.int32(), nullable=False)),
        ),
        (
            '<list><zeroOrMore><data type="int"/></zeroOrMore></list>',
            pa.list_(pa.field("item", pa.int32(), nullable=False)),
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
    assert projection.schema.field("value").type == pa.int32()
    assert not projection.schema.field("value").nullable
    metadata = projection.schema.field("value").metadata
    assert metadata[b"pyxsd:type"] == b"int"
    assert metadata[b"pyxsd:arrow"] == b"int32"
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
          <data type="decimal"><param name="totalDigits">30</param><param name="fractionDigits">0</param></data>
        </element>
        """
    )
    projection = RelaxNGRecordProjection(schema, element="doc")
    assert projection.schema.names == ["version", "amount", "count", "flag", "item"]
    assert projection.schema.field("version").type == pa.int32()
    assert not projection.schema.field("version").nullable
    amount = projection.schema.field("amount").type
    assert amount.field("currency").type == pa.string()
    assert amount.field("$").type == pa.decimal128(30, 0)
    assert projection.schema.field("flag").nullable
    assert projection.schema.field("item").type == pa.list_(
        pa.field("item", pa.int32(), nullable=False)
    )
    assert projection.schema.field("item").nullable
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
    assert row.field("id").type == pa.int32()
    assert isinstance(row.field("child").type, pa.StructType)
    assert row.field("child").type.field("value").type == pa.string()
    group = projection.schema.field("group").type
    assert isinstance(group, pa.ListType)
    assert isinstance(group.value_type, pa.StructType)
    assert group.value_type.field("value").type == pa.int32()


def test_default_child_nullability_tracks_optional_and_empty_content():
    schema = compile_element(
        """
        <element name="required"><data type="integer"/></element>
        <optional><element name="optional"><data type="integer"/></element></optional>
        <element name="maybe"><optional><data type="integer"/></optional></element>
        <element name="text"><text/></element>
        <element name="choice"><choice><data type="integer"/><empty/></choice></element>
        <element name="attrs">
          <attribute name="required"><text/></attribute>
          <optional><attribute name="optional"><text/></attribute></optional>
        </element>
        <element name="nested">
          <element name="value"><optional><data type="integer"/></optional></element>
          <element name="struct"><optional><attribute name="optional"><text/></attribute></optional></element>
        </element>
        """
    )
    projection = RelaxNGRecordProjection(schema, element="doc")
    assert not projection.schema.field("required").nullable
    assert projection.schema.field("optional").nullable
    assert projection.schema.field("maybe").nullable
    assert projection.schema.field("text").nullable
    assert projection.schema.field("choice").nullable
    attributes = projection.schema.field("attrs").type
    assert not attributes.field("required").nullable
    assert attributes.field("optional").nullable
    assert "$" not in attributes.names
    nested = projection.schema.field("nested").type
    assert not projection.schema.field("nested").nullable
    assert nested.field("value").nullable
    assert not nested.field("struct").nullable
    assert nested.field("struct").type.field("optional").nullable


def test_repeated_primitive_items_and_optional_lists_are_nullable():
    schema = compile_element(
        """
        <zeroOrMore><element name="items"><optional><data type="int"/></optional></element></zeroOrMore>
        <zeroOrMore><element name="plain"><data type="int"/></element></zeroOrMore>
        <oneOrMore><element name="requiredPlain"><data type="int"/></element></oneOrMore>
        """
    )
    projection = RelaxNGRecordProjection(schema, element="doc")
    items = projection.schema.field("items")
    assert items.type == pa.list_(pa.field("item", pa.int32(), nullable=True))
    assert items.nullable
    plain = projection.schema.field("plain")
    assert plain.type == pa.list_(pa.field("item", pa.int32(), nullable=False))
    assert plain.nullable
    required = projection.schema.field("requiredPlain")
    assert required.type == pa.list_(pa.field("item", pa.int32(), nullable=False))
    assert not required.nullable


def test_default_nullable_child_schema_writes_a_null_parquet_value(tmp_path):
    schema = compile_element(
        '<element name="value"><optional><data type="integer"/></optional></element>'
    )
    projection = RelaxNGRecordProjection(schema, element="doc")
    assert projection.schema.field("value").nullable
    table = pa.Table.from_pylist([{"value": None}], schema=projection.schema)
    destination = tmp_path / "null.parquet"
    pq.write_table(table, destination)
    assert pq.read_table(destination).to_pylist() == [{"value": None}]


def nested_element_schema(depth: int) -> RelaxNGSchema:
    body = '<data type="integer"/>'
    for index in range(depth - 1, -1, -1):
        body = f'<element name="n{index}">{body}</element>'
    return compile_element(body)


@pytest.mark.parametrize("depth", [900, 1200])
def test_deep_default_projection_raises_a_controlled_error(depth):
    schema = nested_element_schema(depth)
    with pytest.raises(IntegrationError, match=r"default field path .* nesting exceeds limit"):
        RelaxNGRecordProjection(schema, element="doc")


def test_nested_default_error_names_the_field_path_and_route():
    schema = compile_element(
        """
        <element name="container">
          <element name="bad">
            <mixed><element name="x"><text/></element></mixed>
          </element>
        </element>
        """
    )
    with pytest.raises(IntegrationError) as error:
        RelaxNGRecordProjection(schema, element="doc")
    message = str(error.value)
    assert "default field path doc/container/bad" in message
    assert "pyxsd.relaxng.route/1:" in message
    assert "mixed" in message


def test_route_ids_are_stable_arrow_metadata():
    schema = compile_element('<data type="integer"/>')
    projection = RelaxNGRecordProjection(schema, element="doc")
    equivalent = RelaxNGRecordProjection(compile_element('<data type="integer"/>'), element="doc")
    row_route = projection.schema.metadata[b"pyxsd:row_route"]
    assert row_route == projection._plan.route.route_id.encode()
    assert equivalent.schema.metadata[b"pyxsd:row_route"] == row_route
    assert projection.schema.field("value").metadata[b"pyxsd:column_route"] == row_route
    columns = RelaxNGRecordProjection(schema, element="doc", columns={"value": FieldSource()})
    assert columns.schema.metadata[b"pyxsd:row_route"] == row_route
    assert (
        columns.schema.field("value").metadata[b"pyxsd:column_route"]
        == columns._plan.columns[0].occurrence_route.route_id.encode()
    )
    assert (
        columns.schema.field("value").metadata[b"pyxsd:column_route"]
        == columns._plan.route.route_id.encode()
    )
    record = compile_element('<element name="child"><data type="integer"/></element>')
    record_projection = RelaxNGRecordProjection(record, element="doc")
    child = record_projection._plan.route.children["child"]
    assert (
        record_projection.schema.field("child").metadata[b"pyxsd:column_route"]
        == child.child_route_id.encode()
    )
    assert record_projection.schema.metadata[b"pyxsd:row_route"] == (
        record_projection._plan.route.route_id.encode()
    )


def test_inconsistent_custom_decimal_facets_fall_back_to_string():
    class InconsistentDecimalFactory(DatatypeFactory):
        def create(self, type_name, parameters, context):
            del type_name, parameters
            return ScalarDatatype(
                library_uri="urn:decimal",
                type_name="inconsistent",
                xsd_type=XsdDecimal,
                parameters=(),
                context=context,
                constraints=FacetConstraints(total_digits=10, fraction_digits=11),
                white_space="collapse",
            )

    schema = compile_element(
        '<data type="inconsistent" datatypeLibrary="urn:decimal"/>',
        datatype_libraries={"urn:decimal": registration(InconsistentDecimalFactory())},
    )
    plan = compile_relaxng_projection(
        schema, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == pa.string()
    assert "inconsistent" in plan.columns[0].arrow_fallback


def test_custom_datatype_with_malformed_xsd_type_falls_back_to_string():
    class MalformedFactory(DatatypeFactory):
        def create(self, type_name, parameters, context):
            del type_name
            return ScalarDatatype(
                library_uri="urn:bad",
                type_name="weird",
                xsd_type=42,  # type: ignore[arg-type]
                parameters=parameters,
                context=context,
                constraints=FacetConstraints(),
                white_space="collapse",
            )

    schema = compile_element(
        '<data type="weird" datatypeLibrary="urn:bad"/>',
        datatype_libraries={"urn:bad": registration(MalformedFactory())},
    )
    plan = compile_relaxng_projection(
        schema, element="doc", path=(), columns={"value": FieldSource()}
    )
    assert plan.columns[0].arrow_shape == pa.string()
    assert "unsupported XSD backing type" in plan.columns[0].arrow_fallback


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
    with pytest.raises(IntegrationError, match="notAllowed"):
        RelaxNGRecordProjection(compile_element("<notAllowed/>"), element="doc")


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


def test_record_projection_write_dataset_publishes_an_empty_manifest(tmp_path):
    projection = RelaxNGRecordProjection(compile_element("<text/>"), element="doc")
    destination = tmp_path / "out"
    result = projection.write_dataset([], destination)
    assert (result.status, result.inputs, result.rows) == ("complete", 0, 0)
    assert result.manifest == destination / "manifest.json"
    assert result.manifest.is_file()


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
    assert field.type == pa.int32()
    source = json.loads(field.metadata[b"pyxsd:source"])
    assert source["datatype"] == list(INT)
    assert "candidate_id" not in source
    assert json.loads(field.metadata[b"pyxsd:contract"])["type_name"] == "int"
    assert field.metadata[b"pyxsd:candidate"] == plan.columns[0].candidate.encode()
    assert field.metadata[b"pyxsd:arrow"] == b"int32"
    assert field.metadata[b"pyxsd:row"] == b'["doc"]'
    assert projection.schema.metadata[b"pyxsd:mode"] == b"columns"

    identifier = plan.columns[0].candidate
    projection = RelaxNGRecordProjection(
        schema, element="doc", columns={"value!": FieldSource(candidate_id=identifier)}
    )
    source = json.loads(projection.schema.field("value!").metadata[b"pyxsd:source"])
    assert source["candidate_id"] == identifier
    assert "datatype" not in source
