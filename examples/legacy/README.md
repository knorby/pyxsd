# Crystallography example transforms (legacy)

> **Legacy.** These transforms expect the crystallography schemas and
> instance documents they were written against in 2006. That data is no
> longer distributed with pyxsd, so they are kept as reading material
> rather than runnable examples.

The transforms in this directory are the crystallography library that
shipped inside `pyxsd.transforms` in pyxsd 0.1. In pyxsd 1.0 they moved
out of the package — the package now ships only the transform framework
plus `PrintData` and `SendTreeToPyXSD` (**breaking change**; see the
migration guide). They live under `examples/legacy/` as historical
examples.

| Module | Class | What it does |
| --- | --- | --- |
| `cell_sizer.py` | `CellSizer` | Shared library: reads atom positions and Bravais vectors from the parsed tree, converts to Cartesian coordinates |
| `expand_cell.py` | `ExpandCell(x, y, z)` | Repeats the unit cell along each axis |
| `sphere_cutter.py` | `SphereCutter(radius, center=None)` | Keeps only the atoms inside a sphere |
| `coord_viewer.py` | `CoordViewer(fileName=None)` | Writes atom positions as Cartesian coordinates |
| `format_for_visit.py` | `FormatForVisit(scale=1, fileName=None)` | Writes atoms in the VisIt visualization format |
| `atom.py`, `vector.py`, `bravais_lattice.py` | — | Support classes used by `CellSizer` |

## Running them

The transform loader searches the directory you run `pyxsd` from, then
the directory holding your XML instance. Run from this directory (or
copy the files next to your data):

```console
$ cd examples/legacy
$ pyxsd -i your_lattice.xml -t 'ExpandCell(3, 3, 3)' -t 'PrintData()'
```

or reference them from a transform-call file with `-T`:

```
ExpandCell(3, 3, 3)
PrintData('newXmlFile.xml')
SphereCutter(2, (0, 0, 1))
```

These transforms expect an instance document whose structure matches
the crystallography schemas they were written against in 2006 (atom
positions, Bravais vectors, and so on), so they are primarily useful
as reading material: each one is a worked example of subclassing
`Transform`/`Displayer` with a shared sibling library.
