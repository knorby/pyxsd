# pyxsd examples

- [`demo/`](demo/) — a runnable, domain-neutral end-to-end demo:
  schema, instance document, a custom transform (`CountElements`), and
  `demo.py` (library use). The walkthrough lives in
  [`docs/demo.md`](../docs/demo.md).
- [`musicxml/`](musicxml/) — a real document format: a MusicXML subset
  schema, a short score, and transforms that report on it (works as a
  strict-mode example and a conformance test).
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
- [`citygml/`](citygml/), [`bpmn/`](bpmn/), [`opendrive/`](opendrive/),
  [`xrechnung/`](xrechnung/), [`musicxml/`](musicxml/) — five self-contained
  example families, each organized around one real data source and rendering
  it end to end: CityGML buildings (SVG plan + 3D plot), BPMN 2.0 diagrams
  from their DI layout, OpenDRIVE road networks (lane maps + elevation
  profiles), UBL 2.3 / XRechnung invoices (HTML + PDF), and MusicXML
  synthesis to WAV. Each family pins and verifies its own upstream sources.
  See [`docs/rendering-examples.md`](../docs/rendering-examples.md).
- [`server/`](server/) — a FastAPI app that mounts the five families
  behind a sample browser, server-side render routes, and a validation
  route for submitted XML.
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
