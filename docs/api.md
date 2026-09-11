# API reference

The public entry points. The metaprogramming core (generated classes,
descriptors) is intentionally dynamic; `type[SchemaBase]` and the
`XMLNode` {class}`~pyxsd.nodes.XMLNode` protocol are the stable seams.

```{eval-rst}
.. autoclass:: pyxsd.PyXSD
   :members:
   :undoc-members:

.. autoclass:: pyxsd.exceptions.PyXSDError
   :undoc-members:

.. autoclass:: pyxsd.exceptions.PyXSDWarning
   :undoc-members:

.. autoclass:: pyxsd.nodes.XMLNode
   :members:
```

The `ValidationReport`/`ValidationIssue` classes are documented in
{doc}`validation` (detailed reference) rather than duplicated here.

## Transforms

See {doc}`transforms/class` for the `Transform`/`Displayer` class
reference, `iter_tree`, and the built-ins.
