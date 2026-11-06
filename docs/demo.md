# End-to-end demo walkthrough

`examples/demo/` is a complete, domain-neutral working example of pyxsd —
schema, instance document, a custom transform, and both invocation styles.
Use a repository checkout with pyxsd installed in your Python environment.
The command-line and library snippets below run from `examples/demo/`;
the final `demo.py` command runs from the repository root.

## The pieces

| File | Role |
| ---- | ---- |
| `schema.xsd` | An `inventory` containing repeated `item` elements. Each item has a required `id` attribute, `name`, `quantity`, `color`, and optional `note`. |
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
file**), builds the object
tree, runs `CountElements()`, and writes the transformed tree to
`transformed.xml`. You should see:

```text
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

`demo.py` also prints validation and node details, with paths anchored to its
own directory. Return to the repository root before running:

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

Rules recap (full guide in {doc}`transforms/writing`): give the entry-point
class a matching module; `__init__` takes the root; `__call__` does the
work; returning the root lets the writer serialize it. The module is
resolved from the installed package first, then the current directory,
then the instance's directory — which is why the CLI picks up the local
file.

## Crystallography examples

The old application transforms in
[`examples/legacy/`](https://github.com/knorby/pyxsd/tree/develop/examples/legacy)
are historical reading material. Their original schemas and input documents
are no longer distributed, so they are not runnable follow-up examples.
See {doc}`transforms/writing` to write a transform for your own data.
