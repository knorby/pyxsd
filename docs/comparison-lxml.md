# pyxsd compared with lxml

```{eval-rst}
.. meta::
   :description: Compare pyxsd runtime XSD bindings with lxml.etree and objectify for typed XML data, validation, XPath, editing, HTML, and incremental processing.
```

[`lxml`](https://lxml.de/) is a general-purpose XML and HTML toolkit with
Python APIs backed by libxml2 and libxslt. It provides tree editing, XPath,
validation, and stylesheet execution. pyxsd focuses on XSD-driven Python
data: it compiles schema declarations into classes and binds documents to
instances of those classes.

Choose pyxsd when the schema should define your application objects and
their validation, extensions, and exports. Keep lxml when the main job is
general tree processing, HTML, or an existing lxml-based pipeline. You can
also use the libraries at different stages of an application. pyxsd does
not depend on lxml or use its validator internally.

## At a glance

| Task or property | pyxsd | lxml |
| --- | --- | --- |
| Primary model | XSD-derived Python classes and a bound `Document` | `etree` elements and trees; `objectify` adds object-style data access |
| XSD validation | XSD 1.1 default and explicit 1.0 mode; see {doc}`supported` | `etree.XMLSchema` uses libxml2's XSD 1.0 implementation; no XSD 1.1 |
| Typed values | XSD datatype classes and schema-derived restrictions | `etree` text/attributes are strings; `objectify` provides typed data elements |
| Application methods | Mixins selected from schema declarations at compile time | Custom element classes and class-lookup schemes |
| Queries | `find`, `findall`, and `xpath` return original bound nodes for element results | ElementPath, XPath 1.0, compiled XPath, and Python extension functions |
| Validation findings | Report attached to the document, stable codes, phase tags, optional occurrence locations | Validation booleans or exceptions; structured error logs with source/error details |
| Python data output | Dict/JSON export; optional XSD-derived Pydantic and Arrow/Parquet projections | Application-defined extraction from `etree` or `objectify` |
| Large instance documents | Full bound instance tree | `iterparse`, pull/feed parsers, and target callbacks |
| Other document tools | XSD validation and Python callable transforms | HTML APIs, DTD/Relax NG/ISO Schematron validation, XSLT 1.0, canonical XML |
| Installation | Pure-Python base; optional extras may contain compiled packages | Compiled extension and native libraries; binary wheels available |

These are interface and scope differences, not throughput or memory
measurements. The separate {doc}`comparison-libxml2` probe concerns one
native validator build, not current lxml releases.
See lxml's [standards overview][standards] for its supported XML languages.

## Where pyxsd helps

### From a schema to application data

In `lxml.etree`, schema validation checks a tree but does not turn its text
into schema-derived Python values. With the trusted input files from
{doc}`quickstart` and lxml installed (`python -m pip install lxml`), a
validation-and-extraction step looks like this:

```python
from lxml import etree

validator = etree.XMLSchema(file="inventory.xsd")
tree = etree.parse("inventory.xml")
validator.assertValid(tree)
ns = {"i": "urn:inventory"}
quantity = int(tree.findtext("i:item/i:quantity", namespaces=ns))
```

With pyxsd, the binding step already applies the schema's declared datatype:

```python
from pyxsd import ParseModes, Schema

schema = Schema.compile("inventory.xsd", mode=ParseModes.NAMESPACED)
schema.require_valid()
document = schema.parse("inventory.xml")
document.require_valid()
item = document.find("i:item", namespaces={"i": "urn:inventory"})
quantity = item.quantity
```

Both produce a quantity equal to `4`. In pyxsd, `quantity` uses the XSD
`positiveInteger` datatype, and the same approach applies across the
schema's declarations. You avoid writing a separate text-to-value mapping
for each field. Runtime class generation needs no source-generation step;
optional {doc}`stubs` describe the classes for static tools.

### Add behavior where the schema defines it

pyxsd turns XSD type extension into Python inheritance and lets you attach
mixins to schema-declared types. Application methods then live on the bound
objects, and a `__pyxsd_validate__` hook contributes findings to their
document's report. Selection can use a type, namespace, declaration path,
or predicate. See {doc}`extensions`.

lxml also supports [custom element classes][element-classes]. Its class
lookup mechanisms associate Python classes with XML elements; they do not
compile an XSD type hierarchy into application classes for you.

### Carry the schema into downstream tools

pyxsd's optional integrations derive Pydantic models and Arrow record
schemas from XSD, including before any XML records are present. Record
projections can write Parquet and include ancestor/root context in selected
columns. This is useful when the same schema must drive both XML checks and
the declared shape of application output. See {doc}`integrations` for
supported shapes and fidelity limits.

The report also stays with the bound `Document`: queries select the objects
your functions consume, occurrence locations help identify findings, and
`revalidate()` returns a freshly checked tree after edits. Lax retention
can preserve more data for repair while leaving validation findings visible.
See {doc}`validation` and {doc}`binding`.

## How lxml.objectify differs

[`lxml.objectify`][objectify] provides attribute-style child access,
typed data elements, and normal Python operations on values. It can also
parse with an `XMLSchema` validator.

Objectify chooses data element classes using Python type annotations,
`xsi:type` hints, and text-value inference. Structural elements use a tree
class. Passing a schema validates the XML; it does not generate classes
from each XSD complex type or use declared simple types as a general binding
map. pyxsd instead builds its classes from the compiled schema and uses
those declarations when binding. Prefer it when schema-derived types,
inheritance, and compile-time extensions should define the object model.

## Where lxml is a better fit

- General XML and HTML processing. lxml has HTML parsing and manipulation,
  comment and processing-instruction nodes, and canonical serialization.
  pyxsd's bound tree targets schema-defined data and does not preserve every
  source construct. Its mixed text can survive writing, but structured
  exports have narrower limits; see {doc}`data-model`.
- Incremental processing. lxml's [parser APIs][parsing] include `iterparse`,
  pull/feed parsing, and target callbacks. Releasing processed elements can
  reduce retained memory; `iterparse` alone does not guarantee bounded use.
  pyxsd builds a full instance tree and allows only one parse at a time per
  compiled `Schema`.
- Other XML languages and existing assets. lxml supports DTD, Relax NG, and
  ISO Schematron validation, plus XPath 1.0 and XSLT 1.0 with Python
  extensions. pyxsd's validation language is XSD and its transforms are Python
  callables. See lxml's [validation][validation] and [query/stylesheet][xpath]
  documentation for those APIs.
- Native-library integration. Keep lxml when your application depends on
  its element APIs, custom resolvers, catalogs, or other libraries that
  consume lxml trees. pyxsd's bound nodes are a different object model, not
  drop-in `etree` elements.

## Use both with an explicit boundary

An application can use lxml for general document work and pyxsd for the
XSD-driven data stage. Serialize XML at that boundary, then compile/parse
with pyxsd through a supported byte stream or file input. This is an
application pipeline, not a built-in lxml adapter: parsing again costs time
and memory, and bound output is not a byte-for-byte copy of its source.

Apply input protections at each stage. lxml exposes parser options,
[resolvers and catalogs][resolvers], and stylesheet access controls;
capabilities and defaults depend on its version and native build. pyxsd has
opt-in parser profiles, structural limits, and resource policies described
in {doc}`xml-security`. Neither a valid document nor a local-only loader
establishes a security boundary by itself.

## Native engine evidence

The page below records the scope and results of a dated libxml2 C-API probe.
lxml may bundle a different libxml2 version and adds its own parsing,
resource, and error-handling behavior, so those results must not be read as
lxml benchmark or conformance results.

```{toctree}
:maxdepth: 1

comparison-libxml2
```

## See also

- {doc}`why-pyxsd`: reasons to choose the schema-driven workflow.
- {doc}`comparison-xmlschema`: another Python XSD processor and its converters.
- {doc}`quickstart`: runnable pyxsd validation, query, and edit examples.
- {doc}`supported`: XSD coverage and known gaps.

[element-classes]: https://lxml.de/element_classes.html
[objectify]: https://lxml.de/objectify.html#how-data-types-are-matched
[parsing]: https://lxml.de/parsing.html
[validation]: https://lxml.de/validation.html
[xpath]: https://lxml.de/xpathxslt.html
[resolvers]: https://lxml.de/resolvers.html
[standards]: https://lxml.de/FAQ.html#what-standards-does-lxml-implement
