# Why pyxsd

```{eval-rst}
.. meta::
   :description: Use XSD as your Python data model: runtime binding classes, extensible validation, document queries, and schema-derived Pydantic and Arrow exports.
```

pyxsd is built for applications that need to **work with schema-defined XML
as Python data**. Compile an XSD once, then parse documents into typed
objects, inspect validation findings, and use the same bound tree for
queries, edits, and transforms. The schema supplies the structure; your
Python code supplies the application behavior.

## Let the schema build the classes

`Schema.compile()` creates Python classes in memory from XSD declarations.
Schema extensions become Python inheritance, declared elements and
attributes get descriptors, and simple values use XSD datatype classes.
You can reuse that compiled schema across documents without a code-generation
command, generated modules to check in, or a second model definition to keep
in sync with the XSD.

This matters when you consume upstream schemas, handle several schema
versions, or want to start processing a new vocabulary immediately. Recompile
the chosen schema to build its bindings. Changes can still require changes
to application code, but there is no generated-source refresh step.

For editor support, `Schema.stub()` emits optional `.pyi` descriptions of
the runtime classes. Stubs supplement the bindings; they do not implement
them. See {doc}`stubs` for typing limits and {doc}`architecture` for the
compiler model.

## Keep validation and application work together

A parsed `Document` carries both its bound tree and a `ValidationReport`.
Recoverable findings accumulate with stable codes and schema/instance phase
tags. Call `require_valid()` where your application needs to reject errors,
or inspect the report to decide what to repair. Unreadable or malformed XML
can still stop a run before a document is returned.

Binding retention is a separate choice. Lax retention can keep invalid
lexical values as raw text and undeclared content as generic nodes while
validation findings remain visible. That is useful for import review and
repair tools: inspecting the bad data need not mean suppressing its errors.
Retained trees are best effort, not a lossless source copy. See {doc}`binding`.

Findings with source locations identify indexed occurrences, so repeated
siblings can be distinguished even when their values are identical.
`Document.locate()` connects a finding to a bound node or its source-only
occurrence when available. Location precision is explicit; missing evidence
is not filled with guessed line numbers. See {doc}`validation`.

## Query and extend the objects you already have

`find()`, `findall()`, and `xpath()` return the original bound nodes for
element results. You can select an inventory item, read its typed quantity,
and pass that object to ordinary Python functions. Callable transforms work
on the same model; after editing, `revalidate()` returns a freshly checked
document for writing. The {doc}`quickstart` walks through that complete path.

Mixin extensions add your methods to selected schema-generated classes at
compile time. A `__pyxsd_validate__` hook can contribute application findings
to the document's report alongside XSD findings. You keep handwritten
behavior separate from the schema-derived structure, without editing
generated files. See {doc}`extensions` and {doc}`transforms/using`.

## Derive application models and tables from XSD

Optional Pydantic and Arrow integrations prepare models and record schemas
from the compiled XSD before instance XML is available. An empty record
selection still has a declared Arrow schema; column types do not depend on
which values happen to appear in the first file.

Use Pydantic snapshots for Python/JSON application boundaries, or select
records for Arrow tables and Parquet. Named columns can include values from
the record's ancestors or document root, such as an order number beside
each line item. These are explicit projections with supported-shape limits,
not lossless XML codecs or substitutes for full XML validation. See
{doc}`integrations` and {doc}`data-workflows`.

## Use it in Python, pipelines, and agent tools

The base library is pure Python with two dependencies, `elementpath` and
`defusedxml`; it needs no compiled extension. Optional integrations bring
their own dependencies. The CLI exposes validation, transforms, and stub
emission, while the optional local MCP server adds schema exploration,
source-linked diagnostics, queries, and exports for agent clients. See
{doc}`cli` and {doc}`mcp`.

pyxsd defaults to XSD 1.1 and offers an explicit XSD 1.0 mode. The published
{doc}`xsts` results make validity-verdict agreement inspectable through a
pinned W3C corpus, reviewed baselines, and reproduction commands. Use
{doc}`supported` to check the constructs your application needs; test-suite
agreement is evidence about those verdicts, not a claim of certification.

## Choose for your workflow

Choose pyxsd when you want the XSD to drive a Python object model and the
work that follows validation. Start with {doc}`quickstart`, or compare
{doc}`comparison-xmlschema` for converter-based encoding and decoding and
{doc}`comparison-libxml2` for native-validator scope and measured evidence.

Check the boundaries before adopting it: pyxsd builds a full instance tree,
parses one document at a time per compiled `Schema`, and writes bound trees
rather than providing a general Python/JSON-to-XML encoder. Some parsed
complex-child descriptors require document queries. Namespace-aware binding
is opt-in with `ParseModes.NAMESPACED`. For untrusted input, select parser
protections and resource rules explicitly using {doc}`xml-security`;
local-only loading is not filesystem confinement.
