# Analytical workflows (DuckDB and Polars)

Executable consumer examples over the schema-derived Arrow projection of
[`../arrow/observations.xml`](../arrow/observations.xml). See the
[data workflow guide](../../docs/data-workflows.md) for the full narrative,
expected outputs, and caveats.

- `common.py` prepares the shared inputs: the nested record projection and the
  contextual scalar columns, plus the station catalog loader and the
  consumer-compatibility preflight.
- `duckdb_observations.py` and `polars_observations.py` expose matching
  `summarize_table` / `summarize_parquet` functions, a left join to
  `stations.json`, quality filtering (`[1, 4]`), and separate tag/replicate
  expansion (four and eight child rows) without multiplying the reading
  totals (`12.000`, `0.015`).
- `stations.json` is application reference data — three station identifiers
  mapped to region labels — not XSD identity semantics. Duplicate keys fail
  before joining; unknown stations keep their row with region `unknown`.

## Prerequisites

```bash
# From a repository checkout (each line is an alternative — uv sync is exact):
uv sync --extra arrow --group examples-duckdb
uv sync --extra arrow --group examples-polars
# or both consumers together:
uv sync --extra arrow --group examples-duckdb --group examples-polars
```

Or install the published extra plus a consumer:

```bash
pip install 'pyxsd[arrow]' duckdb
pip install 'pyxsd[arrow]' polars
```

## Run

```bash
python examples/analytics/duckdb_observations.py
python examples/analytics/duckdb_observations.py --write-parquet observations.parquet
python examples/analytics/duckdb_observations.py --parquet observations.parquet

python examples/analytics/polars_observations.py
python examples/analytics/polars_observations.py --write-parquet observations.parquet
python examples/analytics/polars_observations.py --parquet observations.parquet
```

The `--parquet` runs summarize an existing export without re-reading the XML.
Scripts resolve their fixtures relative to their own file, so they also run
from any working directory, and the DuckDB path access is parameterized for
directories containing spaces or quotes.
