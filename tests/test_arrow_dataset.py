"""Manifested datasets preserve exact schemas and isolate failed inputs."""

import json
from decimal import Decimal

import pytest

from integration_helpers import compile_schema
from pyxsd.batch import DocumentSource, InputFailure, ParseOutcome
from pyxsd.integrations import IntegrationError
from pyxsd.integrations.projection import FieldSource as F
from pyxsd.validation import IssueSeverity, ValidationIssue

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
records = pytest.importorskip("pyxsd.integrations.arrow").records

SIMPLE = '<xs:element name="r"><xs:complexType><xs:sequence><xs:element name="v" type="xs:int" minOccurs="0" maxOccurs="unbounded"/></xs:sequence></xs:complexType></xs:element>'


def sources_for(tmp_path, *xmls):
    result = []
    for index, xml in enumerate(xmls, 1):
        path = tmp_path / f"input-{index}.xml"
        path.write_text(xml)
        result.append(DocumentSource(f"source-{index}", path))
    return result


def manifest(result):
    return json.loads(result.manifest.read_text())


def test_two_sources_incremental_provenance(tmp_path, monkeypatch):
    import pyxsd
    from pyxsd.integrations.arrow import RecordProjection

    calls = {"compile": 0, "prepare": 0}
    compile_original, prepare_original = pyxsd.compile, RecordProjection.__init__

    def compile_once(*args, **kwargs):
        calls["compile"] += 1
        return compile_original(*args, **kwargs)

    def prepare_once(self, *args, **kwargs):
        calls["prepare"] += 1
        prepare_original(self, *args, **kwargs)

    monkeypatch.setattr(pyxsd, "compile", compile_once)
    monkeypatch.setattr(RecordProjection, "__init__", prepare_once)
    schema = compile_schema(SIMPLE)
    projection = records(schema, element="r", path=("v",), columns={"value": F()})
    monkeypatch.setattr(projection, "table", lambda *a, **kw: pytest.fail("must stream batches"))
    sources = sources_for(tmp_path, "<r><v>1</v><v>2</v><v>3</v></r>", "<r><v>4</v><v>5</v></r>")
    result = projection.write_dataset(sources, tmp_path / "out", selector="v", batch_size=2)
    assert (result.status, result.inputs, result.succeeded, result.failed, result.rows) == (
        "complete",
        2,
        2,
        0,
        5,
    )
    metadata = manifest(result)
    tables = [pq.read_table(result.destination / e["part"]) for e in metadata["entries"]]
    assert [t.num_rows for t in tables] == [3, 2]
    assert tables[0].to_pydict() == {
        "value": [1, 2, 3],
        "__pyxsd_source_id": ["source-1"] * 3,
        "__pyxsd_row_index": [1, 2, 3],
    }
    assert tables[1].to_pydict() == {
        "value": [4, 5],
        "__pyxsd_source_id": ["source-2"] * 2,
        "__pyxsd_row_index": [1, 2],
    }
    reference = pq.read_schema(result.destination / "_schema.parquet")
    assert all(t.schema.equals(reference, check_metadata=True) for t in tables)
    assert reference.metadata == projection.schema.metadata
    assert calls == {"compile": 1, "prepare": 1}
    assert metadata["format_version"] == 1
    assert metadata["selector"] == "v" and metadata["schema_file"] == "_schema.parquet"


def test_empty_selection_has_typed_part(tmp_path):
    projection = records(compile_schema(SIMPLE), element="r", path=("v",), columns={"value": F()})
    result = projection.write_dataset(sources_for(tmp_path, "<r/>"), tmp_path / "out", selector="v")
    entry = manifest(result)["entries"][0]
    assert entry["status"] == "empty" and entry["rows"] == 0
    assert entry["part"] == "parts/part-000001.parquet"
    assert pq.read_table(result.destination / entry["part"]).num_rows == 0


def test_late_projection_failure_discards_entire_part(tmp_path, monkeypatch):
    projection = records(compile_schema(SIMPLE), element="r", path=("v",), columns={"value": F()})
    original = projection.batches

    def late(document, **kwargs):
        yield next(original(document, **kwargs))
        raise IntegrationError("late projection error")

    monkeypatch.setattr(projection, "batches", late)
    result = projection.write_dataset(
        sources_for(tmp_path, "<r><v>1</v><v>2</v><v>3</v></r>"),
        tmp_path / "out",
        selector="v",
        batch_size=2,
        errors="report",
    )
    assert (result.status, result.rows, result.failed) == ("failed", 0, 1)
    entry = manifest(result)["entries"][0]
    assert entry["status"] == "projection_error" and entry["rows"] == 0 and entry["part"] is None
    assert list((result.destination / "parts").iterdir()) == []


def test_namespace_exact_contextual_and_nested_readback(tmp_path):
    body = """<xs:simpleType name="Money"><xs:restriction base="xs:decimal"><xs:totalDigits value="9"/><xs:fractionDigits value="2"/></xs:restriction></xs:simpleType>
    <xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="v" maxOccurs="unbounded"><xs:complexType><xs:sequence>
    <xs:element name="amount" nillable="true"><xs:complexType><xs:simpleContent><xs:extension base="t:Money"><xs:attribute name="unit" type="xs:string"/></xs:extension></xs:simpleContent></xs:complexType></xs:element>
    </xs:sequence><xs:attribute name="id" type="xs:integer" use="required"/></xs:complexType></xs:element>
    </xs:sequence></xs:complexType></xs:element>"""
    schema = compile_schema(body, namespace="urn:data")
    xml = '<r xmlns="urn:data" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><v id="123456789012345678901234567890"><amount unit="USD">12.30</amount></v><v id="2"><amount xsi:nil="true" unit="EUR"/></v></r>'
    sources = sources_for(tmp_path, xml)
    columns = {
        "id": F(attribute="id"),
        "amount": F(path=("{urn:data}amount",)),
        "unit": F(path=("{urn:data}amount",), attribute="unit"),
    }
    projection = records(schema, element="{urn:data}r", path=("{urn:data}v",), columns=columns)
    result = projection.write_dataset(
        sources,
        tmp_path / "columns",
        selector="alias:v",
        namespaces={"alias": "urn:data"},
        batch_size=1,
    )
    rows = pq.read_table(result.destination / manifest(result)["entries"][0]["part"]).to_pylist()
    assert [(r["id"], r["amount"], r["unit"]) for r in rows] == [
        ("123456789012345678901234567890", Decimal("12.30"), "USD"),
        ("2", None, "EUR"),
    ]
    nested = records(schema, element="{urn:data}r")
    result = nested.write_dataset(sources, tmp_path / "nested")
    row = pq.read_table(result.destination / manifest(result)["entries"][0]["part"]).to_pylist()[0]
    assert row["{urn:data}v"][0]["{urn:data}amount"]["$"] == Decimal("12.30")
    assert row["{urn:data}v"][1]["{urn:data}amount"]["$nil"] is True


@pytest.mark.parametrize("name", ["__pyxsd_source_id", "__pyxsd_row_index"])
def test_public_collision_precedes_input_pull_and_staging(tmp_path, name):
    projection = records(compile_schema(SIMPLE), element="r", path=("v",), columns={name: F()})

    def sources():
        pytest.fail("collision must be checked before pulling sources")
        yield

    with pytest.raises(IntegrationError, match="reserved"):
        projection.write_dataset(sources(), tmp_path / "out")
    assert list(tmp_path.iterdir()) == []


def test_augmented_schema_preserves_fields_and_metadata():
    from pyxsd.integrations._dataset import augmented_schema

    original = pa.schema(
        [pa.field("n", pa.decimal128(9, 2), nullable=True, metadata={b"x": b"y"})],
        metadata={b"route": b"r"},
    )
    result = augmented_schema(original)
    assert result.names == ["n", "__pyxsd_source_id", "__pyxsd_row_index"]
    assert result.field(0).equals(original.field(0), check_metadata=True)
    assert result.metadata == original.metadata
    assert result.field(1).type == pa.string() and not result.field(1).nullable
    assert result.field(2).type == pa.int64() and not result.field(2).nullable


@pytest.mark.parametrize("name", ["__pyxsd_source_id", "__pyxsd_row_index"])
def test_augmented_schema_rejects_collision(name):
    from pyxsd.integrations._dataset import augmented_schema

    with pytest.raises(IntegrationError, match="reserved"):
        augmented_schema(pa.schema([(name, pa.string())]))


@pytest.mark.parametrize(
    "status", ["written", "empty", "invalid", "input_error", "projection_error"]
)
def test_entry_is_json_safe_snapshot(status, tmp_path):
    from pyxsd.integrations._dataset import entry_snapshot

    issue = ValidationIssue(IssueSeverity.ERROR, "bad", "sensitive diagnostic", "r")
    outcome = ParseOutcome(
        DocumentSource("../label", tmp_path / "secret.xml"),
        "input_error",
        None,
        (issue,),
        InputFailure("io", "missing"),
    )
    entry = json.loads(json.dumps(entry_snapshot(outcome, 3, status, 0, None)))
    assert entry == {
        "source_id": "../label",
        "ordinal": 3,
        "status": status,
        "rows": 0,
        "part": None,
        "issues": [
            {
                "severity": "error",
                "code": "bad",
                "message": "sensitive diagnostic",
                "element": "r",
                "phase": "instance",
            }
        ],
        "failure": {"kind": "io", "message": "missing"},
    }
