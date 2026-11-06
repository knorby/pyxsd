# Rendering real XML formats

The rendering example families compile external XSDs, bind real XML
documents into Python trees, and render those trees as SVG, PNG, HTML,
PDF, WAV, or Markdown. This page describes the pattern they share and the
catalog of formats they cover.

Other example families do different work and have their own
documentation: the Arrow/DuckDB analysis workflows in
{doc}`data-workflows`, {doc}`batch` processing, the {doc}`demo`
transform tour, and the healthcare family described in its
`examples/healthcare/README.md`.

## The pattern

Every rendering family follows the same shape:

1. **Compile.** Compile the family's schema into Python classes. Reuse the
   compiled schema when rendering multiple documents sequentially.
2. **Bind.** `Schema.parse()` validates and binds the document into a
    typed tree. Each family decides whether errors prevent rendering:
    BPMN requires valid input, while CityGML can render with recorded
    findings. Schema findings are on `schema.report`; the merged schema
    and instance findings are on `document.report`. See {doc}`validation`.
3. **Walk.** The renderer reads the bound tree — typed descriptors,
   `to_dict()` projections, or XPath — and extracts exactly the facts it
   presents.
4. **Render plain Python.** Output is built with the standard library or
   a single-purpose library. SVG, HTML,
   and WAV generation need only pyxsd's base environment; the plotting,
   server, and PDF families use example-only dependency groups.

pyxsd handles schema interpretation, typed binding, and diagnostics;
application code decides how to present the data. That boundary is the
same one described in {doc}`data-workflows`, and the transform API is in
{doc}`transforms/writing`.

CityGML, BPMN, OpenDRIVE, XRechnung, and MusicXML pin their sources per
family: `sources.toml` records URLs, revisions,
licenses, and SHA-256 digests; the downloader installs verified files
under the family's gitignored `.cache/`, and repeating the command
verifies the cache without network access. Rendered artifacts go under
the family's gitignored `out/`. Output is byte-deterministic except where
a library stamps metadata (reportlab PDFs carry a creation date).

DOCX uses `download_schemas.py` to populate `schemas/`; its documented
transform writes `document.md` in the working directory. Follow its README
for setup and output paths.

```sh
uv sync --group examples-matplotlib --group examples-fastapi --group examples-reportlab
```

These groups do not add dependencies to the published library; each
`uv sync` matches the groups named in that command, so omit one and it is
removed from the environment.

## The families

| Family | Data source | Artifacts | One caveat |
| --- | --- | --- | --- |
| [CityGML](https://github.com/knorby/pyxsd/tree/develop/examples/citygml) | OGC CityGML 2.0 LOD1/LOD2 buildings | SVG plan, 3D PNG | known schema findings; needs `examples-matplotlib` |
| [BPMN](https://github.com/knorby/pyxsd/tree/develop/examples/bpmn) | BPMN 2.0 with diagram interchange | SVG diagram | draws the saved layout; not an auto-layout engine |
| [OpenDRIVE](https://github.com/knorby/pyxsd/tree/develop/examples/opendrive) | ASAM OpenDRIVE 1.4–1.7 roads | SVG map, profile PNG | schema picked from the header revision; needs `examples-matplotlib` |
| [XRechnung](https://github.com/knorby/pyxsd/tree/develop/examples/xrechnung) | UBL 2.3 / EN 16931 invoices | HTML, PDF | keeps lexical amounts; XSD validity is not business-rule conformance |
| [MusicXML](https://github.com/knorby/pyxsd/tree/develop/examples/musicxml) | MusicXML 4.0 scores | WAV, SVG preview | merged ties, fixed tempo; not an engraver |
| [DOCX](https://github.com/knorby/pyxsd/tree/develop/examples/docx) | ECMA-376 WordprocessingML | Markdown | fetches its own schema set (`download_schemas.py`) |

Each README has the commands, the binding details that family
demonstrates, and its full list of limitations.

## FastAPI: serve each family

Each of the five pinned XML families (CityGML, BPMN, OpenDRIVE,
XRechnung, MusicXML) owns its own local FastAPI use beside its CLI
renderers. A server reuses only that family's pinned sources, schema,
samples, and render functions:

```sh
uv run --group examples-fastapi uvicorn examples.bpmn.server:app --port 8000
uv run --group examples-fastapi uvicorn examples.citygml.server:app --port 8000
uv run --group examples-fastapi uvicorn examples.opendrive.server:app --port 8000
uv run --group examples-fastapi uvicorn examples.xrechnung.server:app --port 8000
uv run --group examples-fastapi uvicorn examples.musicxml.server:app --port 8000
```

Every family server exposes the same single-source routes:

| Method and route | Purpose |
| --- | --- |
| `GET /` | Browse that family's sample inventory. |
| `GET /api/samples` | Read that inventory as JSON. |
| `GET /render/{sample}` | Render a curated sample; `?format=` selects an optional artifact. |
| `POST /validate` | Submit XML for diagnostics and family-specific facts. |

Run one family at a time, or give each uvicorn process a different port.
These are local examples, not deployment recipes for untrusted uploads;
see {doc}`xml-security` for parser policies and resource limits. Each
family README documents its run command, samples, output formats, and
caveats.

## Sources and licensing

For the five manifest-based families, see `SOURCES.md` for provenance,
attribution, and license evidence, and `sources.toml` for pinned digests.
Their downloaded files stay in `.cache/` and default artifacts in `out/`.
DOCX documents its separate fetcher and paths in its README. Keep downloaded
schemas, sample corpora, and generated artifacts out of version control.
