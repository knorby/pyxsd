# pyxsd

[![CI](https://github.com/knorby/pyxsd/actions/workflows/ci.yml/badge.svg)](https://github.com/knorby/pyxsd/actions/workflows/ci.yml)
[![Docs](https://github.com/knorby/pyxsd/actions/workflows/docs.yml/badge.svg)](https://github.com/knorby/pyxsd/actions/workflows/docs.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/pyxsd/)
[![PyPI](https://img.shields.io/pypi/v/pyxsd)](https://pypi.org/project/pyxsd/)
[![Docs](https://img.shields.io/badge/docs-pyxsd.knorby.com-blue)](https://pyxsd.knorby.com/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)

**pyxsd** maps XML documents into Python object trees according to an XML
Schema (XSD), reports non-fatal validation issues, runs user-defined
*transforms*, and writes the tree back out as XML.

- **Minimal dependencies** — one small, pure-Python package (`elementpath`)
  for XSD regular expressions and XPath queries; no compiled extensions in the base install
- **Optional integrations** — generated Pydantic models and schema-derived
  Arrow tables/Parquet output, independently installed with
  `pyxsd[pydantic]` and `pyxsd[arrow]`; Python 3.11+ remains supported
- **Schema-compiled classes** — your schema becomes real Python classes;
  `xs:extension` becomes real subclassing
- **XSD 1.1 processor** — with an optional XSD 1.0 mode
  (`Schema.compile(xsd, xsd_version="1.0")` / `--xsd-version 1.0`) that
  applies the 1.0 vocabulary gate and semantic differences
- **Lax validation** — bad documents still build a tree; every issue is a
  code-tagged entry in a `ValidationReport` (`--strict` and
  `require_valid()` make it a CI failure)
- **Query and export** — `Document.xpath` / `find` / `findall` return the
  original bound nodes, and `Document.to_dict` / `to_json` export plain
  Python data (also as the `ToDict` transform)
- **Transform pipeline** — apply plain callables or `Transform` classes to
  the bound tree (`document.transform(fn)`); chain them on the CLI
  (`PrintData() > PrintData()`)
- A niche-but-real niche: runtime XML↔Python binding *plus* a transform
  framework, without pulling in `lxml` or generating static code

## Uses

### Validate XML against its schema

Check incoming documents before processing them, validate exports in CI, or
inspect issues in imperfect data. Compile an XSD once, parse multiple documents
against it, and inspect each `document.report`. Call `require_valid()` when
invalid input should stop the workflow; use `revalidate()` to check a tree
after edits.

For an explicit sequence of local files, `Schema.iter_parse` yields structured
per-source outcomes. It stops at the first failure by default; pass
`errors="report"` to report invalid or unreadable inputs and continue. See
[sequential batch parsing](docs/batch.md) and the
[validation example](examples/batch/validate_documents.py).

See [validation and issue codes](https://pyxsd.knorby.com/validation.html),
[parse modes](https://pyxsd.knorby.com/binding.html), and the
[supported-features coverage](https://pyxsd.knorby.com/supported.html).

### Generate Python types and extract structured data

Use the XSD as the source of your XML access types rather than maintaining
parallel Python classes. Compilation generates native binding classes at
runtime, including inheritance for schema extensions. Parsing binds XML
occurrences to those classes and validates their declared values and structure.
There is no static code-generation step.

Query the bound tree with `find`, `findall`, or `xpath`; export dictionaries or
JSON with `to_dict` / `to_json`; or apply transforms and write XML back out.
For schema-declared tabular types and nested records, use the Arrow adapter
below instead of inferring columns from dictionary exports.

See the [data model](https://pyxsd.knorby.com/data-model.html),
[API reference](https://pyxsd.knorby.com/api.html), and
[transform guide](https://pyxsd.knorby.com/transforms/).

### Create dynamic Pydantic access models

Install `pyxsd[pydantic]` to generate Pydantic models from a compiled XSD,
without handwritten model definitions or example XML. Validate independent
Python dictionaries, inspect JSON Schema, or take typed snapshots of bound
documents. Nested records, repeated elements, attributes, exact decimal
values, and supported constraints come from the schema.

Using the included purchase-order dataset:

```python
import pyxsd
from pyxsd.integrations.pydantic import models

schema = pyxsd.compile("examples/pydantic/orders.xsd", mode=pyxsd.ParseModes.NAMESPACED)
registry = models(schema)
Order = registry.model_for(element="orders", path=("order",))

document = schema.parse("examples/pydantic/orders.xml")
snapshot = registry.from_document(document, revalidate=True)
order = snapshot.order[0]
print(order.customer.name)  # River Research Lab
print(order.line[0].price.xml_value)  # Decimal value: 125.50
```

`Order.model_validate(data)` also validates Python input before any XML is
read. Models preserve explicit presence separately from effective defaults;
changing a snapshot does not change the XML. Follow the
[purchase-order tutorial](examples/pydantic/README.md) for standalone input,
validation errors, defaults, and absent versus nil values.

### Extract typed Arrow records and write Parquet

Install `pyxsd[arrow]` to extract XML records into Arrow tables or write them
to Parquet. Choose a record declaration in the XSD and select its occurrences
in the document. The adapter prepares column types before reading rows,
retains nested structs and lists, and uses exact representations for decimals
and integers that exceed native column widths. Empty selections keep the same
declared schema.

Using the included environmental observations:

```python
import pyxsd
from pyxsd.integrations.arrow import records

schema = pyxsd.compile("examples/arrow/observations.xsd", mode=pyxsd.ParseModes.NAMESPACED)
projection = records(schema, element="observations", path=("observation",))
document = schema.parse("examples/arrow/observations.xml")
projection.write_parquet(
    document, "observations.parquet", selector="observation", batch_size=2, revalidate=True
)
```

Use `projection.table(...)` for an in-memory table or `projection.batches(...)`
for RecordBatches. Pydantic and pandas are not required. Output batching limits
projected row count; XML parsing still materializes the document. Follow the
[Arrow/Parquet tutorial](examples/arrow/README.md) for typed measurements,
coordinates, lists, nil values, and verified Parquet readback.

Both optional adapters support a documented projection subset, not the entire
XSD validation contract. See the [integration guide](docs/integrations.md) for
type mappings, presence semantics, supported shapes, and limitations. For
runnable end-to-end workflows — DuckDB and Polars analysis plus an order
assessment application — see the
[data workflow examples](docs/data-workflows.md).

For scalar analytical rows with parent context, pass named `FieldSource`
columns. For example, a line row can retain its enclosing order number:

```python
from pyxsd.integrations.projection import FieldSource

projection = records(
    schema,
    element="orders",
    path=("order", "line"),
    columns={
        "order_number": FieldSource(scope="ancestor", levels=1, attribute="number"),
        "sku": FieldSource(attribute="sku"),
        "quantity": FieldSource(path=("quantity",)),
    },
)
table = projection.table(document, selector="order/line", revalidate=True)
```

Here `schema` and `document` use the purchase-order fixtures above. Column
routes must be singleton and scalar; lists/structs remain whole-record outputs.
Run `python examples/arrow/project_order_lines.py order-lines.parquet` for the
complete Arrow-only example with prices, currency and Parquet readback.

## Quickstart

```bash
pip install pyxsd
```

Validate and parse from the command line:

```bash
# schema is located from the instance's schemaLocation hints
pyxsd -i inventory.xml --strict

# write the parsed tree and apply a transform
pyxsd -i inventory.xml -k -o parsed.xml -t 'PrintData()'
```

Or as a library:

```python
import pyxsd

schema = pyxsd.Schema.compile("inventory.xsd")
schema.require_valid()

document = schema.parse("inventory.xml")
document.require_valid()


def normalize_units(root): ...


updated = document.transform(normalize_units)
updated.write("normalized.xml")
```

`Schema.compile` builds the schema once and generates its Python classes;
`schema.parse` binds an instance into a `Document` whose `report` holds
every issue found. A transform is any callable taking the tree root —
see the [quickstart](https://pyxsd.knorby.com/quickstart.html) and the
[full documentation](https://pyxsd.knorby.com/) for more.

## What it validates

All 45 XSD 1.0 built-in types with lexical validation; sequence/choice/all
content models; groups and attributeGroups (with refs); wildcards;
unions (including inline members); substitution groups; element refs;
`xsi:type` dispatch; `xsi:nil`; default/fixed; abstract/final; include /
import / redefine; `key`/`unique`/`keyref` with an XPath subset. Opt-in
namespace-aware validation (`--namespaces strict` / `ParseModes.NAMESPACED`)
handles `targetNamespace`, form defaults, cross-namespace imports, wildcard
namespace/`processContents`, and QName values. Facets on user-defined
simpleTypes are enforced (enumeration, pattern, length family, bounds,
digits, and whiteSpace), including values bound through `simpleContent`
complex types. Known
gaps are tabulated in the
[supported-features page](https://pyxsd.knorby.com/supported.html).

Backed by the W3C XML Schema Test Suite (xsdtests): pyxsd passes **99.78%**
of the XSD 1.1 profile and **99.61%** of the XSD 1.0 profile. For context,
the `xmlschema` library — the standard Python library in this space, and
pyxsd's conformance oracle — passes 99.75% (1.1) and 99.77% (1.0), but
declines (does not attempt) substantially more cases, so the percentages
are not directly comparable. Residual gaps are tabulated on the
[supported-features page](https://pyxsd.knorby.com/supported.html).

## Status

pyxsd was written at Oak Ridge National Laboratory in 2006 (see
[history](https://pyxsd.knorby.com/history/origins.html)),
abandoned around 2008, and revived as a Python 3 project in 2026.
Version 1.0.0 is the first release of the modernized library.

## Documentation

Full documentation is published at **<https://pyxsd.knorby.com/>**:

- [Quickstart](https://pyxsd.knorby.com/quickstart.html)
- [CLI reference](https://pyxsd.knorby.com/cli.html)
- [Architecture](https://pyxsd.knorby.com/architecture.html)
- [Data model](https://pyxsd.knorby.com/data-model.html)
- [Validation and issue codes](https://pyxsd.knorby.com/validation.html)
- [Parse modes](https://pyxsd.knorby.com/binding.html)
- [Transforms](https://pyxsd.knorby.com/transforms/)
- [Supported features](https://pyxsd.knorby.com/supported.html)
- [Pydantic and Arrow integrations](docs/integrations.md)
- [Data workflow examples](docs/data-workflows.md)
- [API reference](https://pyxsd.knorby.com/api.html)
- [Migrating from 0.1](https://pyxsd.knorby.com/migration-1.0.html)
- [Project history](https://pyxsd.knorby.com/history/origins.html)
- [Contributing](https://pyxsd.knorby.com/contributing.html)

The Sphinx sources live in `docs/`; build them locally with
`uv run sphinx-build -b html docs docs/_build/html`.

## License

BSD 3-Clause. See [LICENSE](LICENSE) — it preserves the original ORNL
provenance note.
