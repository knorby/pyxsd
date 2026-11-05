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

1. **Compile once.** The family's pinned schema closure is compiled into
   Python classes once per process and reused for every document it
   renders.
2. **Bind.** `Schema.parse()` validates and binds the document into a
   typed tree. Rendering never requires a clean validation verdict:
   schemas with known stock findings (CityGML) and documents with
   recorded instance findings still bind, while diagnostics stay
   available on `schema.report`. See {doc}`validation`.
3. **Walk.** The renderer reads the bound tree — typed descriptors,
   `to_dict()` projections, or XPath — and extracts exactly the facts it
   presents.
4. **Render plain Python.** Output is built with the standard library or
   a single-purpose library, never with a stylesheet engine. SVG, HTML,
   and WAV generation need only pyxsd's base environment; the plotting,
   server, and PDF families use example-only dependency groups.

pyxsd handles schema interpretation, typed binding, and diagnostics;
application code decides how to present the data. That boundary is the
same one described in {doc}`data-workflows`, and the transform API is in
{doc}`transforms/writing`.

Sources are pinned per family: `sources.toml` records URLs, revisions,
licenses, and SHA-256 digests; the downloader installs verified files
under the family's gitignored `.cache/`, and repeating the command
verifies the cache without network access. Rendered artifacts go under
the family's gitignored `out/`. Output is byte-deterministic except where
a library stamps metadata (reportlab PDFs carry a creation date).

```sh
uv sync --group examples-matplotlib --group examples-fastapi --group examples-reportlab
```

These groups do not add dependencies to the published library; each
`uv sync` matches the groups named in that command, so omit one and it is
removed from the environment.

## The families

| Family | Data source | Artifacts | One caveat |
| --- | --- | --- | --- |
| `citygml/` | OGC CityGML 2.0 LOD1/LOD2 buildings | SVG plan, 3D PNG | known stock schema findings; needs `examples-matplotlib` |
| `bpmn/` | BPMN 2.0 with diagram interchange | SVG diagram | draws the saved layout; not an auto-layout engine |
| `opendrive/` | ASAM OpenDRIVE 1.4–1.8 roads | SVG map, profile PNG | schema picked from the header revision; needs `examples-matplotlib` |
| `xrechnung/` | UBL 2.3 / EN 16931 invoices | HTML, PDF | keeps lexical amounts; XSD validity is not business-rule conformance |
| `musicxml/` | MusicXML 4.0 scores | WAV, SVG preview | merged ties, fixed tempo; not an engraver |
| `docx/` | ECMA-376 WordprocessingML | Markdown | fetches its own schema set (`download_schemas.py`) |

Each README has the commands, the binding details that family
demonstrates, and its full list of limitations.

## FastAPI: render on the server

The shared server example brings the five pinned families behind a sample
browser, render routes, and a validation route for submitted XML:

```sh
uv run --group examples-fastapi uvicorn examples.server.app:app --reload
```

| Method and route | Purpose |
| --- | --- |
| `GET /` | Browse the sample inventory and rendering links. |
| `GET /api/samples` | Read the sample inventory as JSON. |
| `GET /render/{domain}/{sample}` | Render a known sample on the server. |
| `POST /validate/{domain}` | Submit XML and receive validation diagnostics. |

The server reuses the domain renderers rather than maintaining a second
transformation pipeline. Treat it as a local example, not a deployment
recipe for untrusted uploads; see {doc}`xml-security` for parser policies
and resource limits, and `examples/server/README.md` for endpoint details.

## Sources and licensing

See each family's `SOURCES.md` for provenance, attribution, and license
evidence, and its `sources.toml` for the pinned digests (the `docx/`
family predates the pinned-manifest discipline and documents its own
fetcher in its README). Never commit downloaded schemas or sample corpora:
they remain in the family's `.cache/`, and generated artifacts remain in
its `out/`.
