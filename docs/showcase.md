# Rendering real XML formats

The workflows in `examples/showcase/` compile external XSDs, bind real XML
documents into Python trees, and render those trees as SVG, PNG, HTML, PDF,
or WAV. They cover CityGML buildings, BPMN diagrams, OpenDRIVE roads, UBL
invoices, and MusicXML scores.

The boundary is the same as in {doc}`data-workflows`: pyxsd handles schema
interpretation, typed binding, and diagnostics; application code decides how
to present the data. A compiled {class}`~pyxsd.schema.Schema` can be reused
for multiple documents. See {doc}`transforms/writing` for the transform API.

Rendering on the server also provides an alternative to browser-side XSLT:
parse and validate XML in Python, then send the browser HTML or SVG rather
than relying on a browser XSLT engine. The invoice workflow and the shared
FastAPI example demonstrate that separation.

## Setup

Run these commands from a repository checkout with Python 3.11 or newer:

```sh
uv run python examples/showcase/download_sources.py
```

`examples/showcase/sources.toml` pins source URLs, revisions, licenses, and
SHA-256 digests. The downloader installs verified files under the gitignored
`examples/showcase/.cache/`; repeating the command verifies the cache without
network access. Rendered artifacts normally go under the gitignored
`examples/showcase/out/`.

SVG, HTML, and WAV generation need no rendering dependency beyond pyxsd's
base environment. Install the optional plotting, server, and PDF dependencies
with the example-only groups:

```sh
uv sync --group examples-matplotlib --group examples-fastapi --group examples-reportlab
```

These groups do not add dependencies to the published library. As with the
analytical examples, each `uv sync` matches the groups named in that command;
omit a previously selected group and it is removed from the environment.

## CityGML: plan view and 3D buildings

The CityGML workflow reads OGC CityGML 2.0 building examples and produces a
top-down SVG plan and a Matplotlib 3D PNG:

```sh
uv run python examples/showcase/citygml/plan_svg.py
uv run python examples/showcase/citygml/plot_city3d.py
```

The renderers project surface rings, follow local `xlink:href` references,
and shade buildings using measured height. They understand LOD1/LOD2 solids
and selected semantic-surface fallbacks, not textures, terrain, or coordinate
reprojection. The 3D command needs `examples-matplotlib`.

This workflow demonstrates typed binding without concealing diagnostics:
the stock CityGML/GML schema closure reports findings, and parsing adds
instance findings, while the building geometry needed by the renderers still
binds. Rendering is not a claim that the schema or document passed
`require_valid()`. See {doc}`validation` for that distinction and
`examples/showcase/citygml/README.md` for the recorded findings and limits.

## BPMN: diagrams from saved layout

BPMN 2.0 documents contain both a semantic process and diagram interchange
(DI) geometry. The renderer uses the saved bounds and waypoints rather than
inventing a layout:

```sh
uv run python examples/showcase/bpmn/render_svg.py examples/showcase/.cache/bpmn-data/starter.bpmn
```

The standalone SVG contains pools, lanes, tasks, events, gateways, sequence
flows, and semantic labels. A document without DI data has no layout to draw;
this is not an automatic diagram-layout engine.

This workflow demonstrates `document.to_dict()` and namespace-aware
`document.xpath()` access on a reference-based schema, where child descriptor
reads can return `None` despite successful binding. Strict namespace handling
keeps the BPMN and DI components distinct. Output is byte-deterministic, and
the CLI refuses to write it when validation reports errors. Details are in
`examples/showcase/bpmn/README.md` and {doc}`data-model`.

## OpenDRIVE: lane maps and elevation profiles

The OpenDRIVE workflow samples road reference geometry and offsets lane
boundaries to make an SVG map. A separate Matplotlib command plots elevation
and, when present, superelevation:

```sh
uv run python examples/showcase/opendrive/render_map.py \
    examples/showcase/.cache/opendrive-data/Town01.xodr
uv run python examples/showcase/opendrive/plot_profiles.py \
    examples/showcase/.cache/opendrive-data/curves_elevation.xodr
```

The profile command needs `examples-matplotlib`. Geometry sampling and lane
offsetting are application code, not pyxsd features: the renderer evaluates
lines, arcs, spirals, and polynomial segments from the bound document's
`to_dict()` projection. SVG output is byte-deterministic.

The CLIs choose a pinned schema from the instance header's `revMajor` and
`revMinor`, not its unrelated `version` attribute. Junction turn paths, road
marks, and signals are not rendered. See
`examples/showcase/opendrive/README.md` for schema-version handling and limits.

## UBL: HTML and PDF invoices

The invoice workflow binds KoSIT XRechnung test invoices against the OASIS
UBL 2.3 schema and renders the same extracted facts as HTML or PDF:

```sh
uv run python examples/showcase/invoice/render_html.py
uv run --group examples-reportlab python examples/showcase/invoice/render_pdf.py
```

The HTML is self-contained and escapes document text. The PDF command uses
reportlab. Both accept an input invoice and an explicit output destination;
see `examples/showcase/invoice/README.md` for those forms.

This transform demonstrates exact amounts and presentation at the edge:
`document.to_dict(typed=False)` preserves lexical amounts such as `336.9`,
without a float conversion, and the renderer formats them as `336.90` only
for display. HTML bytes are deterministic; PDF metadata includes a creation
date, so PDF bytes are not expected to match across runs.

XSD validity is not EN 16931 or XRechnung business-rule conformance. Those
rules require Schematron validation, which pyxsd does not implement. Rendering
an invoice is not a compliance decision.

## MusicXML: WAV synthesis and score preview

The score workflow reads the existing public-domain Bach chorale in
`examples/musicxml/instance.xml` against the pinned MusicXML 4.0 schema:

```sh
uv run python examples/showcase/score/synthesize.py
uv run python examples/showcase/score/render_preview.py
```

It produces a 16-bit mono WAV at 22.05 kHz and an SVG staff preview using
only the Python standard library for rendering. Note durations and divisions
become quarter-note spans; ties merge before the spans are scheduled at the
default 96 quarter notes per minute.

This workflow demonstrates an ordered walk over the bound node tree and
deterministic synthesis: the same input produces byte-identical WAV output,
without randomness or timestamps. It is not a full engraver or performance
engine: tempo changes, repeats, dynamics, and articulation are out of scope.
See `examples/showcase/score/README.md` for the synthesis model and limits.

## FastAPI: render on the server

The shared server example brings the five domains behind a sample browser,
render routes, and a validation route for submitted XML. Its planned launch
command uses the example-only server group:

```sh
uv run --group examples-fastapi uvicorn examples.showcase.server.app:app --reload
```

The interface is organized around these routes:

| Method and route | Purpose |
| --- | --- |
| `GET /` | Browse the sample inventory and rendering links. |
| `GET /api/samples` | Read the sample inventory as JSON. |
| `GET /render/{domain}/{sample}` | Render a known sample on the server. |
| `POST /validate/{domain}` | Submit XML and receive validation diagnostics. |

The server reuses the domain renderers rather than maintaining a second
transformation pipeline. Treat it as a local example, not a deployment recipe
for untrusted uploads; see {doc}`xml-security` for parser policies and resource
limits. Server-specific setup and behavior belong in
`examples/showcase/server/README.md` as that interface is completed.

## Sources and licensing

See `examples/showcase/SOURCES.md` for provenance, attribution, and license
evidence, and `examples/showcase/sources.toml` for the pinned digests. Never
commit downloaded schemas or sample corpora: they remain in `.cache/`, and
generated artifacts remain in `out/`. The existing public-domain chorale is
already committed separately. The family overview is
`examples/showcase/README.md`; each domain README documents its own limits.
