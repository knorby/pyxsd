"""RELAX NG Parquet datasets and the schema-neutral manifest.

The dataset writer is shared with XSD: one private part per valid source,
exclusive publication, and projection failures that never leave a partial
part.  XSD manifest keys stay exactly as they were; RELAX NG describes its own
format, version, typed parse policy, and schema limits without claiming an XSD
version.  Field-source metadata round-trips explicit selectors.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import asdict

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

import pyxsd  # noqa: E402
from integration_helpers import compile_schema  # noqa: E402
from pyxsd.batch import DocumentSource  # noqa: E402
from pyxsd.integrations import IntegrationError  # noqa: E402
from pyxsd.integrations._dataset import (  # noqa: E402
    ROW_INDEX,
    SOURCE_ID,
    augmented_schema,
)
from pyxsd.integrations.arrow import records  # noqa: E402
from pyxsd.integrations.dataset import DatasetExportError  # noqa: E402
from pyxsd.integrations.projection import FieldSource  # noqa: E402
from pyxsd.relaxng.contracts import candidate_id  # noqa: E402
from pyxsd.relaxng.limits import RelaxNGLimits  # noqa: E402
from pyxsd.relaxng.records import RelaxNGRecordProjection  # noqa: E402
from pyxsd.relaxng.schema import RelaxNGSchema  # noqa: E402

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
INT = (XSD, "int")
INTEGER = (XSD, "integer")
STRING = (XSD, "string")

MANIFEST_KEYS = [
    "format_version",
    "status",
    "versions",
    "schema_format",
    "schema_version",
    "parse_policy",
    "projection",
    "selector",
    "namespaces",
    "schema_file",
    "counts",
    "entries",
]
XSD_MANIFEST_KEYS = [
    "format_version",
    "status",
    "versions",
    "xsd_version",
    "parse_policy",
    "projection",
    "selector",
    "namespaces",
    "schema_file",
    "counts",
    "entries",
]


def compile_element(
    body: str,
    *,
    name: str = "doc",
    ns: str | None = None,
    limits: RelaxNGLimits | None = None,
) -> RelaxNGSchema:
    namespace = f' ns="{ns}"' if ns else ""
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="{name}"{namespace} '
            f'datatypeLibrary="{XSD}">{body}</element>'
        ),
        limits=limits,
    )
    schema.require_valid()
    return schema


def compile_grammar(body: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(io.StringIO(f'<grammar xmlns="{NS}">{body}</grammar>'))
    schema.require_valid()
    return schema


def item_schema() -> RelaxNGSchema:
    return compile_element(
        '<zeroOrMore><element name="item">'
        '<attribute name="id"><data type="int"/></attribute>'
        "</element></zeroOrMore>"
    )


def item_projection(schema: RelaxNGSchema | None = None) -> RelaxNGRecordProjection:
    return RelaxNGRecordProjection(
        schema or item_schema(),
        element="doc",
        path=("item",),
        columns={"id": FieldSource(attribute="id")},
    )


def typed(schema: RelaxNGSchema, xml: str):
    document = schema.parse(io.StringIO(xml), typed=True)
    document.require_valid()
    return document


def sources_for(tmp_path, *xmls: str) -> list[DocumentSource]:
    result = []
    for index, xml in enumerate(xmls, 1):
        path = tmp_path / f"input-{index}.xml"
        path.write_text(xml)
        result.append(DocumentSource(f"source-{index}", path))
    return result


def manifest(result) -> dict:
    return json.loads(result.manifest.read_text())


def decode_metadata(snapshot: list[dict[str, str]]) -> dict[bytes, bytes]:
    return {
        base64.b64decode(entry["key_base64"]): base64.b64decode(entry["value_base64"])
        for entry in snapshot
    }


def source_from_metadata(data: dict) -> FieldSource:
    return FieldSource(
        scope=data["scope"],
        path=tuple(data["path"]),
        attribute=data["attribute"],
        levels=data["levels"],
        datatype=tuple(data["datatype"]) if "datatype" in data else None,
        candidate_id=data.get("candidate_id"),
    )


def assert_clean(tmp_path, name: str = "out") -> None:
    assert not list(tmp_path.glob(f".{name}.pyxsd-*"))
    assert not (tmp_path / name).exists()


def test_relaxng_dataset_manifest_is_schema_neutral(tmp_path):
    limits = RelaxNGLimits(max_derivative_states=5000, max_diagnostics=7)
    schema = compile_element(
        '<zeroOrMore><element name="item">'
        '<attribute name="id"><data type="int"/></attribute>'
        "</element></zeroOrMore>",
        limits=limits,
    )
    projection = item_projection(schema)
    result = projection.write_dataset(
        sources_for(tmp_path, '<doc><item id="1"/><item id="2"/></doc>'),
        tmp_path / "out",
        selector="item",
        batch_size=1,
    )
    assert (result.status, result.inputs, result.succeeded, result.failed, result.rows) == (
        "complete",
        1,
        1,
        0,
        2,
    )
    metadata = manifest(result)
    assert list(metadata) == MANIFEST_KEYS
    assert "xsd_version" not in metadata
    assert metadata["schema_format"] == "relaxng"
    assert metadata["schema_version"] == "1.0"
    policy = metadata["parse_policy"]
    assert policy == {
        "typed": True,
        "capture": "none",
        "datatype_fingerprint": schema.datatype_fingerprint,
        "limits": asdict(limits),
    }
    assert policy["limits"]["max_derivative_states"] == 5000
    assert json.loads(json.dumps(metadata, allow_nan=False)) == metadata

    assert metadata["selector"] == "item"
    assert metadata["namespaces"] is None
    assert metadata["schema_file"] == "_schema.parquet"
    assert metadata["projection"]["row_route"] == ["doc", "item"]
    assert metadata["counts"] == {"inputs": 1, "succeeded": 1, "failed": 0, "rows": 2}
    assert metadata["projection"]["schema_metadata"] == [
        {
            "key_base64": base64.b64encode(key).decode("ascii"),
            "value_base64": base64.b64encode(value).decode("ascii"),
        }
        for key, value in projection.schema.metadata.items()
    ]
    fields = metadata["projection"]["fields"]
    assert [field["name"] for field in fields] == projection.schema.names
    for field, snapshot in zip(projection.schema, fields, strict=True):
        assert decode_metadata(snapshot["metadata"]) == field.metadata
    entry = metadata["entries"][0]
    assert entry["status"] == "written"
    assert entry["rows"] == 2
    assert entry["part"] == "parts/part-000001.parquet"

    reference = pq.read_schema(result.destination / "_schema.parquet")
    assert reference.equals(augmented_schema(projection.schema), check_metadata=True)


def test_relaxng_dataset_multiple_sources_batches_and_schema_identity(tmp_path):
    schema = item_schema()
    projection = item_projection(schema)
    sources = sources_for(
        tmp_path,
        '<doc><item id="1"/><item id="2"/><item id="3"/></doc>',
        '<doc><item id="4"/><item id="5"/></doc>',
    )
    result = projection.write_dataset(sources, tmp_path / "out", selector="item", batch_size=2)
    assert (result.status, result.inputs, result.succeeded, result.failed, result.rows) == (
        "complete",
        2,
        2,
        0,
        5,
    )
    metadata = manifest(result)
    assert [entry["rows"] for entry in metadata["entries"]] == [3, 2]
    tables = [pq.read_table(result.destination / entry["part"]) for entry in metadata["entries"]]
    assert [table.num_rows for table in tables] == [3, 2]
    assert tables[0].to_pydict() == {
        "id": [1, 2, 3],
        SOURCE_ID: ["source-1"] * 3,
        ROW_INDEX: [1, 2, 3],
    }
    assert tables[1].to_pydict() == {
        "id": [4, 5],
        SOURCE_ID: ["source-2"] * 2,
        ROW_INDEX: [1, 2],
    }
    reference = pq.read_schema(result.destination / "_schema.parquet")
    assert all(table.schema.equals(reference, check_metadata=True) for table in tables)
    assert reference.metadata == augmented_schema(projection.schema).metadata


def test_relaxng_dataset_empty_selection_rows_and_source_sequence(tmp_path):
    schema = item_schema()
    projection = item_projection(schema)
    document = typed(schema, "<doc/>")
    empty = projection.table(document, selector="item")
    assert empty.num_rows == 0
    assert empty.schema.equals(projection.schema, check_metadata=True)

    result = projection.write_dataset(
        sources_for(tmp_path, "<doc/>"), tmp_path / "out", selector="item"
    )
    entry = manifest(result)["entries"][0]
    assert entry["status"] == "empty" and entry["rows"] == 0
    assert entry["part"] == "parts/part-000001.parquet"
    part = pq.read_table(result.destination / entry["part"])
    assert part.num_rows == 0
    assert part.schema.equals(augmented_schema(projection.schema), check_metadata=True)

    result = projection.write_dataset([], tmp_path / "empty-run", selector="item")
    assert (result.status, result.inputs, result.succeeded, result.failed, result.rows) == (
        "complete",
        0,
        0,
        0,
        0,
    )
    assert manifest(result)["entries"] == []
    assert len(pq.read_schema(result.destination / "_schema.parquet")) == 3


def test_relaxng_dataset_namespaced_selector_and_manifest_context(tmp_path):
    schema = compile_grammar(
        f"""
        <start>
          <element name="doc" ns="urn:data" datatypeLibrary="{XSD}">
            <zeroOrMore>
              <element name="item" ns="urn:data">
                <attribute name="id"><data type="int"/></attribute>
              </element>
            </zeroOrMore>
          </element>
        </start>
        """
    )
    projection = RelaxNGRecordProjection(
        schema,
        element="{urn:data}doc",
        path=("{urn:data}item",),
        columns={"id": FieldSource(attribute="id")},
    )
    result = projection.write_dataset(
        sources_for(
            tmp_path,
            '<doc xmlns="urn:data"><item id="1"/><item id="2"/></doc>',
        ),
        tmp_path / "out",
        selector="d:item",
        namespaces={"d": "urn:data"},
    )
    metadata = manifest(result)
    assert metadata["selector"] == "d:item"
    assert metadata["namespaces"] == {"d": "urn:data"}
    table = pq.read_table(result.destination / metadata["entries"][0]["part"])
    assert table.to_pydict()["id"] == [1, 2]


def test_relaxng_dataset_default_projection_and_local_route(tmp_path):
    schema = compile_element(
        '<element name="order"><element name="value"><data type="int"/></element></element>'
        '<element name="header"><element name="code"><text/></element></element>'
    )
    projection = RelaxNGRecordProjection(schema, element="doc", path=("order",))
    sources = sources_for(
        tmp_path,
        "<doc><order><value>1</value></order><header><code>x</code></header></doc>",
    )
    result = projection.write_dataset(sources, tmp_path / "out", selector="order", batch_size=1)
    entry = manifest(result)["entries"][0]
    assert entry["status"] == "written" and entry["rows"] == 1
    assert pq.read_table(result.destination / entry["part"]).to_pylist() == [
        {"value": 1, SOURCE_ID: "source-1", ROW_INDEX: 1}
    ]

    with pytest.raises(DatasetExportError) as caught:
        projection.write_dataset(
            sources_for(
                tmp_path,
                "<doc><order><value>1</value></order><header><code>x</code></header></doc>",
            ),
            tmp_path / "missing-selector",
        )
    assert caught.value.stage == "projection"
    assert "local" in str(caught.value) and "selector" in str(caught.value)
    assert_clean(tmp_path, "missing-selector")


def test_relaxng_explicit_datatype_and_candidate_metadata_round_trip(tmp_path):
    schema = compile_element('<choice><data type="int"/><data type="string"/></choice>')
    route = schema.projection.routes[schema.projection.roots[0]]
    contracts = {contract.type_name: contract for contract in route.content.scalar_contracts}
    identifier = candidate_id(schema._grammar_fingerprint, contracts["string"])

    datatype_projection = RelaxNGRecordProjection(
        schema, element="doc", columns={"value": FieldSource(datatype=INT)}
    )
    candidate_projection = RelaxNGRecordProjection(
        schema, element="doc", columns={"value": FieldSource(candidate_id=identifier)}
    )
    for name, projection, source, expected in (
        ("datatype", datatype_projection, FieldSource(datatype=INT), 12),
        ("candidate", candidate_projection, FieldSource(candidate_id=identifier), "12"),
    ):
        metadata = json.loads(projection.schema.field("value").metadata[b"pyxsd:source"])
        reconstructed = source_from_metadata(metadata)
        assert reconstructed == source
        assert reconstructed.datatype == source.datatype
        assert reconstructed.candidate_id == source.candidate_id
        if source.datatype is not None:
            assert isinstance(reconstructed.datatype, tuple)
            assert "candidate_id" not in metadata
        else:
            assert reconstructed.candidate_id == identifier
            assert "datatype" not in metadata
        result = projection.write_dataset(
            sources_for(tmp_path, "<doc>12</doc>"),
            tmp_path / f"out-{name}",
        )
        entry = manifest(result)["entries"][0]
        assert pq.read_table(result.destination / entry["part"]).to_pylist() == [
            {"value": expected, SOURCE_ID: "source-1", ROW_INDEX: 1}
        ]
        fields = manifest(result)["projection"]["fields"]
        assert decode_metadata(fields[0]["metadata"]) == projection.schema.field("value").metadata
        assert (
            source_from_metadata(
                json.loads(decode_metadata(fields[0]["metadata"])[b"pyxsd:source"])
            )
            == source
        )

    plain = RelaxNGRecordProjection(
        compile_element('<data type="int"/>'), element="doc", columns={"value": FieldSource()}
    )
    plain_source = json.loads(plain.schema.field("value").metadata[b"pyxsd:source"])
    assert plain_source == {
        "scope": "record",
        "path": [],
        "attribute": None,
        "levels": 0,
    }
    assert "datatype" not in plain_source and "candidate_id" not in plain_source


@pytest.mark.parametrize("errors", ["raise", "report"])
@pytest.mark.parametrize("case", ["candidate", "joint"])
def test_relaxng_invalid_row_selection_never_publishes_a_part(tmp_path, errors, case):
    if case == "candidate":
        schema = compile_element('<choice><data type="int"/><data type="string"/></choice>')
        projection = RelaxNGRecordProjection(
            schema, element="doc", columns={"value": FieldSource(datatype=INT)}
        )
        xml = "<doc>abc</doc>"
        needle = "did not survive"
    else:
        schema = compile_element(
            '<choice><group><element name="x"><data type="integer"/></element>'
            '<element name="y"><data type="integer"/></element></group>'
            '<group><element name="x"><data type="string"/></element>'
            '<element name="y"><data type="string"/></element></group></choice>'
        )
        projection = RelaxNGRecordProjection(
            schema,
            element="doc",
            columns={
                "x": FieldSource(path=("x",), datatype=INTEGER),
                "y": FieldSource(path=("y",), datatype=STRING),
            },
        )
        xml = "<doc><x>12</x><y>34</y></doc>"
        needle = "conflict"
    sources = sources_for(tmp_path, xml)
    if errors == "raise":
        with pytest.raises(DatasetExportError) as caught:
            projection.write_dataset(sources, tmp_path / "out", errors=errors)
        assert caught.value.stage == "projection"
        assert needle in str(caught.value)
        assert_clean(tmp_path)
        return
    result = projection.write_dataset(sources, tmp_path / "out", errors=errors)
    assert (result.status, result.rows, result.failed) == ("failed", 0, 1)
    entry = manifest(result)["entries"][0]
    assert entry["status"] == "projection_error"
    assert entry["rows"] == 0 and entry["part"] is None
    assert needle in entry["failure"]["message"]
    assert list((result.destination / "parts").iterdir()) == []


@pytest.mark.parametrize("errors", ["raise", "report"])
@pytest.mark.parametrize(
    "kind,status,stage",
    [
        ("malformed", "input_error", "input_error"),
        ("invalid", "invalid", "invalid"),
        ("selector", "projection_error", "projection"),
    ],
)
def test_relaxng_dataset_expected_failures_follow_error_policy(
    tmp_path, errors, kind, status, stage
):
    projection = item_projection()
    if kind == "malformed":
        sources = sources_for(tmp_path, "<doc>")
    elif kind == "invalid":
        sources = sources_for(tmp_path, '<doc><item id="bad"/></doc>')
    else:
        sources = sources_for(tmp_path, '<doc><item id="1"/></doc>')
    selector = "." if kind == "selector" else "item"
    if errors == "raise":
        with pytest.raises(DatasetExportError) as caught:
            projection.write_dataset(sources, tmp_path / "out", selector=selector, errors=errors)
        assert caught.value.stage == stage
        assert caught.value.source_id == "source-1"
        assert_clean(tmp_path)
        return
    result = projection.write_dataset(sources, tmp_path / "out", selector=selector, errors=errors)
    entry = manifest(result)["entries"][0]
    assert entry["status"] == status
    if kind == "invalid":
        assert entry["issues"][0]["severity"] == "error"
    elif kind == "malformed":
        assert entry["failure"]["kind"] == "malformed_xml"
    else:
        assert entry["failure"]["kind"] == "projection"


def test_relaxng_report_mode_mixed_inputs_are_partial(tmp_path):
    projection = item_projection()
    sources = sources_for(
        tmp_path,
        '<doc><item id="1"/></doc>',
        "<doc>",
        '<doc><item id="2"/></doc>',
    )
    result = projection.write_dataset(sources, tmp_path / "out", selector="item", errors="report")
    assert (result.status, result.inputs, result.succeeded, result.failed, result.rows) == (
        "partial",
        3,
        2,
        1,
        2,
    )
    entries = manifest(result)["entries"]
    assert [entry["status"] for entry in entries] == ["written", "input_error", "written"]
    assert [entry["part"] for entry in entries] == [
        "parts/part-000001.parquet",
        None,
        "parts/part-000003.parquet",
    ]


@pytest.mark.parametrize("errors", ["raise", "report"])
@pytest.mark.parametrize(
    "kind", ["ArrowMemoryError", "ArrowIOError", "ArrowNotImplementedError", "ArrowCapacityError"]
)
def test_relaxng_dataset_backend_fault_is_fatal(tmp_path, monkeypatch, errors, kind):
    class FaultyBatch:
        @staticmethod
        def from_pylist(*args, **kwargs):
            raise getattr(pa, kind)("backend failure")

    monkeypatch.setattr(pa, "RecordBatch", FaultyBatch)
    with pytest.raises(DatasetExportError) as caught:
        item_projection().write_dataset(
            sources_for(tmp_path, '<doc><item id="1"/></doc>'),
            tmp_path / "out",
            selector="item",
            errors=errors,
        )
    assert caught.value.stage == "projection_backend"
    assert caught.value.source_id == "source-1"
    assert_clean(tmp_path)


def test_relaxng_translated_arrow_value_error_is_reportable(tmp_path, monkeypatch):
    class FaultyBatch:
        @staticmethod
        def from_pylist(*args, **kwargs):
            raise pa.ArrowInvalid("value incompatible with fixed type")

    monkeypatch.setattr(pa, "RecordBatch", FaultyBatch)
    result = item_projection().write_dataset(
        sources_for(tmp_path, '<doc><item id="1"/></doc>'),
        tmp_path / "out",
        selector="item",
        errors="report",
    )
    assert (result.status, result.rows, result.failed) == ("failed", 0, 1)
    assert manifest(result)["entries"][0]["status"] == "projection_error"


@pytest.mark.parametrize("name", [SOURCE_ID, ROW_INDEX])
def test_relaxng_reserved_provenance_name_rejects_before_pull(tmp_path, name):
    projection = RelaxNGRecordProjection(
        compile_element('<data type="int"/>'), element="doc", columns={name: FieldSource()}
    )

    def sources():
        pytest.fail("reserved provenance must be checked before pulling sources")
        yield

    with pytest.raises(IntegrationError, match="reserved"):
        projection.write_dataset(sources(), tmp_path / "out")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"batch_size": 0},
        {"batch_size": True},
        {"errors": "ignore"},
        {"selector": 7},
        {"namespaces": {"p": 7}},
    ],
)
def test_relaxng_bad_dataset_configuration_precedes_staging(tmp_path, kwargs):
    def sources():
        pytest.fail("configuration must reject first")
        yield

    with pytest.raises(IntegrationError):
        item_projection().write_dataset(sources(), tmp_path / "out", **kwargs)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["empty_dir", "file", "symlink", "dangling"])
def test_relaxng_existing_destination_is_never_changed(tmp_path, kind):
    target = tmp_path / "out"
    marker = tmp_path / "marker"
    marker.write_bytes(b"unchanged")
    if kind == "empty_dir":
        target.mkdir()
    elif kind == "file":
        target.write_bytes(b"unchanged")
    else:
        target.symlink_to(marker if kind == "symlink" else tmp_path / "missing")

    def sources():
        pytest.fail("existing destination must reject before source pull")
        yield

    with pytest.raises(DatasetExportError):
        item_projection().write_dataset(sources(), target, selector="item")
    assert marker.read_bytes() == b"unchanged"
    assert target.is_symlink() if kind in ("symlink", "dangling") else target.exists()
    assert not list(tmp_path.glob(".out.pyxsd-*"))


def test_relaxng_borrowed_parquet_sink_and_local_replacement(tmp_path):
    schema = item_schema()
    projection = item_projection(schema)
    document = typed(schema, '<doc><item id="1"/><item id="2"/></doc>')
    table = projection.table(document, selector="item")

    sink = io.BytesIO()
    projection.write_parquet(document, sink, selector="item", batch_size=1)
    assert not sink.closed
    assert pq.read_table(pa.BufferReader(sink.getvalue())).to_pylist() == table.to_pylist()

    destination = tmp_path / "rows.parquet"
    destination.write_bytes(b"old")
    projection.write_parquet(document, destination, selector="item")
    assert pq.read_table(destination).equals(table)

    empty = io.BytesIO()
    projection.write_parquet(document, empty, selector="missing")
    result = pq.read_table(pa.BufferReader(empty.getvalue()))
    assert result.num_rows == 0
    assert result.schema.equals(projection.schema, check_metadata=True)


def test_xsd_dataset_manifest_keys_and_values_are_unchanged(tmp_path):
    schema = compile_schema(
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="v" type="xs:int" minOccurs="0" maxOccurs="unbounded"/>'
        "</xs:sequence></xs:complexType></xs:element>"
    )
    projection = records(schema, element="r", path=("v",), columns={"value": FieldSource()})
    result = projection.write_dataset(
        sources_for(tmp_path, "<r><v>1</v><v>2</v></r>"),
        tmp_path / "out",
        selector="v",
        batch_size=1,
    )
    metadata = manifest(result)
    assert list(metadata) == XSD_MANIFEST_KEYS
    assert "schema_format" not in metadata and "schema_version" not in metadata
    assert metadata["xsd_version"] == str(schema._host.processor_version) == "1.1"
    assert metadata["parse_policy"] == asdict(schema.mode)
    assert metadata["counts"] == {"inputs": 1, "succeeded": 1, "failed": 0, "rows": 2}
    assert metadata["projection"]["row_route"] == ["r", "v"]
    reference = pq.read_schema(result.destination / "_schema.parquet")
    assert reference.equals(augmented_schema(projection.schema), check_metadata=True)


def test_relaxng_write_dataset_result_contract(tmp_path):
    result = item_projection().write_dataset(
        sources_for(tmp_path, '<doc><item id="1"/></doc>'),
        tmp_path / "out",
        selector="item",
    )
    assert result.destination == (tmp_path / "out")
    assert result.manifest == result.destination / "manifest.json"
    assert result.manifest.is_file()
    assert json.loads(result.manifest.read_text())["versions"]["pyxsd"] == pyxsd.__version__
