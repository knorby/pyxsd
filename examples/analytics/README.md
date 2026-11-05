# Analytical workflows (DuckDB and Polars)

Query schema-derived Arrow records from
[`observations.xml`](../arrow/observations.xml) with DuckDB or Polars.
The commands below show setup, expected output, and consumer limits.
See the [integration guide](../../docs/integrations.md) for conversion rules.

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

Run these commands from the repository root, in the environment prepared above:

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

## Inputs and record shapes

`common.py` prepares four observations from the committed
[`observations.xsd`](../arrow/observations.xsd) and XML pair. No downloads are
required. The nested projection retains `reading` as a struct (`$nil`, `@unit`,
`$`) and `tag`/`replicates` as lists. Scalar contextual columns carry `station`,
`sequence`, `accession`, `collected`, `analyte`, `reading`, `unit`, and `quality`.

`accession` (an arbitrary-size integer) and `collected` (an XSD dateTime with
nanosecond lexical precision) remain strings. Convert them explicitly if your
analysis needs numeric IDs or instants. `reading` is exact `decimal128(8, 3)`,
not floating point. The station catalog is application data, not an XSD keyref
across files.

## Expected output

Both XML paths print:

```text
--- nested struct and list demonstration (XML path only) ---
Reading units: [(1, 'mg/L'), (2, 'mg/L'), (3, 'mg/L'), (4, 'mg/L')]
Child row counts: {'tags': 4, 'replicates': 8}
Station regions: [(1, 'river'), (2, 'river'), (3, 'river'), (4, 'coastal')]
Passing quality sequences: [1, 4]
--- summary ---
dissolved oxygen [mg/L] readings=2 populated=2 total=12.000
nitrate [mg/L] readings=2 populated=1 total=0.015
```

The nil nitrate reading keeps its unit. Tags and replicates expand separately
from the four-record relation used for totals; expansion therefore does not
multiply `12.000` or `0.015`. Grouping includes the unit to avoid summing unlike
measurements. Parquet readback prints the same summary without parsing XML.

## Consumer limits and failures

The examples were tested with DuckDB 1.5.6, Polars 1.44.2, and PyArrow 25.0.1.
The Arrow extra supports PyArrow >=22; verify the types delivered by your
consumer version. The Polars path checks strings, unsigned sequence integers,
booleans, `Decimal(8, 3)`, and representative exact values after import.

- `assert_consumer_profile` rejects decimals wider than precision 38 with a
  field-specific error. It does not silently cast `decimal256` to floats or strings.
- Duplicate station keys raise `ValueError` before joining. Unknown stations
  retain their row with region `unknown`.
- Empty/null lists produce zero child rows. Polars uses
  `explode(empty_as_null=False).drop_nulls()` explicitly.
- Lazy Parquet scanning is not streaming XML parsing. Preparing the original
  Arrow table materializes the XML document.
- The DuckDB Parquet path uses parameterized `read_parquet($path)`, including
  paths with spaces or quotes.

The tests in `tests/test_data_workflows_docs.py` run these commands against
fixed expected values; consumer-specific CI requires the optional dependencies.
