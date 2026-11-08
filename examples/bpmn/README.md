# BPMN 2.0 diagrams from their DI layout

Real `.bpmn` files carry two layers: the semantic process model (tasks,
gateways, events, sequence flows) and a `BPMNDiagram` interchange section
with the geometry an editor drew (`dc:Bounds`, `di:waypoint`). This
workflow compiles the pinned OMG BPMN 2.0 schema set, binds both layers
into typed Python trees, and renders the drawing as a standalone SVG.

It is this family's clearest "the schema is the interface" demo: the
renderer never guesses a layout, it reads the coordinates the modeler
saved and turns them into SVG glyphs.

## Setup

```sh
uv run python examples/bpmn/download_sources.py --only bpmn-schema --only bpmn-data
```

This verifies and installs the five-schema closure (via the Apache-2.0
Camunda mirror) and four real MIT-licensed bpmn-io diagrams below
`examples/bpmn/.cache/` (gitignored). Re-running the command verifies
the cache without network access. Sources, revisions, digests, and license
evidence are in [`sources.toml`](sources.toml) and
[`SOURCES.md`](SOURCES.md).

## Render

```sh
uv run python examples/bpmn/render_svg.py examples/bpmn/.cache/bpmn-data/starter.bpmn
# wrote examples/bpmn/out/starter.svg

uv run python examples/bpmn/render_svg.py path/to/diagram.bpmn -o /tmp/diagram.svg
uv run python examples/bpmn/render_svg.py path/to/diagram.bpmn --width 1600
```

The CLI compiles the pinned schema, parses the file, and refuses to write
output when the document has validation errors (diagnostics go to
stderr). The default output directory is `examples/bpmn/out/`.

## Serve

The family also includes a local FastAPI use for curated rendering and
submitted XML validation. It reuses this family's pinned schema, sample
cache, and SVG renderer:

```sh
uv run --group examples-fastapi uvicorn examples.bpmn.server:app --port 8000
```

Open <http://127.0.0.1:8000/> for the sample browser. Rendering is
available at `/render/starter`, `/render/simple`,
`/render/sequence-flows`, and `/render/nested-subprocesses`. Submit a
document for validation and shape/edge facts:

```sh
curl --data-binary @document.bpmn -H 'Content-Type: application/xml' \
    http://127.0.0.1:8000/validate
```

This is a local example, not a deployment recipe for untrusted uploads.

## What it draws

- **Pools and lanes** as backdrop frames, from the `participant` and `lane`
  shape bounds; pool names are rotated onto the left band.
- **Tasks and subprocesses** as rounded rectangles; a subprocess gets a
  `[+]` badge.
- **Gateways** as diamonds, **events** as circles (an end event has a
  heavier stroke).
- **Sequence flows** as polylines through their DI waypoints, with one
  arrowhead marker defined in `<defs>` and referenced by every edge.
- **Labels** from the semantic element's `name` attribute (multi-line
  names keep their line breaks), XML-escaped.
- The drawing box comes from the DI coordinates, scales uniformly to
  `--width`, and flips the y axis: BPMN DI grows upward, SVG grows down.
  Output is byte-deterministic.

## Python API

```python
from examples.bpmn.render_svg import di_edges, di_shapes, render_svg
from examples.bpmn.common.schemas import compile_schema
from examples.bpmn.common.sources import require_cache

document = compile_schema().parse(require_cache("bpmn-data"))

di_shapes(document)
# [{'element_id': 'sid-...', 'kind': 'task', 'x': 393.0, 'y': 170.0,
#   'w': 100.0, 'h': 80.0, 'is_horizontal': False, 'label': 'Scan QR code'}, ...]
di_edges(document)
# [{'element_id': 'sid-...', 'waypoints': [(493.0, 210.0), (585.0, 210.0)],
#   'label': ''}, ...]
svg = render_svg(document, width=1000)
```

`kind` is one of `participant`, `lane`, `task`, `subprocess`, `gateway`,
`start_event`, `end_event`, `event`, or `other`, classified from the tag of
the element that `BPMNShape/@bpmnElement` resolves to. `element_id` is that
semantic id (not the shape's own DI id).

## Two things worth knowing

**Namespace-aware mode is mandatory.** `ParseModes.NAMESPACED` is now the
default, and this example pins it explicitly. In the legacy local-name mode,
components merge by local name, so `BPMNShape` and `BPMNEdge` no longer see
`di:DiagramElement` as their substitution-group head; schema compilation
reports two errors and every document then fails with sixteen.
`examples/bpmn/common/schemas.py` uses `ParseModes.NAMESPACED` for `bpmn`:
the schema compiles with zero issues, and all four pinned samples parse with
zero report issues.

**Ignore the file's `xsi:schemaLocation`.** Real files often point at dead
URLs (the starter diagram points at a `20100501` path that returns 404).
This example always compiles the local pinned `BPMN20.xsd` and never
resolves an instance hint, so the cached schema is the only oracle.

Child attribute access on this ref-heavy schema returns `None` for elements
reached through `xsd:element ref="…"`, so the code reads semantic elements
from `document.to_dict()` (Clark-notation keys) and DI children by expanded
name via `document.xpath(..., namespaces=...)`.

## Limitations

- **DI-driven layout only.** A `.bpmn` file with no `BPMNDiagram` renders
  an empty canvas; there is no auto-layout engine.
- Labels use semantic names, not the optional `BPMNLabel` bounds or
  `BPMNLabelStyle` fonts; edge labels sit at the midpoint of the longest
  segment rather than at their saved label bounds.
- All `BPMNDiagram` shapes in a document share one canvas (the usual case
  is a single diagram).
- Gateway markers (`X`, `+`, `O`) and expanded-subprocess contents are not
  drawn; the subprocess is a single frame with a `[+]` badge.
- BPMN extension attributes/elements (Camunda, bpmn-io colors) validate
  but are not rendered.

## Tests

```sh
uv run pytest tests/test_bpmn_examples.py tests/test_bpmn_server.py -q
```

The server tests cover the family-local HTTP contract with injected
samples and schemas, plus cache-gated real render/validate cases. The
offline tests build a synthetic BPMN subset spanning the model,
BPMN-DI, DD-DC, and DD-DI namespaces and cover shape classification,
waypoint reading, glyph output, y-axis flipping, text escaping, and byte
determinism. The real-schema tests parse all four pinned samples and skip
with the setup command above when `.cache/` is absent.
