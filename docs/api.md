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
