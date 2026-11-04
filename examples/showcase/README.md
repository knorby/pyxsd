# Showcase examples

Real XML formats from five domains, one pinned source manifest, and a
rendering payoff for each: **see what the transform pipeline can turn
your documents into**.

| Workflow | Format | Renders |
| --- | --- | --- |
| [`citygml/`](citygml/README.md) | OGC CityGML 2.0 building models | SVG plan view + Matplotlib 3D city |
| [`bpmn/`](bpmn/README.md) | OMG BPMN 2.0 process diagrams | SVG diagrams from the BPMN DI layout |
| [`opendrive/`](opendrive/README.md) | ASAM OpenDRIVE road networks | SVG lane map + Matplotlib elevation profiles |
| [`invoice/`](invoice/README.md) | OASIS UBL 2.3 / EN 16931 (XRechnung) | server-rendered HTML invoice + PDF report |
| [`score/`](score/README.md) | MusicXML 4.0 (Bach chorale) | WAV synthesis (pure stdlib) + SVG score preview |
| [`server/`](server/README.md) | all of the above | FastAPI app: browse samples, render server-side, POST XML for validation or rendering |

## Setup

From a pyxsd checkout with Python 3.11+ and `uv`:

```sh
uv run python examples/showcase/download_sources.py
```

This verifies and installs every pinned section below `.cache/`
(gitignored; nothing downloaded is ever committed). Repeating the command
verifies the cache without network access. Sources, revisions, licenses,
and SHA-256 digests live in [`sources.toml`](sources.toml), with
[attribution and license evidence in SOURCES.md](SOURCES.md).

Optional rendering extras (`matplotlib`, `fastapi`/`uvicorn`,
`reportlab`) install as example-only dependency groups:

```sh
uv sync --group examples-matplotlib --group examples-fastapi --group examples-reportlab
```

## Why these examples exist

Browsers are dropping client-side XSLT, geospatial and engineering
formats are XML at their core, and "validate, bind, transform, render" is
exactly the pipeline pyxsd is built for. Each workflow compiles the real
upstream schema once, binds real instance documents into typed Python
trees, and walks them into a visual artifact — offline, deterministically,
and without vendoring anyone's data.
