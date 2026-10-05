"""Run the dataset recipe and query only manifest-admitted parts."""

import importlib.util
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load_recipe():
    spec = importlib.util.spec_from_file_location(
        "dataset_recipe", EXAMPLES / "batch" / "export_dataset.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_cli(root, destination, *files, errors="report"):
    return subprocess.run(
        [
            sys.executable,
            str(EXAMPLES / "batch" / "export_dataset.py"),
            str(root),
            str(destination),
            *files,
            "--errors",
            errors,
            "--batch-size",
            "2",
        ],
        capture_output=True,
        text=True,
    )


def test_export_cli_sorted_ids_exact_readback_and_manifest_only(tmp_path):
    xml = (EXAMPLES / "arrow" / "observations.xml").read_text()
    for name in ("b.xml", "a.xml"):
        (tmp_path / name).write_text(xml)
    completed = run_cli(tmp_path, tmp_path / "out", "b.xml", "a.xml")
    assert completed.returncode == 0, completed.stderr
    assert "complete" in completed.stdout and "rows=8" in completed.stdout
    metadata = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert [e["source_id"] for e in metadata["entries"]] == ["a.xml", "b.xml"]
    # Unrelated valid Parquet must not enter a manifest-directed read.
    pq.write_table(pa.table({"wrong": [99]}), tmp_path / "out" / "parts" / "unrelated.parquet")
    recipe = load_recipe()
    table = recipe.read_dataset(tmp_path / "out")
    assert table.num_rows == 8
    assert table.column("__pyxsd_source_id").to_pylist() == ["a.xml"] * 4 + ["b.xml"] * 4
    assert table.column("__pyxsd_row_index").to_pylist() == [1, 2, 3, 4] * 2
    assert (
        table.column("reading").to_pylist()
        == [Decimal("7.125"), Decimal("4.875"), None, Decimal("0.015")] * 2
    )
    assert table.column("accession").to_pylist()[3] == "123456789012345678901234567890"


@pytest.mark.parametrize(
    "kind,code,status",
    [
        ("partial", 1, "partial"),
        ("failed", 1, "failed"),
        ("empty", 0, "complete"),
        ("zero", 0, "complete"),
        ("fatal", 2, None),
    ],
)
def test_export_exit_codes_and_typed_empty(tmp_path, kind, code, status):
    (tmp_path / "bad.xml").write_text("<observations>")
    (tmp_path / "good.xml").write_text((EXAMPLES / "arrow" / "observations.xml").read_text())
    (tmp_path / "empty.xml").write_text("<observations/>")
    files = {
        "partial": ("bad.xml", "good.xml"),
        "failed": ("bad.xml",),
        "empty": ("empty.xml",),
        "zero": (),
        "fatal": ("bad.xml",),
    }[kind]
    completed = run_cli(
        tmp_path, tmp_path / "out", *files, errors="raise" if kind == "fatal" else "report"
    )
    assert completed.returncode == code, completed.stderr
    if kind == "fatal":
        assert "fatal" in completed.stderr and not (tmp_path / "out").exists()
    else:
        metadata = json.loads((tmp_path / "out" / "manifest.json").read_text())
        assert metadata["status"] == status
        table = load_recipe().read_dataset(tmp_path / "out")
        if kind in ("failed", "empty", "zero"):
            assert table.num_rows == 0
            assert table.schema.field("reading").type == pa.decimal128(8, 3)


def test_export_cli_rejects_outside_root_and_existing_destination(tmp_path):
    completed = run_cli(tmp_path, tmp_path / "out", "../outside.xml")
    assert completed.returncode == 2 and "fatal" in completed.stderr
    (tmp_path / "out").mkdir()
    completed = run_cli(tmp_path, tmp_path / "out")
    assert completed.returncode == 2 and list((tmp_path / "out").iterdir()) == []


@pytest.mark.parametrize("consumer", ["duckdb", "polars"])
def test_consumer_reuses_existing_summary_query(tmp_path, consumer):
    if os.environ.get("PYXSD_REQUIRE_EXAMPLES"):
        __import__(consumer)
    else:
        pytest.importorskip(consumer)
    completed = run_cli(EXAMPLES / "arrow", tmp_path / "out", "observations.xml")
    assert completed.returncode == 0, completed.stderr
    query = subprocess.run(
        [
            sys.executable,
            str(EXAMPLES / "batch" / "summarize_dataset.py"),
            str(tmp_path / "out"),
            "--consumer",
            consumer,
        ],
        text=True,
        capture_output=True,
    )
    assert query.returncode == 0, query.stderr
    assert "dissolved oxygen [mg/L] readings=2 populated=2 total=12.000" in query.stdout
    assert "nitrate [mg/L] readings=2 populated=1 total=0.015" in query.stdout
