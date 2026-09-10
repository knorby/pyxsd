# Using transforms

A transform is a Python class. Each one accepts the parsed tree root and
zero or more of its own arguments, manipulates the tree, and (usually)
returns the root so the writer can serialize it.

## Built-in transforms

| Transform | What it does |
| --------- | ------------ |
| `PrintData` | Writes the tree as readable text to a file or stdout. |
| `SendTreeToPyXSD` | Re-parses the transformed tree through the full pipeline (useful after structural changes). |

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

(See `examples/sampleTransformFile.txt`.)

## Bundled application transforms

The crystallography transforms that shipped with pyxsd 0.1 — `ExpandCell`,
`SphereCutter`, `CellSizer`, `BravaisLattice`, `CoordViewer`,
`FormatForVisit`, plus the `Atom`/`Vector` helper libraries — now live in
**`examples/transforms/`** and are **no longer part of the installed
package**. To use them:

- run pyxsd from that directory (`cd examples/transforms`), or
- copy the transform files you need next to your data, or
- pass the module file via your transform library setup.

They expect crystallography-shaped data and are meant as adaptable examples.
See `examples/transforms/README.md`.

## Library use

When driving pyxsd as a library, transform calls are strings in a list:

```python
parser = PyXSD(
    xmlFileInput="inventory.xml",
    transforms=["PrintData()"],
    transformOutputName="out.xml",
)
```
