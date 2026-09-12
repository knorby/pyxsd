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
from pyxsd.transforms import Transform, iter_tree


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

## A runnable in-memory example

The transform above can be exercised without touching the filesystem:
build a parser over string streams, run the transform directly, serialize
the result to a `StringIO`, and revalidate the changed tree with
`SendTreeToPyXSD` so its report can be inspected.

```python
import io

from pyxsd.parser import PyXSD
from pyxsd.transforms import Transform, iter_tree
from pyxsd.transforms.send_tree_to_pyxsd import SendTreeToPyXSD
from pyxsd.writers import XmlTreeWriter

SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="note">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="body" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

INSTANCE = "<note><body>hello &amp; goodbye</body></note>"


class UpperValues(Transform):
    """Uppercase every string value in the tree."""

    def __init__(self, rootInstance):
        super().__init__(rootInstance)

    def __call__(self):
        for node in iter_tree(self.root):
            if node._value_:
                node._value_ = [value.upper() for value in node._value_]
        return self.root


def main():
    parser = PyXSD(
        io.StringIO(INSTANCE),
        xsdFile=io.StringIO(SCHEMA),
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
    )

    print("body before:", parser.schemaRootInstance.body)

    transformed = UpperValues(parser.schemaRootInstance)()

    output = io.StringIO()
    XmlTreeWriter(transformed, output)
    print(output.getvalue().strip())

    # Revalidate the changed tree against the same schema; the transform
    # keeps the original tree but exposes the reparse's report.
    revalidation = SendTreeToPyXSD(transformed)
    revalidation.outerParser = parser
    revalidation()
    print("revalidation errors:", [issue.code for issue in revalidation.report.issues])


if __name__ == "__main__":
    main()
```

Returning `self.root` hands the tree to the next stage; returning `None`
ends the pipeline without writing output. Both are covered in
{doc}`using`.

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
