# Optional Pydantic and Arrow integrations

Both integrations ship in **pyxsd**, not a separate library. Python **3.11+**
remains supported, including installations with extras:

```bash
pip install 'pyxsd[pydantic]'        # Pydantic >=2.12.5,<3
pip install 'pyxsd[arrow]'           # PyArrow >=22, including Parquet
pip install 'pyxsd[pydantic,arrow]'  # Both, independently usable
```

These library minimum versions support Python 3.14; they do not require it.
Base pyxsd still depends only on `elementpath`. Extras may install compiled
packages. Importing or using one integration does not require the other.

## Generate models without instance XML

```python
import pyxsd
from pyxsd.integrations.pydantic import models

schema = pyxsd.compile("orders.xsd", mode=pyxsd.ParseModes.NAMESPACED)
registry = models(schema)
Order = registry.model_for(element="{urn:orders}order")

order = Order.model_validate({"{urn:orders}number": 7})
snapshot = registry.from_document(schema.parse("order.xml"))
description = Order.model_json_schema()
```

Models are ordinary runtime-generated Pydantic classes, separate from native
XML binding classes. Primitive roots use `RootModel`; complex roots use
`BaseModel`. Extra fields are forbidden. Bool, int, string, bytes, and list
inputs use their projected Python types, without broad coercion. Decimals
accept `Decimal` or exact decimal strings, never binary floats.

Changing a model does not change its source XML. Model-to-XML encoding,
schema-specific static typing, and pickle persistence are not provided.

### Names and local declarations

`element` selects a global element by expanded name (`{namespace}local`, or
`local` for no namespace). `path=("child", "grandchild")` follows declarations
from it; each step also uses its expanded name. Ambiguous paths are errors.
This identifies local declarations before documents or rows exist:

```python
Item = registry.model_for(element="{urn:orders}orders", path=("{urn:orders}order",))
item = registry.from_node(bound_order, element="{urn:orders}orders", path=("{urn:orders}order",))
```

Input/output aliases are expanded-name children, `@` plus expanded-name
attributes, `$` for simple content, and `$nil` for complex nil state. Safe
Python names are allocated for keywords, punctuation, collisions, and
reserved Pydantic methods. Attribute names start with `attr_`; simple content
and nil state use `xml_value` and `xml_nil`. Aliases are the unambiguous input
interface. Do not supply both an alias and its Python name for one field.
This does **not** change `Document.to_dict()` or its existing conventions.

### Presence and defaults

- Optional does not mean nillable: omission can be valid while explicit
  `None` is rejected. Required nillable fields must be supplied.
- Repeated elements are lists for zero, one, and many occurrences. Nil
  members require a nillable declaration; the list container cannot be null.
- XSD list-valued scalars are separate from repeated elements and may yield
  lists of lists.
- Attribute defaults/fixed values are available as effective values.
  Document snapshots do not mark synthesized defaults as explicitly set.
- Element defaults apply to empty **present** elements, not absent elements.
- Complex simple content always has its declared value/attribute shell.
  Nilled complex elements retain attributes and `$nil=true`, rather than
  disappearing into a plain `None`.

Use `model_fields_set` and
`model_dump(exclude_unset=True, by_alias=True)` to preserve current explicit
presence. Ordinary dumps can include defaults and `None` placeholders for
omitted optional fields; do not treat such dumps as a lossless XML codec.

## Arrow tables and Parquet

An XML document does not inherently define a table. Select the record
declaration separately from its occurrences:

```python
from pyxsd.integrations.arrow import records

projection = records(schema, element="{urn:orders}orders", path=("{urn:orders}order",))
document = schema.parse("orders.xml")
selection = {"selector": ".//o:order", "namespaces": {"o": "urn:orders"}}
table = projection.table(document, **selection)
projection.write_parquet(document, "orders.parquet", batch_size=10_000, **selection)
```

`projection.schema` exists before rows are read. No selector means the
document root is the only candidate. Selectors use `Document.findall`'s
ElementTree path contract, not arbitrary XPath scalar expressions. Every
selected occurrence must match its declaration and compiled Schema object.
Zero matches still yield a typed empty table or valid empty Parquet file.

Nested children become structs and repeated children become lists. There is
no implicit flattening, exploding, or Cartesian join. Primitive record
roots use a single `value` column. Arrow does not depend on Pydantic or pandas.

### Named scalar columns with row context

Use `columns` to extract one analytical row per selected element, including
values from its enclosing occurrences or the document root. For the bundled
purchase orders, each line can carry its order number without projecting the
entire order:

```python
from pyxsd.integrations.arrow import records
from pyxsd.integrations.projection import FieldSource

projection = records(
    schema,
    element="orders",
    path=("order", "line"),
    columns={
        "order_number": FieldSource(scope="ancestor", levels=1, attribute="number"),
        "customer_account": FieldSource(
            scope="ancestor", levels=1, path=("customer",), attribute="account"
        ),
        "sku": FieldSource(attribute="sku"),
        "quantity": FieldSource(path=("quantity",)),
        "unit_price": FieldSource(path=("price",)),
        "currency": FieldSource(path=("price",), attribute="currency"),
    },
)
table = projection.table(document, selector="order/line", revalidate=True)
```

`FieldSource` is frozen and dependency-free. Its keyword-only arguments are:

- `scope="record"` (default), `"ancestor"`, or `"root"` selects the starting
  occurrence. Ancestors require a positive integer `levels`, measured in
  element-parent edges; other scopes require `levels=0`. Climbing above the
  configured root is an error.
- `path=()` follows child declarations by expanded XML names, such as
  `("{urn:orders}price",)`. It must be a tuple, not slash-delimited text or
  Python aliases. An empty tuple addresses the starting occurrence itself.
- `attribute=None` reads the reached element's scalar/simple-content value.
  Otherwise it reads the expanded attribute name **without** an `@` prefix,
  after following `path`. `$` is not a path step.

`columns` is a nonempty mapping of nonempty output names to sources. Names
and insertion order are preserved, including punctuation; the mapping is
copied during preparation. `columns=None` retains whole-record structs/lists
and their original metadata. Named columns use projection metadata version 2
and `pyxsd:mode=columns`, with source/row identities and scalar facet metadata.

Column schemas and nullability are compiled before XML is read. Each downward
source step must be at-most-one; potentially repeated routes, struct/list
endpoints (including XSD list-valued scalars), unions, and unsupported scalar
types fail during preparation. Repetition on the root-to-row route is allowed.
No list explosion, expressions, casts, aggregates, sibling joins, or inference
from sample rows are performed.

The configured global declaration must govern the document root. A local row
path does **not** select rows automatically: supply an explicit selector.
Selectors remain ElementTree occurrence filters with optional namespace maps,
not column expressions. Every candidate must match the exact configured
declaration route; a reused declaration on another branch is not a match.
Rows retain document order, including identical-valued siblings.

Nullability includes the entire source path's optional groups, choices and
nillable steps. Missing optional content yields null; an activated container
still requires its mandatory members. A nil element's value is null, but its
ordinary attributes remain available; valid nil containers suppress descent.
Absent defaulted elements remain absent, empty present elements use their
declared default/fixed value, and optional attributes include effective
defaults/fixed values. Required absent attributes are errors, even with a fixed
value. Current `xsi:nil` must be valid and agree with an existing bound nil
flag; revalidate after changing that state. Absence and nil collapse to the
same scalar null, not a round-trip XML representation.

Without revalidation, contextual projection checks current lexical values and
attributes along extraction paths, plus traversed containers' immediate child
identities/counts and group/choice rules. It does **not** freshly validate deep
unselected sibling contents: unsupported deep sibling types need not prevent
reading supported columns. `revalidate=True` first validates the whole document
and uses the returned tree for both selection and ancestry. Historical report
errors still require explicit revalidation after repairs. XML order, identity
constraints and assertions remain parser/revalidation checks, not a guarantee
from successful projection or metadata. Namespace evidence must match current
bindings, including ancestor overrides. Original namespace declarations are
not retained as bound attributes: untouched QName values use their resolved
parse evidence, while explicit current `xmlns` overrides must agree with it.
Removing an original declaration that was never retained cannot be inferred
from bound attributes alone; this does not extend the native writer's QName
namespace-preservation contract.

Named columns share the existing batch-size, typed-empty and atomic local-path
Parquet guarantees. Batching is not XML streaming, and this feature does not
add concurrent parsing against a shared Schema.

### Type and fidelity policy

| Values | Default Arrow representation |
| --- | --- |
| Booleans | Boolean, not integer |
| Finite bounded integers | Matching standard signed/unsigned width |
| Arbitrary integers | Exact decimal integer strings |
| Decimal with finite total/fraction digit bounds | Decimal128/256 if capacity permits |
| Other decimal | Exact fixed-point strings |
| Float/double | Float64; no implicit narrowing to Float32 |
| Binary | Decoded bytes |
| Temporal/duration values | Validated XSD strings, preserving extended years, resolution and offsets |
| QName | Expanded identity, requiring trustworthy namespace context |
| XSD lists (whole-record mode only) | Fixed-item-type Arrow lists |

Decimal precision is conservative: `totalDigits=p`, `fractionDigits=s`
requires capacity up to `p+s` at scale `s`. `9999` under four total digits and
two fraction digits stays valid. No rounding or float fallback is allowed.
Exactness means numerical equality, not preservation of original decimal
spelling. Decimal256 interoperability with non-Arrow readers is not promised.

Arrow deliberately collapses absent versus nil primitive values and explicit
versus defaulted attributes. Complex nil attributes remain in structs. Schema
and field metadata describe identities, facets, projection version, and this
presence loss; metadata is not validation evidence or a schema fingerprint.

### Batching and destinations

`projection.batches(document, batch_size=..., ...)` yields RecordBatches;
`write_parquet` converts and writes successive batches without first creating
a complete projected table. Batch size is a positive integer row count, not
a total-memory limit: parsing materializes the XML tree, selection returns a
node list, and one nested record can be large. This is **not XML streaming**.

Local path writes use a sibling temporary file and replace the destination
only after successful writer closure. A failed later batch preserves an
existing destination. Caller-owned writable sinks remain open but may contain
partial output on failure. Schema preparation rejects unsupported Parquet
shapes before opening an output.

## Validation, supported subset, and errors

The initial profile supports element-only sequence/all content, non-repeating
composite groups, optional nested records, repeated child elements, simple
single-element choice branches, attributes, defaults/fixed values, simple
content, and declaration-driven nil shells. Optional groups retain mandatory
member relationships; a required choice does not become unrelated optional
fields.

Reachable repeated composite groups, complex choices, ambiguous same-name
positions, mixed/wildcard/open content, heterogeneous unions, recursive
graphs, contextual QName-valued XSD lists, and differing runtime polymorphic
types are rejected. Unsupported
unrelated declarations do not block a supported selection. Unknown fields,
overflow, and invalid values are errors, never silent nulls or dropped data.

Snapshots read current serializable lexical state, including write-through
scalar edits. Existing report errors are rejected. Reports can become stale
after mutations: use `from_document(..., revalidate=True)` or
`table`/`write_parquet(..., revalidate=True)` for fresh whole-document XML
validation. Revalidation returns a new bound document; it does not mutate the
original. Node snapshots do not establish whole-document validity.

Generated models validate their projected contract, not XML ordering,
identity/keyref scope, document assertions, or all XSD semantics. Numeric
lexical-only patterns and comparable contextual restrictions are rejected
during model generation rather than validated against invented lexical forms.
JSON Schema exposes representable bounds/lengths, with custom facet metadata;
custom validators do not magically become standard JSON Schema constraints.

Invalid XML/schema reports raise `pyxsd.ValidationError`. Unsupported shapes
and snapshot/selection failures raise `pyxsd.integrations.IntegrationError`
with declaration/row context. Independent model validation raises Pydantic's
`ValidationError`. Missing extras raise an actionable import error; a broken
optional installation retains its actual missing-module cause.

## Runnable examples

Two standalone tutorials use synthetic, substantive datasets and need only
their own extra:

- [Pydantic purchase orders](https://github.com/knorby/pyxsd/tree/develop/examples/pydantic): generate nested
  access models from the XSD before reading XML, validate Python input, inspect
  exact prices/defaults/presence, and snapshot three orders with line items.
- [Arrow/Parquet environmental observations](https://github.com/knorby/pyxsd/tree/develop/examples/arrow):
  prepare a declared nested schema, extract four observations with decimal
  readings, coordinates, lists and nil shells, then batch-write/read Parquet.

```bash
python examples/pydantic/demo.py
python examples/arrow/demo.py observations.parquet
python examples/arrow/project_order_lines.py order-lines.parquet
```

The smaller combined smoke example is still available with both extras:

```bash
python examples/optional_integrations.py output.parquet
```
