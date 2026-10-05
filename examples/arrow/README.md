# Schema-typed environmental observations to Parquet

This example extracts four fictional water-quality observations into a nested
Arrow table and writes them to Parquet in two-row batches. **The XSD determines
the columns and their types before any XML rows are read.** It does not infer
types from a dictionary export or flatten nested records into unrelated columns.

The synthetic dataset covers upstream/downstream/estuary stations, coordinates,
exact concentrations, quality flags, replicate readings, repeated tags, a nil
measurement with its unit preserved, and a large accession number. No network
access, pandas, or Pydantic is required.

## Run

From the repository root, with pyxsd installed:

```bash
pip install 'pyxsd[arrow]'
python examples/arrow/demo.py observations.parquet
```

For an editable checkout use `pip install -e '.[arrow]'`. The destination's
parent directory must exist. The demo reads data relative to its script, so
it also runs from another working directory. It replaces the requested local
destination only after successful Parquet writer closure.

## Follow the data flow

1. **Compile `observations.xsd`.** Its types describe the nested location,
   decimal measurement with a unit attribute, list-valued replicates, and
   repeated tags. The root is a collection, not itself a table row.
2. **Select the record declaration.**

   ```python
   projection = records(schema, element="observations", path=("observation",))
   print(projection.schema)  # Exists before parsing observations.xml
   ```

3. **Select matching bound occurrences.**

   ```python
   document = schema.parse(DATA / "observations.xml")
   table = projection.table(document, selector="observation", revalidate=True)
   ```

   The path identifies the local declaration; the selector identifies actual
   occurrences in this document. A selected node with a different declaration
   is an error, not a row silently converted to nulls.
4. **Inspect the schema-derived representation.**

   | Column | Arrow type | Why |
   | --- | --- | --- |
   | `sequence` | uint32 | XSD `xs:unsignedInt` bounds |
   | `accession` | string | Arbitrary XSD integers can exceed 64 bits |
   | `collected` | string | Preserve XSD precision and timezone offsets |
   | `location` | struct of decimal128(11,5) | Named nested XSD complex type |
   | `reading` | struct with decimal128(8,3), `@unit`, `$nil` | Nillable simple-content shell |
   | `replicates` | list of decimal128(8,3) | One XSD list-valued scalar |
   | `quality` | bool | XSD boolean, not inferred integer |
   | `tag` | list of string | Repeated child declarations, including empty lists |

   Decimal capacity is deliberately conservative: `totalDigits=5` and
   `fractionDigits=3` need precision 8 at scale 3. XSD does not mean “only two
   integer digits” here. Values are never rounded through binary floats.

   The fourth accession stays `"123456789012345678901234567890"`, and its
   timestamp stays `"2026-10-01T12:45:00.123456789Z"`. The offline nitrate
   reading retains `{"$nil": True, "@unit": "mg/L", "$": None}`. Missing
   replicates become null; no tags become an empty list.
5. **Write batches and verify real readback.**

   ```python
   projection.write_parquet(document, destination, selector="observation", batch_size=2)
   restored = pyarrow.parquet.read_table(destination)
   ```

   The writer projects batches directly; it does not write the already
   materialized demonstration table. The demo verifies restored values and
   schema against that table and prints `Parquet readback: 4 rows`.
6. **Select zero rows without losing the schema.**

   ```python
   empty = projection.table(document, selector="observation[@station='absent']")
   ```

   It reports zero rows with exactly the same declared schema. There is no
   first-row inference step to fail or accidentally choose the wrong type.

## Fidelity and memory boundaries

The preserved structure is nested, not relational: no implicit list explosion,
joins, or Cartesian products. Schema metadata records declaration and facet
information. Arrow intentionally loses some XML presence distinctions, such
as explicit versus defaulted attributes; it is not an XML round-trip format.

The parser materializes the XML tree, and selection materializes its node
list. `batch_size` bounds projected row count, not total memory: this is not
streaming XML parsing. The demo additionally materializes a table to make
inspection/readback comparisons easy; a production export can call only the
writer. Full XML validation and projected type validation remain distinct.

See [the integration guide](../../docs/integrations.md) for the complete policy.
`tests/test_optional_examples.py` checks numeric types, list/struct values,
nil attributes, two-row batches, empty selections, and actual Parquet readback.

For a consumer workflow over the contextual scalar columns — DuckDB and Polars
summaries, child expansion, a station/region join, and a Parquet round trip —
see [`examples/analytics/`](../analytics/README.md) and the
[data workflow guide](../../docs/data-workflows.md).
