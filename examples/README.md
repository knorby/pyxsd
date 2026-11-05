# pyxsd examples

- [`demo/`](demo/) — a runnable, domain-neutral end-to-end demo:
  schema, instance document, a custom transform (`CountElements`), and
  `demo.py` (library use). The walkthrough lives in
  [`docs/demo.md`](../docs/demo.md).
- [`gpx/`](gpx/) — a real geospatial format: a GPX schema subset, a
  recorded track, and a transform that computes track statistics.
- [`docx/`](docx/) — a real office format: a `word/document.xml` schema
  subset and a style-aware Markdown transform, parsed in lax mode to
  show how messy documents are bound without losing data.
- [`analytics/`](analytics/) — executable DuckDB and Polars workflows over
  schema-derived Arrow projections: nested versus contextual tables, child
  expansion, a station/region join, and a Parquet round trip. See
  [`docs/data-workflows.md`](../docs/data-workflows.md).
- [`healthcare/`](healthcare/) — real C-CDA documents with datatype mixins,
  a scoped CDA-to-FHIR R4 XML pipeline, and an MCP diagnostic walkthrough.
  One verified source-fetch command supplies both schema trees.
- [`citygml/`](citygml/) — CityGML 2.0 buildings as an SVG plan, a
  Matplotlib 3D plot, and a FastAPI server (`examples.citygml.server:app`).
- [`bpmn/`](bpmn/) — BPMN 2.0 diagrams drawn from the document's saved DI
  layout, plus a FastAPI server (`examples.bpmn.server:app`).
- [`opendrive/`](opendrive/) — ASAM OpenDRIVE lane maps, elevation
  profiles, and a FastAPI server (`examples.opendrive.server:app`).
- [`xrechnung/`](xrechnung/) — UBL 2.3 / EN 16931 invoices as standalone
  HTML and PDF, plus a FastAPI server (`examples.xrechnung.server:app`).
- [`musicxml/`](musicxml/) — MusicXML 4.0 scores as synthesized WAV, an
  SVG staff preview, and a FastAPI server (`examples.musicxml.server:app`).

Each rendering family pins and verifies its own upstream sources, owns its
multiple example uses, documents its workflow in its README, and follows
the shared pattern in
[`docs/rendering-examples.md`](../docs/rendering-examples.md).
- [`pydantic/assess_orders.py`](pydantic/assess_orders.py) — the purchase-order
  assessment application from the same guide: schema-generated snapshots,
  synthetic policy, and deterministic reason codes. The directory's
  [`demo.py`](pydantic/demo.py) covers model generation itself.
- [`legacy/`](legacy/) — the crystallography transforms from pyxsd 0.1,
  kept as reading material under `examples/legacy/` after the
  crystallography data left the project. See
  [`legacy/README.md`](legacy/README.md).
- [Transform templates](#transform-templates) — the three deployment
  patterns for user transforms, described in
  [`about_examples.md`](about_examples.md).
- [`legacy/sampleTransformFile.txt`](legacy/sampleTransformFile.txt) —
  the one-call-per-line format for the `-T` CLI option.

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
