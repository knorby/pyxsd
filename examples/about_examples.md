# About the examples

The `examples/` directory shows how to use pyxsd's transform system and
carries the application transforms that shipped with pyxsd 0.1.

## Transform templates

Three templates demonstrate the three ways transforms are typically
deployed:

| File | Pattern |
| ---- | ------- |
| `transform_template_fromTransform.py` | A self-contained transform that subclasses `pyxsd.transforms.Transform`. |
| `transform_template_library.py` | A transform *library*: helper functions/classes that a transform imports. |
| `transform_template_fromLibrary.py` | A transform that uses a sibling library module (`from yourTransformLibrary import ...`). |

All import the framework as `from pyxsd.transforms import Transform` —
pyxsd 1.0 ships the framework in the package; only application transforms
moved out of it.

## Legacy application transforms (`legacy/`)

The crystallography transforms from pyxsd 0.1 live under
`examples/legacy/` — they are **no longer installed with the package**
(see `docs/migration-1.0.md`) and their 2006 schemas/data are no longer
distributed, so they are kept as reading material:

| Transform | What it does |
| --------- | ------------ |
| `ExpandCell` | Fills out a unit cell from asymmetric atoms, using symmetry. |
| `SphereCutter` | Keeps only atoms within a radius of the cell origin. |
| `CellSizer` | Converts between unit cell representations (a↔b/c, lengths↔angles). |
| `BravaisLattice` | Derives the Bravais lattice from cell parameters. |
| `CoordViewer` | Emits coordinates for a viewer. |
| `FormatForVisit` | Formats cell data for the VisIt visualization tool. |

Helper libraries: `Atom` (atom objects with Cartesian/fractional
conversions) and `Vector` (3-vector math).

These expect crystallography-shaped XML data that is no longer shipped.
To read them, start in `examples/legacy/` or copy the files you need
next to your data — see `legacy/README.md` in that directory.

## Sample transform file

`legacy/sampleTransformFile.txt` shows the one-call-per-line format
accepted by the `-T`/`--transformFile` CLI option.
