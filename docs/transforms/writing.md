# Writing transforms

Writing a transform means writing a small Python class with a fixed shape.
pyxsd loads transforms dynamically from just a name and a call string, so
the shape matters.

## The rules

Every transform must follow these rules:

1. **One class per file**, and the class name matches the file name (case
   and underscores are normalized on lookup, but match exactly to be safe).
2. **Inherit from `Transform`** (from `pyxsd.transforms`), directly or via
   a base class. `Transform` and `Displayer` are abstract — `__init__` must
   be defined.
3. **`__init__(self, rootInstance)`** takes the parsed root as its *only*
   positional argument.
4. **`__call__(self, *args, **kwargs)`** does the work; its parameters are
   the arguments given in the transform call.
5. **Return the root instance** from `__call__` if you want the writer to
   serialize it (or the next transform to consume it).

## Minimal example

```python
from pyxsd.transforms import Transform


class UpperValues(Transform):
    """Uppercase all string values in the tree."""

    def __init__(self, rootInstance):
        super().__init__(rootInstance)

    def __call__(self):
        for node in iter_tree(self.root):
            if node._value_:
                node._value_ = [v.upper() for v in node._value_]
        return self.root
```

`iter_tree(instance)` is a generator yielding every node in pre-order,
descending into lists, tuples and dicts; non-node objects are skipped.

## Using the visitor helpers

`Transform.walk(instance, visitor, *args, **kwargs)` traverses the tree and
calls `visitor(node, attrNames, elemNames, *args, **kwargs)` on every node.
Stock visitors (`classCollector`, `tagCollector`, `tagFinder`) power the
`getInstancesByClassName`, `getAllSubElements`, and `getElementsByName`
helpers; see {doc}`class` for the full API.

## Making new nodes

Use `makeElemObj(name)` to mint a node with the correct
`_name_`/`_attribs_`/`_children_`/`_value_` structure, and
`makeCommentElem(text)` for comments. Anything you assemble this way is
writable by `XmlTreeWriter`. The node shape is documented in {doc}`../data-model`.

## Re-parsing after structural changes

If your transform changes the tree's shape so much that it no longer
corresponds to the schema, wrap it with `SendTreeToPyXSD()` — it writes the
tree to a temp file and re-runs the full parse pipeline, validating the
new structure:

```bash
pyxsd -i input.xml -t 'ExpandCell() > SendTreeToPyXSD() > PrintData()'
```

## Distributing transforms

- Keep your transform (and any helper library) in its own directory; pyxsd
  resolves sibling imports of transform modules automatically.
- Name the entry-point module after the class.
- License your work under terms of your choice (BSD/MIT recommended); only
  transforms contributed *into* the pyxsd package fall under pyxsd's
  license.
- Document what the transform expects the data to look like — the bundled
  examples each carry a module docstring you can use as a template.
