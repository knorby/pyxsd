# Data workflow examples

Choose a workflow by its output. pyxsd validates and binds XML; your application
decides how to aggregate values, join reference data, or apply business policy.
See {doc}`integrations` for conversion rules and supported shapes. Each family
README supplies runnable commands, expected results, and sample-specific
limitations.

## Choose a workflow

| Task | Example | Result |
| --- | --- | --- |
| Validate Python input or read typed order snapshots | [Purchase-order models](https://github.com/knorby/pyxsd/tree/develop/examples/pydantic#readme) | Pydantic models, JSON Schema, explicit presence |
| Apply approval rules after XML validation | [Order assessment](https://github.com/knorby/pyxsd/tree/develop/examples/pydantic#order-assessment) | Exact subtotals and reason codes |
| Extract nested XML records or scalar columns | [Arrow and Parquet](https://github.com/knorby/pyxsd/tree/develop/examples/arrow#readme) | Typed tables, record batches, Parquet |
| Aggregate observations and join station metadata | [DuckDB and Polars](https://github.com/knorby/pyxsd/tree/develop/examples/analytics#readme) | Grouped totals, child expansion, Parquet readback |
| Validate or export several XML files | {doc}`batch` | Per-file outcomes or a manifested dataset |
| Convert clinical documents or diagnose imperfect input | [Healthcare](https://github.com/knorby/pyxsd/tree/develop/examples/healthcare#readme) | Scoped FHIR XML and source-specific diagnostics |

(install)=
## Prerequisites

These examples require a repository checkout; a package installation alone does
not supply the input files or scripts. Each family documents its optional extras
and consumer dependencies. Install only what that workflow needs. With `uv sync`,
name all groups you intend to keep: synchronization removes groups omitted from
the command.

(shared-preparation)=
## Choose the record shape before analysis

Nested Arrow records preserve child structs and lists. Scalar columns can carry
parent context into one analytical row. Choose the declaration from the schema,
then select its occurrences in the document; do not infer a table from the
first dictionary row. See {doc}`integrations` for declaration paths and
`FieldSource` columns.

Expand child lists separately from parent totals to avoid multiplying measures.
Keep identifiers and temporal values in their exact exported forms until your
application explicitly chooses a numeric width, timezone, and precision.
DuckDB/Polars compatibility is narrower than Arrow's type system; the analytics
example checks its consumer profile before querying.

(duckdb-workflow)=
(polars-workflow)=
The [analytics README](https://github.com/knorby/pyxsd/tree/develop/examples/analytics#readme)
contains both consumer walkthroughs and their expected outputs.

(order-assessment)=
The [purchase-order README](https://github.com/knorby/pyxsd/tree/develop/examples/pydantic#order-assessment)
contains approval thresholds, rejection cases, and the assessment commands.

(caveats-and-failure-modes)=
## Check validity and application policy separately

Revalidate edited documents before conversion. A Pydantic snapshot is independent
of its XML source; changing it does not update XML. An Arrow table is a data
export, not a lossless XML representation. Output batching limits projected rows,
not parser memory: each XML document is still materialized as a tree.

Malformed XML, XSD findings, unsupported adapter shapes, and business rejection
are different failures. Report them separately so an invalid document cannot
appear to be a valid order rejected only by policy. Base healthcare XSD validity
also does not establish clinical, terminology, template, or profile conformance.

(why-analysis-stays-outside-the-library)=
## Library and application responsibilities

pyxsd handles schema interpretation, supported validation checks, and typed
conversion. SQL/dataframe execution, joins, catalog ownership, exchange rates,
and approval rules belong to the application. Keeping those responsibilities
separate lets each example state which checks it actually performs.
