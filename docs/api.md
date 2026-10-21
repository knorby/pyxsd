# API reference

The public entry points. The metaprogramming core (generated classes,
descriptors) is intentionally dynamic; `type[SchemaBase]` and the
`XMLNode` {class}`~pyxsd.nodes.XMLNode` protocol are the stable seams.

## Schema and documents

```{eval-rst}
.. autoclass:: pyxsd.schema.Schema
   :members:

.. autoclass:: pyxsd.document.Document
   :members:

.. autofunction:: pyxsd.schema.compile

.. autofunction:: pyxsd.schema.parse
```

`Schema.compile` (also exposed as {func}`pyxsd.compile`) builds the
schema once; `Schema.parse` binds one instance document and returns a
{class}`~pyxsd.document.Document`. The module-level
{func}`pyxsd.parse` is the one-call convenience: it resolves the schema
from an argument or the instance's own schema hints, compiles, and
binds.

## Mixin extensions

```{eval-rst}
.. autoclass:: pyxsd.extensions.Extensions
   :members:
```

{class}`~pyxsd.extensions.Extensions` (also exported as
`pyxsd.Extensions`) is a per-compile registry: register mixins with its
`for_type` / `for_namespace` / `for_kind` / `for_path` / `where`
selectors — each usable as a decorator or called directly with
`mixin=` — and pass the registry to
`Schema.compile(..., extensions=...)`. Registration is compile-time
only; the registry's `mixins_for` and the `__pyxsd_validate__` hook
contract are documented in {doc}`extensions`.

## Type stubs for generated classes

The generated classes live in `schema.classes`, created at runtime, so
mypy and IDEs cannot import them. `Schema.stub()` renders their typing
surface as `.pyi` text, and the same renderer is importable without the
method:

```{eval-rst}
.. autofunction:: pyxsd.stubs.emit_stub
```

`Schema.stub(*, compact=False) -> str` reads the **live** schema at call
time — nothing is cached at compile time — so a stub regenerated after an
overlay is loaded shows the overlay's members.

### The two modes

The full form (the default) is an importable-quality `.pyi` document: a
provenance and consumption header, imports of the real pyxsd datatype
classes (annotations name `pyxsd.xsd_data_types.Int`, not a stand-in
`int`), one class block per generated class with inheritance mirroring
the XSD derivation and simple types carrying their facets as comments,
and the global elements as module-level aliases. There is no importable
module to point at; save the text next to your code and name it under
`TYPE_CHECKING`:

```python
if TYPE_CHECKING:
    from schema_stubs import PurchaseOrderType
root = cast(PurchaseOrderType, document.root)
```

pyxsd ships the PEP 561 `py.typed` marker, so the stub's imports of
`SchemaBase` and the `pyxsd.xsd_data_types` classes resolve statically:
mypy checks code written against the stub with the real pyxsd types
behind the annotations.

The compact form (`schema.stub(compact=True)`) is the agent-oriented
description: a two-line header (provenance and an alias legend mapping
short spellings such as `str`, `int`, and `date` back to the pyxsd
classes they stand for), one line per class and member, facet-only simple
types folded onto the members that use them, and a trailing `# roots:`
comment line. It is a description, not Python source.

### Truthfulness contract

Stubs describe reads from **valid parsed documents**, and every reachable
absence is annotated:

- an element with `maxOccurs > 1` reads as `list[T]`; an optional one
  (`minOccurs="0"`, a branch of a choice, or an optional ancestor group)
  adds `| None` — an absent repeated member reads as `None`, because the
  list exists only after the first bind;
- an attribute with `use="required"` reads as `T`; every other attribute
  reads as `T | None`;
- a member carrying a `default` or `fixed` value annotates `T | str | None`
  with the value in a comment: a declaration-local element `default` reads
  back the raw lexical `str` when its element is absent, a fixed element
  reads `None`, and an attribute's `default`/`fixed` is applied typed by
  the binder.

Two asymmetries the annotations cannot encode are stated in the emitted
preamble: an absent optional member — and a required member of an
*invalid* document — reads as `None`, and repeated-element assignment
appends a single item (`obj.x = item`; assigning a list raises).

One more runtime divergence is stated there too: the parser binds a
complex-typed child in the parsed tree only, so `obj.child` reads `None`
for it, while a primitive-typed child is accessor-bound. The
annotations describe the descriptor and construction API.

Where a construct cannot be typed honestly the stub degrades instead of
lying: a member whose value comes from a union, a recursive back edge,
an unresolvable declaration, and a schema name that is not
dot-accessible become `Any`, a comment, or both; mixed content,
wildcards, and open content become class-body comments. Rendering never
raises for a schema that compiled, even one whose report carries
errors: `stub()` never calls `require_valid()`, so call it yourself
when you want to reject a bad schema.

Two rendering limitations are known and deliberate. An overlay method
parameter whose default has a non-literal repr (for example
`p=Path("x")`, emitted as `PosixPath("x")`) is written verbatim and may
fail mypy: only annotations are resolved or degraded, not defaults. And a
schema type literally named `Any` or `SchemaBase` renders a class that
collides with the emitted imports (mypy `no-redef`).

### The overlay (extension) loop

Because rendering reads the live class table, the overlay mechanism
composes with stubs into the extension workflow:

1. compile the schema and emit the stub;
2. write overlay code against the stub — autocomplete and mypy work;
3. load the overlay and emit the stub again — the overlay's members now
   appear in its class block.

```python
from pathlib import Path

from pyxsd.schema import Schema

schema = Schema.compile("orders.xsd")
Path("orders.pyi").write_text(schema.stub())

schema = Schema.compile("orders.xsd", overlay="overlay.py")
Path("orders.with_overlay.pyi").write_text(schema.stub())
```

An overlay class is loaded under its `name` attribute in place of the
generated class of the same schema type (see {doc}`architecture`), so
stub regeneration describes what the overlay's class actually provides.
The CLI emits the same text with
`pyxsd --emit-stubs [PATH] -s SCHEMA`; see {doc}`cli`.

### Example: full mode

Using the `ORDERS` schema throughout this section:

```python
import io

from pyxsd.schema import Schema

ORDERS = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="SKU"><xs:restriction base="xs:string">
    <xs:pattern value="\\d{3}-[A-Z]{2}"/>
  </xs:restriction></xs:simpleType>
  <xs:complexType name="Item"><xs:sequence>
    <xs:element name="sku" type="SKU"/>
    <xs:element name="quantity" type="xs:int"/>
    <xs:element name="note" type="xs:string" minOccurs="0"/>
  </xs:sequence>
  <xs:attribute name="id" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:element name="orders"><xs:complexType><xs:sequence>
    <xs:element name="item" type="Item" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

print(Schema.compile(io.StringIO(ORDERS)).stub())
```

```text
# Generated by pyxsd 1.2.1 from <stream>. Do not edit by hand.
#
# This file is a generated typing surface for the classes a compiled
# schema creates at runtime; there is no importable module (the classes
# live in ``schema.classes``). Save this stub next to your code and
# import it for type checking:
#     if TYPE_CHECKING:
#         from schema_stubs import Orders
#     root = cast(Orders, document.root)
#
# Semantics: annotations model valid parsed documents. Absent optional
# members read as None (including absent repeated members); required
# members of invalid documents read as None too. Repeated assignment
# appends one item (obj.x = item); it does not take a list. The parser
# binds a complex-typed child in the parsed tree only (obj.child reads
# None), while a primitive-typed child is accessor-bound; the
# annotations describe the descriptor and construction API. Constructs
# a stub cannot type truthfully are Any with a comment.

from typing import Any

from pyxsd.schema_base import SchemaBase
from pyxsd.xsd_data_types import Int, String

class SKU(String):
    ...
    # pattern: \d{3}-[A-Z]{2}

class Item(SchemaBase):
    sku: SKU
    quantity: Int
    note: String | None
    id: String

# bookkeeping name 'orders|complexType'; stub name synthesized
class Orders(SchemaBase):
    item: list[Item]

# Root elements:
orders: type[Orders]
```

The inline `orders` declaration has no name of its own, so the class is
synthesized as `Orders` with the bookkeeping name kept in a comment —
class names are never guessed silently.

### Example: compact mode

```python
print(Schema.compile(io.StringIO(ORDERS)).stub(compact=True))
```

```text
# Generated by pyxsd 1.2.1 from <stream>; compact view, not Python source.
# aliases: str=String, int=Int

class Item:
  sku: str  # pattern: \d{3}-[A-Z]{2}
  quantity: int
  note: str | None
  id: str

# bookkeeping name 'orders|complexType'; stub name synthesized
class Orders:
  item: list[Item]

# roots: orders: Orders
```

Every annotation spelling is a class the document declares, a pyxsd
builtin's own name, or a legend alias; a folded simple type's facets ride
on the members that use it.

## Batch parsing

`Schema.iter_parse` reuses the compiled schema for explicit local files.
See {doc}`batch` for lazy stop/report behavior and resource limits.

```{eval-rst}
.. autoclass:: pyxsd.batch.DocumentSource
   :members:

.. autoclass:: pyxsd.batch.ParseOutcome
   :members:

.. autoclass:: pyxsd.batch.InputFailure
   :members:

.. autoclass:: pyxsd.batch.BatchParseError
   :members:

.. autofunction:: pyxsd.batch.iter_parse
```

### Optional Parquet datasets

`pyxsd.integrations.arrow.RecordProjection.write_dataset(sources, destination,
*, selector=None, namespaces=None, batch_size=10000, errors="raise")` uses
the projection's own compiled Schema. Install `pyxsd[arrow]` for the writer;
the result/error records below need no optional dependencies. No `revalidate`
argument is needed: every source is freshly parsed and required to be valid.
See {doc}`batch` for publication and failure contracts; {doc}`integrations`
describes preparation and supported projections.

```{eval-rst}
.. autoclass:: pyxsd.integrations.dataset.DatasetResult
   :members:

.. autoclass:: pyxsd.integrations.dataset.DatasetExportError
   :members:
```

## Querying a document

`Document.xpath(expr)` evaluates an XPath expression over the bound tree
and returns the **original bound nodes** (scalars such as `count(...)`
pass through). The context item is the document's root element, so an
absolute path starts `/root`. `Document.find(path)` and
`Document.findall(path)` accept ElementTree's path subset instead. Pass
`namespaces={"p": uri}` for prefixed queries.

Queries run over a projection of the bound tree, so they see what the
object model holds: descriptor write-through is reflected, `text` mirrors
`_value_`, and a child's `tail` mirrors the recorded `_tail_` — so mixed
character data is queryable in document order. Parsing does not preserve
comment nodes, and a nil/no-content element projects `text=None`. The
projection is document-faithful, not source-XPath-exact: element-only
content's recorded layout whitespace is part of the projected text. The
projection tags nodes with their expanded (Clark-notation) name, so
prefixed queries work in both namespace modes. `Document.to_dict`/
`to_json` provide structured export (see {doc}`data-model`).

## Errors

```{eval-rst}
.. autoclass:: pyxsd.exceptions.PyXSDError
   :undoc-members:

.. autoclass:: pyxsd.exceptions.ValidationError
   :members:

.. autoclass:: pyxsd.exceptions.PyXSDWarning
   :undoc-members:
```

`PyXSDError` signals fatal input problems (unreadable or malformed
files, a document whose root is not `xs:schema`). `ValidationError` is
what `require_valid()` raises; its `report` attribute carries the full
{class}`~pyxsd.validation.ValidationReport`.

The `ValidationReport`/`ValidationIssue` classes are documented in
{doc}`validation` (detailed reference) rather than duplicated here.

## Modes and the node protocol

`BindingPolicy` and the `ParseModes` presets are documented in
{doc}`binding`. The node shape is the `XMLNode` protocol:

```{eval-rst}
.. autoclass:: pyxsd.nodes.XMLNode
   :members:
```

## Transforms

See {doc}`transforms/class` for the `Transform`/`Displayer` class
reference, `iter_tree`, and the built-ins.
