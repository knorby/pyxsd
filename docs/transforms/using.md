# Using transforms

A transform is anything that accepts the parsed tree root — a plain
function, or a `Transform` subclass — optionally takes its own
arguments, manipulates the tree, and (usually) returns the root so the
writer can serialize it.

## Built-in transforms

| Transform | What it does |
| --------- | ------------ |
| `PrintData` | Writes the tree as readable text to a file or stdout. |

After a transform changes the tree's shape, revalidate the transformed
document with `Document.revalidate()` — it re-parses the serialized tree
against the same schema and returns a fresh document whose report
reflects the new structure.

## Transform syntax

Transform calls are class names with parenthesized arguments:

```
PrintData()
SphereCutter(0.5)
```

The class name must match the module name containing it, modulo casing and
underscores (`PrintData` resolves to `print_data` and `printData`;
underscore-insensitive fallback matching is applied).

On the command line, chain calls with `>` and separate transforms run in
order, each seeing the previous transform's result:

```bash
pyxsd -i input.xml -t 'ExpandCell() > PrintData()'
```

Or place one call per line in a file and pass it with `-T`:

```text
ExpandCell()
PrintData()
```

(See `examples/legacy/sampleTransformFile.txt`.)

## Bundled application transforms

The crystallography transforms that shipped with pyxsd 0.1 — `ExpandCell`,
`SphereCutter`, `CellSizer`, `BravaisLattice`, `CoordViewer`,
`FormatForVisit`, plus the `Atom`/`Vector` helper libraries — now live in
**`examples/legacy/`** and are **no longer part of the installed
package**. To use them:

- run pyxsd from that directory (`cd examples/legacy`), or
- copy the transform files you need next to your data, or
- pass the module file via your transform library setup.

They expect crystallography-shaped data and are meant as adaptable examples.
See `examples/legacy/README.md`.

## Library use

When driving pyxsd as a library, transforms are callables applied with
`Document.transform`. A `Transform` subclass is invoked through a small
wrapper (the class takes the root in `__init__`, its `__call__` does the
work); a plain function taking the root needs no wrapper:

```python
import pyxsd
from pyxsd.cli import parse_transform_call, resolve_transform_class

schema = pyxsd.Schema.compile("inventory.xsd")
document = schema.parse("inventory.xml")


def normalize_units(root): ...


# A plain callable:
updated = document.transform(normalize_units)
updated.write("out.xml")

# A Transform class, resolved by name exactly like the CLI does:
name, args, kwargs = parse_transform_call("PrintData()")
transform_cls = resolve_transform_class(name)
document.transform(lambda root: transform_cls(root)(*args, **kwargs))
```

A transform that returns a tree root comes back as a new `Document`; a
transform that changes the tree in place and returns `None` leaves the
document itself as the result. After a structural change, use
`Document.revalidate()` (above) to refresh the report.
