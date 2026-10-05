# Sequential multi-document parsing

Compile an XSD once and parse an explicit sequence of local XML files:

```python
from pyxsd import Schema
from pyxsd.batch import DocumentSource

schema = Schema.compile("records.xsd")
sources = [DocumentSource("first", "a.xml"), DocumentSource("second", "b.xml")]
for outcome in schema.iter_parse(sources, errors="report"):
    print(outcome.source.id, outcome.status)
    for issue in outcome.issues:
        print(issue.phase, issue.format())
    if outcome.error is not None:
        print(outcome.error.kind, outcome.error.message)
```

This API adds no dependencies. An ordinary loop over `Schema.parse` remains
supported, including its broader stream and parsed-element inputs.

## Sources and outcomes

`DocumentSource(id, path)` requires a nonempty string ID and a text path
(`str` or `os.PathLike[str]`). It anchors relative paths to the current
working directory at construction; the file need not exist yet. Changing
directories during iteration does not change those paths. Inline XML, URLs,
streams, directory discovery, and parsed elements are not batch inputs.

IDs are caller labels, not filenames, hashes, or XSD identities. Duplicate
source IDs stop iteration with `ValueError` when encountered, before opening
the duplicate. Different IDs may refer to the same path: it is parsed twice.
XSD keys/keyrefs are checked independently within each document. A repeated
business ID in another document is legal; a keyref cannot resolve across files.

Each frozen `ParseOutcome` carries its source and one of these statuses:

| Status | Document | Issues | Error |
| --- | --- | --- | --- |
| `valid` | Parsed Document | All issues, including warnings | `None` |
| `invalid` | Parsed Document (possibly without a bound root) | All validation issues | `None` |
| `input_error` | `None` | Empty tuple | `InputFailure` |

Issues are tuple snapshots of the native report. Their `phase` identifies
schema-compilation or instance-binding findings; see {doc}`validation` for
codes and severities. The record is frozen, not its contained Document.
Later mutations need explicit `Document.revalidate()` under the existing API.
Earlier Documents remain usable after later parses, with the same native
query/export/serialization limitations as single-document parsing.

`InputFailure(kind, message)` stores text, not an exception or traceback.
Its kind is `io` for expected file I/O failures or `malformed_xml` for XML
syntax errors. Source paths are absolute runtime inputs; avoid exposing them
in exported records by default. Native I/O error messages may also include
paths, so redact them before publishing diagnostics.

## Error timing and policies

`Schema.iter_parse(sources, *, errors="raise")` returns a lazy iterator.
Policy validation and `schema.require_valid()` run on first iteration,
before pulling any source, even for an empty iterable. An invalid schema is
a global `ValidationError`, not a repeated per-file failure. Compilation
always precedes the batch; instance schema-location hints do not select a
different schema.

The default `raise` policy stops at the first invalid document or expected
input failure. It raises `pyxsd.batch.BatchParseError`, a `PyXSDError`
subclass, with the failed result in `.outcome`. Its native validation/input
exception is chained as `__cause__`. The failed outcome is not yielded, and
later inputs are not requested.

The `report` policy yields those failed outcomes and continues. Neither
policy suppresses internal binding defects, unclassified `PyXSDError`s,
input-iterator exceptions, interrupts, or cancellation. Invalid policies,
non-`DocumentSource` inputs, and duplicate IDs are configuration errors,
not document outcomes.

## Resources and limits

Processing is sequential in caller order, with no prefetch. Do not parse on
the same Schema concurrently or reentrantly while its iterator is active.
The API offers no scheduler, shared-schema concurrency, or XML streaming:
each file still becomes a materialized tree.

Files are closed before results are yielded. The iterator keeps no list of
Documents or outcomes, only current input state and an O(number of inputs)
ID set. The native compiler host may keep bounded state from its latest
parse. Retaining outcomes intentionally retains their Documents; an exception
caught in raise mode can also retain trees through its traceback. Report-mode
failure records contain no live tracebacks. Close a partially consumed
iterator to release its own references:

```python
from contextlib import closing

with closing(schema.iter_parse(sources, errors="report")) as outcomes:
    first = next(outcomes)
```

No constant-RSS guarantee is made: tree size and Python allocator behavior
still matter.

## Runnable validation example

From a repository checkout:

```bash
uv run python examples/batch/validate_documents.py records.xsd inputs b.xml a.xml
```

The example accepts explicit filenames relative to the input root, sorts
their relative IDs, and uses namespaced binding. It prints each observed
result and exits **0** for all valid inputs, **1** for invalid/unreadable/
malformed inputs, or **2** for configuration, schema, or internal failures.
Unexpected defects include a traceback. Sorting is application behavior;
the library never discovers or sorts files.

## Optional Parquet dataset export

Install `pyxsd[arrow]`, prepare one projection, and give it explicit sources:

```python
from pyxsd import ParseModes, Schema
from pyxsd.batch import DocumentSource
from pyxsd.integrations.arrow import records

schema = Schema.compile("examples/arrow/observations.xsd", mode=ParseModes.NAMESPACED)
projection = records(schema, element="observations", path=("observation",))
result = projection.write_dataset(
    [DocumentSource("day-1", "examples/arrow/observations.xml")],
    "new-observation-dataset",
    selector="observation",
    batch_size=10000,
    errors="report",
)
print(result.status, result.rows, result.manifest)
```

`DatasetResult` is a frozen, dependency-free record from
`pyxsd.integrations.dataset`: `destination`, `status`, `inputs`, `succeeded`,
`failed`, `rows`, and `manifest`. A returned result is not a claim that every
source succeeded:

| Overall status | Meaning |
| --- | --- |
| `complete` | No failures, including zero inputs or successful empty selections |
| `partial` | At least one successful input and at least one expected failure |
| `failed` | Attempted inputs all failed; report mode still publishes schema and manifest |

The default `errors="raise"` aborts at the first expected input/validation/
projection failure, does not pull later inputs, removes its staging output,
and publishes nothing. `errors="report"` records those failures and continues.
Input I/O and malformed XML remain distinct from invalid documents and
projection errors. A late projection failure discards **that source's entire
part**, even after earlier batches were written; its row count is zero.
Output write/close/publication failures, unexpected backend/binding defects,
iterator errors and duplicate IDs abort both modes. `DatasetExportError`
extends `IntegrationError`, carries `source_id` (possibly `None`) and `stage`,
and chains the cause. Interrupts/cancellation propagate after cleanup.

### Layout, provenance and manifest version 1

```text
new-observation-dataset/
  manifest.json
  _schema.parquet
  parts/
    part-000001.parquet
    part-000003.parquet
```

One closed part is admitted per successful input, including a typed zero-row
part for an empty selection. Filenames use 1-based input ordinals, never source
IDs or source paths; failed inputs leave gaps. `_schema.parquet` is always a
zero-row reference with the complete schema, not a source part.

Original fields, nullability and metadata are preserved, followed by two
reserved non-null columns: `__pyxsd_source_id` (string) and
`__pyxsd_row_index` (int64). Row indexes start at 1 per source and continue
across batches. Their pair identifies rows within this run/selection, **not a
stable XML node key** after edits or selector changes. Storage/query order is
not guaranteed: order by provenance explicitly. Repeated business keys across
files are allowed; related-table joins/deduplication remain application work.

Manifest JSON includes `format_version=1`, overall `status`, `versions`
(`pyxsd`, `pyarrow`), `xsd_version`, `parse_policy`, `projection` metadata and
`row_route`, `selector`, `namespaces`, `schema_file`, `counts` and ordered
`entries`. Schema/field byte metadata is encoded as lists of
`key_base64`/`value_base64` pairs. The schema reference supplies exact Arrow
types; version/route metadata is not a transitive-XSD fingerprint.
Dataset writers preserve prepared list-child names (typically `item`) instead
of PyArrow's default canonical `element` rename; the Parquet list layout stays
three-level. Consumers must support that legacy child-name convention.

Each entry has `source_id`, `ordinal`, `status`, `rows`, `part`, copied
`issues` (`severity`, `code`, `message`, `element`, `phase`), and `failure`
(null or `kind`/`message`). Entry statuses are `written`, `empty`, `invalid`,
`input_error` or `projection_error`. Failed entries always have `rows=0` and
`part=null`. No Documents, live exceptions, raw XML or dedicated absolute
source paths are serialized. **Diagnostic text can include paths and sensitive
values**: manifests are local artifacts, not sanitized telemetry.

The manifest uses O(inputs + diagnostic text) memory; the writer retains no
list of Documents or batches. Batching bounds projected output row count,
not XML memory: each input still becomes a tree and selection can materialize
nodes. Keep source files unchanged throughout the run and do not share the
Schema with concurrent/reentrant parses. Instance schema hints do not choose
or independently fetch another schema; the prepared schema/resolver policy
governs all inputs.

### Publication and recovery limits

Only a **new local destination** is supported. Its parent must already exist.
Existing files, empty directories, and symlinks (including dangling ones) are
rejected and never removed. There is no append, overwrite, resume, cloud
publication, partition inference or schema merging.

The writer creates an owned sibling `.DESTINATION.pyxsd-*` staging directory
on the same filesystem, closes parts/schema/manifest, rechecks absence, then
renames it. macOS uses `renamex_np(RENAME_EXCL)` and Linux libc with `renameat2`
uses `RENAME_NOREPLACE`; unsupported native/filesystem operations abort rather
than weaken that primitive. Windows `rename` rejects existing targets. Other
POSIX hosts (or Linux libc without `renameat2`) have only checked rename.
**One writer and a stable destination parent are required on every platform**;
the checked fallback is not race-proof. Concurrent/adversarial filesystem
races are outside this API's contract.

Rename provides atomic local visibility, not fsync/power-loss durability or a
distributed transaction. Handled failures remove only owned staging output.
A process kill/crash can leave staging directories: identify a known run's
directory manually, verify that no writer is active, and remove it or inspect
it outside the API. Incomplete staging is not a published dataset and has no
automatic recovery/resume; never delete arbitrary lookalike directories.

### Export and consumption recipes

The observation recipe compiles once and sorts explicit relative IDs. From
a checkout, with no preexisting `observation-dataset`:

```bash
uv run --extra arrow python examples/batch/export_dataset.py examples/arrow observation-dataset observations.xml
```

It exits **0** for complete, **1** for partial/failed, **2** for fatal export
errors (with chained traceback). `--errors raise` stops instead of reporting;
`--batch-size` controls rows per output batch.

Read only the successful `part` paths listed in a **trusted local manifest**,
never recursively glob the dataset. If there are no parts, read
`_schema.parquet` for a typed empty table. The recipe's `read_dataset` function
does this and orders by source/row index. This consumer materializes the whole
dataset; the incremental writer does not.

Reuse the existing analytical observation queries, with consumer libraries
installed only in the example environment:

```bash
uv run --extra arrow --group examples-duckdb python examples/batch/summarize_dataset.py observation-dataset --consumer duckdb
uv run --extra arrow --group examples-polars python examples/batch/summarize_dataset.py observation-dataset --consumer polars
```

These reuse the precision-limited consumer profile in {doc}`data-workflows`;
they add no DuckDB/Polars/pandas library dependency. Generalize the projection
for your schema rather than inferring types from sample XML.
