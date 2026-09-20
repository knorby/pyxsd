# End-to-end demo walkthrough

`examples/demo/` is a complete, domain-neutral working example of pyxsd —
schema, instance document, a custom transform, and both invocation styles.
Follow along by running the commands from that directory.

## The pieces

| File | Role |
| ---- | ---- |
| `schema.xsd` | A small XSD: `inventory` (maxOccurs unbounded, required `id` attribute) of `item` elements (`name`, `quantity`, `color`, optional `note`). |
| `instance.xml` | Two items — one with a `note`, exercising optional elements. |
| `CountElements.py` | A custom transform: prints an element-name frequency table using `iter_tree`. |
| `demo.py` | Drives the whole pipeline as a library. |

## Command line

```bash
cd examples/demo
pyxsd -i instance.xml -o transformed.xml -t 'CountElements()'
```

pyxsd finds the schema through the instance's
`xsi:noNamespaceSchemaLocation` hint (resolved **relative to the instance
file**, so this works from any working directory), builds the object
tree, runs `CountElements()`, and writes the transformed tree to
`transformed.xml`. You should see:

```
CountElements: element frequency
  color: 2
  inventory: 1
  item: 2
  name: 2
  note: 1
  quantity: 2
```

Add `--strict` to turn validation errors into exit code 1 (try removing
the `id` attribute from an item to see the `missing-attribute` report).

## Library

```python
from pyxsd.cli import parse_transform_call, resolve_transform_class
from pyxsd.document import Document
from pyxsd.schema import Schema

document = Schema.compile("schema.xsd").parse("instance.xml")
name, args, kwargs = parse_transform_call("CountElements()")
transform_cls = resolve_transform_class(name)
result = document.transform(lambda root: transform_cls(root)(*args, **kwargs))
if isinstance(result, Document):
    document = result
document.write("transformed.xml")
print(document.report)  # "pyxsd: 0 errors" on a clean document
```

`Schema.compile` builds the schema once; `parse` binds the instance and
returns a `Document`. `document.report` collects every issue;
`document.root` holds the root of the parsed tree; the class for the
root element is named `inventory|complexType` (element name,
compositor bookkeeping name). `CountElements` returns the bound root,
so `transform` hands back a fresh `Document` whose tree
`document.write` serializes to `transformed.xml`.

`demo.py` is this script with the paths anchored to its own directory
(so it also runs from the repository root) — run it with:

```bash
uv run python examples/demo/demo.py
```

## The transform

`CountElements.py` shows the standard shape:

```python
from pyxsd.transforms import Transform, iter_tree


class CountElements(Transform):
    def __init__(self, rootInstance):
        super().__init__(rootInstance)  # stores self.root

    def __call__(self):
        for node in iter_tree(self.root):
            ...  # inspect node._name_, ._attribs_, ...
        return self.root  # hand the tree to the writer
```

Rules recap (full guide in {doc}`transforms/writing`): one class per file
named after the module; `__init__` takes the root; `__call__` does the
work; returning the root lets the writer serialize it. The module is
resolved from the installed package first, then the current directory,
then the instance's directory — which is why the CLI picks up the local
file.

## Crystallography examples

The old application transforms live in `examples/legacy/` and are
used the same way — run from that directory or copy the files next to
your data. See `examples/legacy/README.md` and the transform docs in
{doc}`transforms/using`.
