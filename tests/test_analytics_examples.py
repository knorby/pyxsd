"""Consumer analytics examples: shared preparation, catalogs, and compatibility."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ANALYTICS = EXAMPLES / "analytics"


def load_common():
    spec = importlib.util.spec_from_file_location("analytics_common", ANALYTICS / "common.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["analytics_common"] = module
    spec.loader.exec_module(module)
    return module


def test_analytical_fixture_contract():
    common = load_common()
    _, document, nested, contextual = common.prepare_observations()

    # Schema-derived types exist before any rows are read.
    assert contextual.schema.field("station").type == pa.string()
    assert contextual.schema.field("sequence").type == pa.uint32()
    assert contextual.schema.field("accession").type == pa.string()
    assert contextual.schema.field("collected").type == pa.string()
    assert contextual.schema.field("analyte").type == pa.string()
    assert contextual.schema.field("reading").type == pa.decimal128(8, 3)
    assert contextual.schema.field("unit").type == pa.string()
    assert contextual.schema.field("quality").type == pa.bool_()

    rows = contextual.table(document, selector="observation", revalidate=True).to_pylist()
    assert len(rows) == 4
    assert [row["sequence"] for row in rows] == [1, 2, 3, 4]
    assert [row["sequence"] for row in rows if not row["quality"]] == [2, 3]
    assert sum(1 for row in rows if row["reading"] is not None) == 3
    assert rows[2]["reading"] is None
    assert rows[2]["unit"] == "mg/L"
    assert rows[3]["accession"] == "123456789012345678901234567890"
    assert [row["collected"] for row in rows] == [
        "2026-10-01T08:00:00-04:00",
        "2026-10-01T08:15:00-04:00",
        "2026-10-01T08:30:00-04:00",
        "2026-10-01T12:45:00.123456789Z",
    ]

    nested_rows = nested.table(document, selector="observation", revalidate=True).to_pylist()
    nil_reading = nested_rows[2]["reading"]
    assert nil_reading["$nil"] is True
    assert nil_reading["@unit"] == "mg/L"
    assert nil_reading["$"] is None
    assert [len(row["tag"]) for row in nested_rows] == [2, 1, 1, 0]
    # A list-valued simple element that is absent is None; consumers normalize to
    # zero child rows. A repeated element that is absent is an empty list.
    assert [
        None if row["replicates"] is None else len(row["replicates"]) for row in nested_rows
    ] == [
        3,
        2,
        None,
        3,
    ]


def test_stations_unique_key_validation(tmp_path):
    common = load_common()
    stations = common.load_stations(ANALYTICS / "stations.json")
    assert stations == [
        {"station": "RIVER-UPSTREAM", "region": "river"},
        {"station": "RIVER-DOWNSTREAM", "region": "river"},
        {"station": "COASTAL-ESTUARY", "region": "coastal"},
    ]
    duplicate = tmp_path / "stations.json"
    duplicate.write_text(
        json.dumps(
            {
                "stations": [
                    {"station": "RIVER-UPSTREAM", "region": "river"},
                    {"station": "RIVER-UPSTREAM", "region": "coastal"},
                ]
            }
        )
    )
    with pytest.raises(ValueError, match="RIVER-UPSTREAM"):
        common.load_stations(duplicate)


def test_assert_consumer_profile_rejects_wide_decimal():
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    common.assert_consumer_profile(
        contextual.table(document, selector="observation", revalidate=True)
    )
    wide = pa.table({"wide": pa.array([1], type=pa.decimal256(76, 6))})
    with pytest.raises(ValueError, match=r"wide.*38"):
        common.assert_consumer_profile(wide)


def test_common_imports_without_consumers():
    common_path = ANALYTICS / "common.py"
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('c', r'{common_path}')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "bad = [name for name in ('pydantic', 'duckdb', 'polars') if name in sys.modules]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
