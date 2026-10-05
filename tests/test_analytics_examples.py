"""Consumer analytics examples: shared preparation, catalogs, and compatibility."""

import importlib.util
import json
import os
import subprocess
import sys
from decimal import Decimal
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


def require(module: str):
    """Consumer imports: skip by default, fail in the dedicated example jobs."""
    if os.environ.get("PYXSD_REQUIRE_EXAMPLES"):
        return importlib.import_module(module)
    return pytest.importorskip(module)


def load_example_module(name: str):
    spec = importlib.util.spec_from_file_location(name, ANALYTICS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _duckdb_observations():
    require("duckdb")
    return load_example_module("duckdb_observations")


def _expected_summary():
    return [
        {
            "analyte": "dissolved oxygen",
            "unit": "mg/L",
            "readings": 2,
            "populated": 2,
            "total": Decimal("12.000"),
        },
        {
            "analyte": "nitrate",
            "unit": "mg/L",
            "readings": 2,
            "populated": 1,
            "total": Decimal("0.015"),
        },
    ]


def _stations(common):
    return common.load_stations(ANALYTICS / "stations.json")


def test_duckdb_summary_matches_expected(tmp_path):
    module = _duckdb_observations()
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    table = contextual.table(document, selector="observation", revalidate=True)
    assert module.summarize_table(table, _stations(common)) == _expected_summary()
    path = tmp_path / "observations.parquet"
    contextual.write_parquet(document, path, selector="observation", batch_size=2)
    assert module.summarize_parquet(path, _stations(common)) == _expected_summary()


def test_duckdb_quality_filter_sequences():
    module = _duckdb_observations()
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    table = contextual.table(document, selector="observation", revalidate=True)
    assert module.passing_sequences(table) == [1, 4]


def test_duckdb_station_join_preserves_rows():
    module = _duckdb_observations()
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    table = contextual.table(document, selector="observation", revalidate=True)
    assert module.with_regions(table, _stations(common)) == [
        {"sequence": 1, "station": "RIVER-UPSTREAM", "region": "river"},
        {"sequence": 2, "station": "RIVER-DOWNSTREAM", "region": "river"},
        {"sequence": 3, "station": "RIVER-UPSTREAM", "region": "river"},
        {"sequence": 4, "station": "COASTAL-ESTUARY", "region": "coastal"},
    ]


def test_duckdb_station_join_unknown_station():
    module = _duckdb_observations()
    common = load_common()
    synthetic = pa.table(
        {"sequence": pa.array([9], type=pa.uint32()), "station": pa.array(["POLAR-1"])}
    )
    assert module.with_regions(synthetic, _stations(common)) == [
        {"sequence": 9, "station": "POLAR-1", "region": "unknown"}
    ]


def test_duckdb_child_expansion_counts():
    module = _duckdb_observations()
    common = load_common()
    _, document, nested, contextual = common.prepare_observations()
    nested_table = nested.table(document, selector="observation", revalidate=True)
    assert module.child_row_counts(nested_table) == {"tags": 4, "replicates": 8}
    # Child expansion never touches the record-level totals.
    table = contextual.table(document, selector="observation", revalidate=True)
    assert module.summarize_table(table, _stations(common)) == _expected_summary()


def test_duckdb_nested_nil_struct_retains_unit():
    module = _duckdb_observations()
    common = load_common()
    _, document, nested, _ = common.prepare_observations()
    nested_table = nested.table(document, selector="observation", revalidate=True)
    rows = module.reading_units(nested_table)
    assert [row["sequence"] for row in rows] == [1, 2, 3, 4]
    assert rows[2] == {"sequence": 3, "unit": "mg/L", "is_nil": True}
    assert all(row["is_nil"] is False for row in rows[:2] + rows[3:])


def test_duckdb_rejects_out_of_profile_decimal():
    module = _duckdb_observations()
    common = load_common()
    wide = pa.table({"wide": pa.array([1], type=pa.decimal256(76, 6))})
    with pytest.raises(ValueError, match="38"):
        module.summarize_table(wide, _stations(common))


def test_duckdb_parquet_path_with_spaces_and_quotes(tmp_path):
    module = _duckdb_observations()
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    directory = tmp_path / "dir with spaces"
    directory.mkdir()
    path = directory / 'it\'s "quoted".parquet'
    contextual.write_parquet(document, path, selector="observation", batch_size=2)
    assert module.summarize_parquet(path, _stations(common)) == _expected_summary()


def _polars_observations():
    require("polars")
    return load_example_module("polars_observations")


def test_polars_summary_matches_expected(tmp_path):
    module = _polars_observations()
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    table = contextual.table(document, selector="observation", revalidate=True)
    assert module.summarize_table(table, _stations(common)) == _expected_summary()
    path = tmp_path / "observations.parquet"
    contextual.write_parquet(document, path, selector="observation", batch_size=2)
    assert module.summarize_parquet(path, _stations(common)) == _expected_summary()


def test_polars_quality_filter_sequences():
    module = _polars_observations()
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    table = contextual.table(document, selector="observation", revalidate=True)
    assert module.passing_sequences(table) == [1, 4]


def test_polars_station_join_preserves_rows():
    module = _polars_observations()
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    table = contextual.table(document, selector="observation", revalidate=True)
    assert module.with_regions(table, _stations(common)) == [
        {"sequence": 1, "station": "RIVER-UPSTREAM", "region": "river"},
        {"sequence": 2, "station": "RIVER-DOWNSTREAM", "region": "river"},
        {"sequence": 3, "station": "RIVER-UPSTREAM", "region": "river"},
        {"sequence": 4, "station": "COASTAL-ESTUARY", "region": "coastal"},
    ]


def test_polars_station_join_unknown_station():
    module = _polars_observations()
    common = load_common()
    synthetic = pa.table(
        {"sequence": pa.array([9], type=pa.uint32()), "station": pa.array(["POLAR-1"])}
    )
    assert module.with_regions(synthetic, _stations(common)) == [
        {"sequence": 9, "station": "POLAR-1", "region": "unknown"}
    ]


def test_polars_child_expansion_counts():
    module = _polars_observations()
    common = load_common()
    _, document, nested, contextual = common.prepare_observations()
    nested_table = nested.table(document, selector="observation", revalidate=True)
    assert module.child_row_counts(nested_table) == {"tags": 4, "replicates": 8}
    table = contextual.table(document, selector="observation", revalidate=True)
    assert module.summarize_table(table, _stations(common)) == _expected_summary()


def test_polars_nested_nil_struct_retains_unit():
    module = _polars_observations()
    common = load_common()
    _, document, nested, _ = common.prepare_observations()
    nested_table = nested.table(document, selector="observation", revalidate=True)
    rows = module.reading_units(nested_table)
    assert [row["sequence"] for row in rows] == [1, 2, 3, 4]
    assert rows[2] == {"sequence": 3, "unit": "mg/L", "is_nil": True}
    assert all(row["is_nil"] is False for row in rows[:2] + rows[3:])


def test_polars_rejects_out_of_profile_decimal():
    module = _polars_observations()
    common = load_common()
    wide = pa.table({"wide": pa.array([1], type=pa.decimal256(76, 6))})
    with pytest.raises(ValueError, match="38"):
        module.summarize_table(wide, _stations(common))


def test_polars_parquet_path_with_spaces_and_quotes(tmp_path):
    module = _polars_observations()
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    directory = tmp_path / "dir with spaces"
    directory.mkdir()
    path = directory / 'it\'s "quoted".parquet'
    contextual.write_parquet(document, path, selector="observation", batch_size=2)
    assert module.summarize_parquet(path, _stations(common)) == _expected_summary()


def test_polars_post_import_dtypes_and_values():
    pl = require("polars")
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    frame = pl.from_arrow(contextual.table(document, selector="observation", revalidate=True))
    assert frame.schema["accession"] == pl.String
    assert frame.schema["collected"] == pl.String
    assert frame.schema["sequence"] == pl.UInt32
    assert frame.schema["quality"] == pl.Boolean
    assert frame.schema["reading"] == pl.Decimal(precision=8, scale=3)
    assert frame["accession"].to_list() == [
        "20261001001",
        "20261001002",
        "20261001003",
        "123456789012345678901234567890",
    ]
    assert frame["collected"].to_list()[3] == "2026-10-01T12:45:00.123456789Z"


def test_polars_lazy_parquet_summary(tmp_path):
    module = _polars_observations()
    common = load_common()
    _, document, _, contextual = common.prepare_observations()
    path = tmp_path / "observations.parquet"
    contextual.write_parquet(document, path, selector="observation", batch_size=2)
    assert module.summarize_parquet(path, _stations(common)) == _expected_summary()
