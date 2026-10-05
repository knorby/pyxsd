# Data workflow examples

pyxsd makes XML schema-valid and schema-typed. It does not do your analysis.
These examples show the boundary in three end-to-end paths, each with runnable
code in this repository:

- **XML to typed application records** — validate the document, snapshot
  generated Pydantic models, and apply business rules with exact arithmetic.
- **XML to nested or contextual Arrow to analysis** — prepare a nested record
  projection, or named scalar columns with parent context, and query them with
  DuckDB or Polars.
- **Parquet to analysis** — repeat the same queries from a Parquet export
  without re-reading the XML.

Schema interpretation, exact decimals, nil/presence semantics, and document
validation stay in the library. Aggregations, joins, business policy, and
consumer-specific type compatibility stay in application code.

## Install

The library is unchanged by these examples. Install only the extras you need:

```bash
pip install 'pyxsd[arrow]' duckdb
pip install 'pyxsd[arrow]' polars
pip install 'pyxsd[pydantic]'
```

From a repository checkout, the `examples-*` dependency groups provide the same
consumer environments without adding DuckDB or Polars to the published package.
Each line is an alternative: `uv sync` makes the environment match exactly the
groups you name, so a second sync without the first group removes it.

```bash
uv sync --extra arrow --group examples-duckdb                    # DuckDB workflow
uv sync --extra arrow --group examples-polars                    # Polars workflow
uv sync --extra arrow --group examples-duckdb --group examples-polars  # both
uv sync --extra pydantic                                         # assessment app
```

Tested with DuckDB 1.5.6 and Polars 1.44.2 against PyArrow 25.0.1; the Arrow
extra supports PyArrow >=22, and the compatible versions are verified at
implementation time rather than treated as universal.

## Shared preparation

`examples/analytics/common.py` prepares the same four observation records two
ways, from the unchanged `examples/arrow/observations.xsd` and
`observations.xml` fixtures:

- a **nested projection** for struct and list handling, where `reading` is a
  struct with `$nil`, `@unit`, and `$` fields and `tag`/`replicates` are lists;
- a **contextual projection** of scalar columns — `station`, `sequence`,
  `accession`, `collected`, `analyte`, `reading`, `unit`, `quality` — suited
  to aggregation and joins.

The two representations are complementary. The analytical examples use the
scalar table for totals and keep the nested table for child expansion, so
expanding tags and replicates never multiplies the original reading totals.

Two representations follow library policy rather than consumer convenience:

- `accession` (an arbitrary-size `xs:integer`) and `collected` (`xs:dateTime`,
  including a nanosecond lexical timestamp) stay **strings**. There is no
  silent numeric narrowing or timezone conversion. Convert explicitly in your
  application if you need numbers or instants.
- `reading` is an exact `decimal128(8, 3)` column (XSD `totalDigits` +
  `fractionDigits`), not a float. Decimal sums are compared exactly before any
  formatting.

A checked-in `examples/analytics/stations.json` maps three station identifiers
to plain region labels. The join enforces one catalog entry per key; duplicate
keys fail before joining, and an unknown station still produces a row with an
explicit `unknown` region.

## DuckDB workflow

From the repository root:

```bash
python examples/analytics/duckdb_observations.py
```

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

What the output shows:

- The nil nitrate reading keeps its `mg/L` unit even though its value is null
  (`reading['$nil']` is true, `reading['@unit']` is still `mg/L`).
- Four tag values and eight replicate values expand from the separate nested
  relation; the analyte totals still come from the unexpanded four-record
  table, so `12.000` and `0.015` are not multiplied.
- `quality` filtering selects sequences `[1, 4]`, excluding the nil reading.
- Grouping is by analyte **and** unit; units are never mixed.

Export a Parquet file and query it back without re-reading the XML:

```bash
python examples/analytics/duckdb_observations.py --write-parquet observations.parquet
python examples/analytics/duckdb_observations.py --parquet observations.parquet
```

The Parquet path uses DuckDB's parameterized file access
(`read_parquet($path)`); paths are never interpolated into SQL, so directories
with spaces or quotes are safe. The `--parquet` run prints the same summary and
reads no XML.

## Polars workflow

```bash
python examples/analytics/polars_observations.py
```

The eager path imports the same Arrow table with `pl.from_arrow` and produces
the same summary lines. The Polars module also checks the imported dtypes
(`accession`/`collected` strings, `sequence` unsigned integer, `quality`
boolean, `reading` `Decimal(8, 3)`) and representative exact values, because
Polars documents fallback casting for some unsupported Arrow types: verify the
types your version actually delivers instead of assuming zero-copy fidelity.

The lazy Parquet companion scans without materializing the XML again:

```bash
python examples/analytics/polars_observations.py --write-parquet observations.parquet
python examples/analytics/polars_observations.py --parquet observations.parquet
```

List expansion normalizes empty and null lists to zero child rows explicitly
(`explode(empty_as_null=False).drop_nulls()`), so both consumers agree on the
documented counts. Lazy Parquet is not lazy XML parsing — the XML is fully
parsed when the Arrow table is prepared.

Both consumers agree on independently enumerated expected values, not merely
with each other: the tests assert the literal expected results, not cross-tool
equality.

## Order assessment

`examples/pydantic/assess_orders.py` parses and validates `orders.xml`,
snapshots generated Pydantic models, and applies synthetic policy in one order
currency:

```bash
python examples/pydantic/assess_orders.py
```

Each order reports an order number, a disposition (`accepted`,
`approval_required`, or `rejected`), its currency, an exact subtotal as a JSON
string, and deterministic reason codes. With the synthetic catalog fully
recognized:

- `PO-2026-1041` — USD `681.90`, `approval_required` (above the
  `600.00` threshold);
- `PO-2026-1042` — EUR `450.00`, `accepted`;
- `PO-2026-1043` — USD `100.00`, `accepted` (exactly at the boundary is
  accepted; only strictly greater requires approval).

The rejection and boundary cases live in `orders_review.xml`:

```bash
python examples/pydantic/assess_orders.py examples/pydantic/orders_review.xml
```

- `PO-2026-2001` — unknown account `NWL-99`, rejected, subtotal still reported;
- `PO-2026-2002` — unknown SKU `GADGET-X`, rejected, subtotal still reported;
- `PO-2026-2003` — a USD line and a EUR line, rejected as `mixed_currencies`
  with no currency and no aggregate subtotal (no exchange rate is invented);
- `PO-2026-2004` — USD `600.00`, accepted at the exact threshold.

The account list, SKU list, and `600.00` approval threshold are **synthetic
policy fixtures for this example**. They are not XSD constraints, not a
complete catalog, and not financial advice. XSD validity and business
acceptance are separate: an XSD-valid order can be rejected by policy, and
invalid XML or model input never reaches policy evaluation at all.

Absent and explicitly nil notes are different states. `model_fields_set`
distinguishes a nil `<note/>` from an omitted element, and the example never
mutates the source XML.

## Caveats and failure modes

**Out-of-profile decimals are rejected, not cast.** The shared
`assert_consumer_profile` check accepts exact decimals with precision <= 38
and rejects wider fields (for example `decimal256(76, 6)`) with an error
naming the field, its type, and the rule. There is no silent float or string
fallback, and no claim that every PyArrow type is DuckDB- or
Polars-compatible.

**Duplicate catalog keys fail before joining.** `load_stations` raises a
`ValueError` naming the duplicate station. One catalog entry per key is a
precondition, not a dedupe step.

**Malformed XML and policy rejection are different outcomes.** A
not-well-formed document raises `pyxsd.exceptions.PyXSDError` during parse; an
XSD-invalid document (for example `quantity` of zero) parses but raises
`pyxsd.exceptions.ValidationError` when the snapshot revalidates. Both happen
before any business rule runs, so invalid input never appears as a
rejected-but-valid order.

**Temporal and identifier conversion is explicit.** Strings stay strings.
Parse timestamps or convert large integers in your own code, where the
precision and timezone decisions belong to your application.

## Why analysis stays outside the library

pyxsd deliberately does not embed SQL execution, dataframe engines, joins,
business rules, exchange rates, or user-supplied expressions in the library or
its integrations. The library delivers schema-typed values with honest
validation; consumers decide what the values mean. That boundary keeps the
base dependency surface at `elementpath`, keeps Arrow and Pydantic
independently optional, and lets DuckDB, Polars, and application policy evolve
on their own schedules.
