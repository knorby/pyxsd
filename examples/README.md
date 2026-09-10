# pyxsd examples

- [`demo/`](demo/) — a runnable, domain-neutral end-to-end demo:
  schema, instance document, a custom transform (`CountElements`), and
  `demo.py` (library use). The walkthrough lives in
  [`docs/demo.md`](../docs/demo.md).
- [`transforms/`](transforms/) — the crystallography transforms from
  pyxsd 0.1 (ExpandCell, SphereCutter, CellSizer, BravaisLattice,
  CoordViewer, FormatForVisit, plus the Atom/Vector libraries),
  reorganized as adaptable examples. See [`transforms/README.md`](transforms/README.md).
- [Transform templates](#transform-templates) — the three deployment
  patterns for user transforms, described in
  [`about_examples.md`](about_examples.md).
- [`sampleTransformFile.txt`](sampleTransformFile.txt) — the
  one-call-per-line format for the `-T` CLI option.

## Running the demo

From the repository root:

```bash
uv run python examples/demo/demo.py     # library pipeline
# or the CLI equivalent
uv run pyxsd -i examples/demo/instance.xml -o out.xml -t 'CountElements()'
```

(The CLI run needs to start in `examples/demo/` — or copy the files
elsewhere — for the local `CountElements.py` transform module to
resolve.)

## Transform templates

| File | Pattern |
| ---- | ------- |
| `transform_template_fromTransform.py` | A self-contained transform subclassing `pyxsd.transforms.Transform`. |
| `transform_template_library.py` | A transform *library* of helper functions/classes. |
| `transform_template_fromLibrary.py` | A transform using a sibling library module. |
