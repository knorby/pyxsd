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


def simple_projection():
    return records(compile_schema(SIMPLE), element="r", path=("v",), columns={"value": F()})


def assert_clean(tmp_path):
    assert not list(tmp_path.glob(".out.pyxsd-*"))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("errors", ["raise", "report"])
@pytest.mark.parametrize(
    "kind", ["ArrowMemoryError", "ArrowIOError", "ArrowNotImplementedError", "ArrowCapacityError"]
)
def test_translated_backend_fault_is_never_skippable(tmp_path, monkeypatch, errors, kind):
    from pyxsd.integrations.dataset import DatasetExportError

    class FaultyBatch:
        @staticmethod
        def from_pylist(*args, **kwargs):
            raise getattr(pa, kind)("backend failure")

    # Exercise the real adapter's exception translation, not a fabricated
    # IntegrationError: it used to hide fatal Arrow faults from the writer.
    monkeypatch.setattr(pa, "RecordBatch", FaultyBatch)
    with pytest.raises(DatasetExportError) as caught:
        simple_projection().write_dataset(
            sources_for(tmp_path, "<r><v>1</v></r>"),
            tmp_path / "out",
            selector="v",
            errors=errors,
        )
    assert caught.value.source_id == "source-1"
    cause = caught.value.__cause__
    while cause.__cause__ is not None:
        cause = cause.__cause__
    assert isinstance(cause, getattr(pa, kind))
    assert_clean(tmp_path)


def test_translated_arrow_value_error_is_reportable(tmp_path, monkeypatch):
    class FaultyBatch:
        @staticmethod
        def from_pylist(*args, **kwargs):
            raise pa.ArrowInvalid("value incompatible with fixed type")

    monkeypatch.setattr(pa, "RecordBatch", FaultyBatch)
    result = simple_projection().write_dataset(
        sources_for(tmp_path, "<r><v>1</v></r>"),
        tmp_path / "out",
        selector="v",
        errors="report",
    )
    assert result.failed == 1 and result.rows == 0
    assert manifest(result)["entries"][0]["status"] == "projection_error"


def test_report_and_raise_parse_sequence(tmp_path):
    from pyxsd.integrations.dataset import DatasetExportError

    sources = sources_for(
        tmp_path, "<r><v>1</v></r>", "<r><v>bad</v></r>", "<r>", "<r/>", "<r><v>2</v></r>"
    )
    sources[3].path.unlink()
    result = simple_projection().write_dataset(
        sources, tmp_path / "report", selector="v", errors="report"
    )
    assert (result.status, result.inputs, result.succeeded, result.failed, result.rows) == (
        "partial",
        5,
        2,
        3,
        2,
    )
    entries = manifest(result)["entries"]
    assert [e["status"] for e in entries] == [
        "written",
        "invalid",
        "input_error",
        "input_error",
        "written",
    ]
    assert entries[1]["issues"][0]["severity"] == "error"
    assert entries[2]["failure"]["kind"] == "malformed_xml"
    assert entries[3]["failure"]["kind"] == "io"
    assert [e["part"] for e in entries] == [
        "parts/part-000001.parquet",
        None,
        None,
        None,
        "parts/part-000005.parquet",
    ]
    pulled = []

    def inputs():
        for source in sources:
            pulled.append(source.id)
            yield source

    with pytest.raises(DatasetExportError) as caught:
        simple_projection().write_dataset(inputs(), tmp_path / "out", selector="v")
    assert caught.value.source_id == "source-2" and caught.value.stage == "invalid"
    assert caught.value.__cause__ is not None
    assert pulled == ["source-1", "source-2"]
    assert_clean(tmp_path)


@pytest.mark.parametrize(
    "xmls,status,succeeded,failed",
    [
        ((), "complete", 0, 0),
        (("<r/>", "<r/>"), "complete", 2, 0),
        (("<r><v>bad</v></r>", "<r>"), "failed", 0, 2),
    ],
)
def test_empty_run_statuses(tmp_path, xmls, status, succeeded, failed):
    result = simple_projection().write_dataset(
        sources_for(tmp_path, *xmls), tmp_path / "out", selector="v", errors="report"
    )
    assert (result.status, result.succeeded, result.failed, result.rows) == (
        status,
        succeeded,
        failed,
        0,
    )
    table = pq.read_table(result.destination / "_schema.parquet")
    assert table.num_rows == 0 and table.schema.field("value").type == pa.int32()
    assert len(list((result.destination / "parts").iterdir())) == succeeded


@pytest.mark.parametrize("errors", ["raise", "report"])
def test_duplicate_ids_abort_but_paths_and_business_keys_can_repeat(tmp_path, errors):
    from pyxsd.integrations.dataset import DatasetExportError

    source = sources_for(tmp_path, "<r><v>7</v></r>")[0]
    with pytest.raises(DatasetExportError) as caught:
        simple_projection().write_dataset(
            [source, source], tmp_path / "out", selector="v", errors=errors
        )
    assert isinstance(caught.value.__cause__, ValueError)
    assert_clean(tmp_path)
    result = simple_projection().write_dataset(
        [source, DocumentSource("../../danger/label", source.path)],
        tmp_path / "out",
        selector="v",
        errors=errors,
    )
    assert result.rows == 2
    assert [e["part"] for e in manifest(result)["entries"]] == [
        "parts/part-000001.parquet",
        "parts/part-000002.parquet",
    ]


@pytest.mark.parametrize("errors", ["raise", "report"])
def test_wrong_selector_route_is_projection_failure(tmp_path, errors):
    from pyxsd.integrations.dataset import DatasetExportError

    projection = simple_projection()
    sources = sources_for(tmp_path, "<r><v>1</v></r>")
    if errors == "raise":
        with pytest.raises(DatasetExportError) as caught:
            projection.write_dataset(sources, tmp_path / "out", selector=".", errors=errors)
        assert caught.value.stage == "projection"
        assert_clean(tmp_path)
    else:
        result = projection.write_dataset(sources, tmp_path / "out", selector=".", errors=errors)
        assert manifest(result)["entries"][0]["status"] == "projection_error"


@pytest.mark.parametrize("kind", ["empty_dir", "file", "symlink", "dangling"])
def test_existing_destination_never_changed(tmp_path, kind):
    from pyxsd.integrations.dataset import DatasetExportError

    target = tmp_path / "out"
    marker = tmp_path / "marker"
    marker.write_bytes(b"unchanged")
    if kind == "empty_dir":
        target.mkdir()
    elif kind == "file":
        target.write_bytes(b"unchanged")
    else:
        target.symlink_to(marker if kind == "symlink" else tmp_path / "missing")

    def inputs():
        pytest.fail("existing destination must reject before source pull")
        yield

    with pytest.raises(DatasetExportError):
        simple_projection().write_dataset(inputs(), target)
    assert marker.read_bytes() == b"unchanged"
    assert target.is_symlink() if kind in ("symlink", "dangling") else target.exists()
    assert not list(tmp_path.glob(".out.pyxsd-*"))


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
def test_bad_configuration_precedes_staging(tmp_path, kwargs):
    def inputs():
        pytest.fail("configuration must reject first")
        yield

    with pytest.raises(IntegrationError):
        simple_projection().write_dataset(inputs(), tmp_path / "out", **kwargs)
    assert list(tmp_path.iterdir()) == []


def test_missing_parent_is_not_created(tmp_path):
    from pyxsd.integrations.dataset import DatasetExportError

    with pytest.raises(DatasetExportError):
        simple_projection().write_dataset([], tmp_path / "missing" / "out")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("errors", ["raise", "report"])
@pytest.mark.parametrize(
    "fault",
    [
        "open",
        "write",
        "close",
        "schema",
        "manifest",
        "publish",
        "binder",
        "iterator",
        "unexpected_projection",
        "interrupt",
    ],
)
def test_fatal_faults_abort_and_cleanup_only_owned_output(tmp_path, monkeypatch, errors, fault):
    import pyxsd.integrations._dataset as impl
    from pyxsd.integrations.dataset import DatasetExportError

    sources = sources_for(tmp_path, "<r><v>1</v></r>")
    stale = tmp_path / ".out.pyxsd-stale"
    stale.mkdir()
    (stale / "keep").write_bytes(b"untouched")
    projection = simple_projection()
    original = impl.pq.ParquetWriter
    opened = []

    class Writer:
        def __init__(self, path, schema, **kwargs):
            if fault == "open" or (fault == "schema" and str(path).endswith("_schema.parquet")):
                raise OSError("disk full")
            self.writer = original(path, schema, **kwargs)
            self.closed = False
            opened.append(self)

        def __enter__(self):
            return self

        def write_batch(self, batch):
            if fault == "write":
                raise OSError("disk full")
            self.writer.write_batch(batch)

        def __exit__(self, *args):
            self.writer.close()
            self.closed = True
            if fault == "close":
                raise OSError("close failed")

    monkeypatch.setattr(impl.pq, "ParquetWriter", Writer)

    def explode(*args, **kwargs):
        if fault == "interrupt":
            raise KeyboardInterrupt()
        raise RuntimeError("unexpected fault")

    if fault == "manifest":
        monkeypatch.setattr(impl.json, "dump", explode)
    elif fault == "publish":
        monkeypatch.setattr(impl, "publish", explode)
    elif fault == "binder":
        monkeypatch.setattr(projection._schema, "parse", explode)
    elif fault in ("unexpected_projection", "interrupt"):
        monkeypatch.setattr(projection, "_rows", explode)
    elif fault == "iterator":
        first = sources[0]

        def broken():
            yield first
            explode()

        sources = broken()
    with pytest.raises(KeyboardInterrupt if fault == "interrupt" else DatasetExportError):
        projection.write_dataset(sources, tmp_path / "out", selector="v", errors=errors)
    assert all(writer.closed for writer in opened)
    assert not (tmp_path / "out").exists()
    assert list(tmp_path.glob(".out.pyxsd-*")) == [stale]
    assert (stale / "keep").read_bytes() == b"untouched"


def test_publish_native_no_replace_preserves_empty_destination(tmp_path, monkeypatch):
    import os
    import sys

    from pyxsd.integrations._dataset import publish

    staging, target = tmp_path / "staging", tmp_path / "out"
    staging.mkdir()
    (staging / "manifest.json").write_text("complete")
    target.mkdir()
    if sys.platform in ("darwin", "linux", "win32"):
        # Model a destination appearing after the absence check; the primitive
        # itself must refuse it, not just our Python preflight.
        monkeypatch.setattr(os.path, "lexists", lambda path: False)
    with pytest.raises(FileExistsError):
        publish(staging, target)
    assert list(target.iterdir()) == [] and (staging / "manifest.json").read_text() == "complete"


def test_dataset_releases_previous_documents_and_report_exceptions(tmp_path, monkeypatch):
    import gc
    import weakref

    projection = simple_projection()
    source = sources_for(tmp_path, "<r><v>1</v></r>")[0]
    documents, causes = [], []
    original = projection._schema.parse

    class Failure(IntegrationError):
        pass

    def observed(path):
        gc.collect()
        assert sum(ref() is not None for ref in documents) <= 1
        doc = original(path)
        documents.append(weakref.ref(doc))
        return doc

    def late(document, **kwargs):
        yield pa.RecordBatch.from_pylist([{"value": 1}], schema=projection.schema)
        error = Failure("late")
        causes.append(weakref.ref(error))
        raise error

    monkeypatch.setattr(projection._schema, "parse", observed)
    monkeypatch.setattr(projection, "batches", late)
    result = projection.write_dataset(
        (DocumentSource(str(i), source.path) for i in range(20)), tmp_path / "out", errors="report"
    )
    gc.collect()
    assert all(ref() is None for ref in documents + causes)
    assert result.failed == 20 and result.rows == 0


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
    from pyxsd.integrations._dataset import augmented_schema

    assert pq.read_schema(result.destination / "_schema.parquet").equals(
        augmented_schema(nested.schema), check_metadata=True
    )
    assert pq.read_schema(result.destination / manifest(result)["entries"][0]["part"]).equals(
        augmented_schema(nested.schema), check_metadata=True
    )
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
