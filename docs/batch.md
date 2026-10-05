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
